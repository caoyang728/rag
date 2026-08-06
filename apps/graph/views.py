"""apps.graph views —— 图谱可视化与实体检索 API

- GET    /api/v1/graph/entities/              实体列表（q 名称模糊 / type 过滤 + 分页）
- GET    /api/v1/graph/entities/search/       语义向量检索实体（q 必填，返回相似度得分）
- GET    /api/v1/graph/entities/<id>/         实体详情（含可见来源文档）
- GET    /api/v1/graph/entities/<id>/neighbors/  实体邻居子图（depth 1~2 跳）
- GET    /api/v1/graph/communities/           社区列表（level 过滤 + 分页）
- GET    /api/v1/graph/communities/<id>/      社区详情（含可见实体）
- POST   /api/v1/graph/communities/detect/    手动触发社区检测（仅知识库管理员 / 超管）

权限口径：图谱数据锚定来源文档权限（实体 source_doc_ids → resolve_doc_access，
见 apps.graph.access）——实体任一来源文档可读即可见；关系仅当两端实体均可见
时返回（避免通过边泄露不可见实体）；社区含任一可见实体即可见。黑名单
Deny Override 对超管同样生效。
"""
from loguru import logger
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.graph.access import filter_accessible_entity_ids
from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity
from apps.graph.serializers import (
    EntityListSerializer, EntityDetailSerializer,
    CommunityListSerializer, CommunityDetailSerializer,
)
from apps.graph.vector_search import search_entities
from apps.knowledge.access import filter_accessible_doc_ids
from apps.knowledge.models import Document
from apps.llm.embedding import get_embedding_client

# 邻居展开最大跳数：超过上限的深度无业务意义且会放大返回体
MAX_NEIGHBOR_DEPTH = 2
# 子图节点数硬上限：防止大枢纽实体一次扩展拉爆接口与前端渲染
MAX_SUBGRAPH_NODES = 500
# 单次语义检索返回数量上限
MAX_SEARCH_TOP_K = 50


def _build_entity_source_docs(user, entity) -> list:
    """返回实体的可见来源文档列表（[{id, title}]，按文档 ID 升序）

    仅包含用户可读的文档；不可见部分通过 source_doc_count 总量体现，
    避免详情页直接暴露无权限文档的标题。

    Args:
        user: 当前用户
        entity: GraphEntity 实例

    Returns:
        [{id, title}] 列表
    """
    doc_ids = entity.source_doc_ids or []
    if not doc_ids:
        return []
    accessible = set(filter_accessible_doc_ids(user, doc_ids))
    if not accessible:
        return []
    docs = Document.objects.filter(id__in=accessible).values('id', 'title')
    return sorted(
        ({'id': d['id'], 'title': d['title']} for d in docs),
        key=lambda d: d['id'],
    )


def _collect_subgraph(user, seed_entity_ids, depth) -> tuple:
    """从种子实体做 BFS 多跳扩展，返回权限过滤后的可见子图

    扩展过程对不可见实体只使用其 ID（不读取数据、不泄露内容），最终仅返回
    用户可读的节点与两端端点均可见的关系。

    Args:
        user: 当前用户
        seed_entity_ids: 种子实体 ID 列表
        depth: 扩展跳数（1~2）

    Returns:
        (nodes: list[GraphEntity], edges: list[GraphRelation])
    """
    current = set(seed_entity_ids)
    visited = set(seed_entity_ids)

    for _ in range(depth):
        if not current:
            break
        rels = GraphRelation.objects.filter(
            Q(source_entity_id__in=current) | Q(target_entity_id__in=current)
        ).values_list('source_entity_id', 'target_entity_id')
        next_ids = set()
        for src, tgt in rels.iterator():
            next_ids.add(src)
            next_ids.add(tgt)
        next_ids -= visited
        visited |= next_ids
        if len(visited) > MAX_SUBGRAPH_NODES:
            # 子图过大时截断扩展，避免一次拉爆接口与前端渲染
            logger.info(f'[Graph Views] 子图超过上限截断 seeds={len(seed_entity_ids)} visited={len(visited)}')
            break
        current = next_ids

    # 权限过滤：仅保留用户可读的实体
    accessible = filter_accessible_entity_ids(user, visited)
    if not accessible:
        return [], []

    nodes = list(GraphEntity.objects.filter(id__in=accessible))
    edges = list(
        GraphRelation.objects.filter(
            source_entity_id__in=accessible,
            target_entity_id__in=accessible,
        ).select_related('source_entity', 'target_entity')
    )
    return nodes, edges


def _page_ids(ordered_ids, params) -> list:
    """对有序 ID 列表做内存分页（graph 列表接口无 DB 分页，权限过滤在 Python 侧）

    Args:
        ordered_ids: 有序候选 ID 列表（已按展示顺序排列）
        params: request.query_params

    Returns:
        当前页 ID 切片
    """
    try:
        page = max(int(params.get('page', 1)), 1)
        page_size = min(int(params.get('page_size', 20)), 100)
    except (TypeError, ValueError):
        page, page_size = 1, 20
    start = (page - 1) * page_size
    return ordered_ids[start:start + page_size]


class EntityViewSet(viewsets.ReadOnlyModelViewSet):
    """图谱实体浏览与检索（列表 / 详情 / 语义搜索 / 邻居子图）"""

    queryset = GraphEntity.objects.all()
    permission_classes = [IsAuthenticated]
    # lookup 默认使用 pk（实体主键即 id），避免 detail 路由把 kwargs 传成 id 导致签名不匹配
    lookup_field = 'pk'

    def list(self, request, *args, **kwargs):
        """实体列表：q（名称模糊）/ type（实体类型）过滤 + 权限过滤 + 分页

        分页在 Python 侧完成：实体可见性取决于来源文档权限，无法在 SQL 层
        一次过滤；先取有序实体 ID，再批量做文档权限判定后分页，保持
        updated_at 倒序的展示顺序。
        """
        params = request.query_params
        qs = GraphEntity.objects.all()
        q = (params.get('q') or '').strip()
        if q:
            qs = qs.filter(name__icontains=q)
        etype = params.get('type')
        if etype:
            qs = qs.filter(type=etype)
        qs = qs.order_by('-updated_at')

        ordered_ids = list(qs.values_list('id', flat=True))
        if not ordered_ids:
            return Response({'count': 0, 'results': []})

        accessible = filter_accessible_entity_ids(request.user, ordered_ids)
        visible_ids = [eid for eid in ordered_ids if eid in accessible]
        page_ids = _page_ids(visible_ids, params)

        objs = {e.id: e for e in GraphEntity.objects.filter(id__in=page_ids)}
        entities = [objs[i] for i in page_ids if i in objs]
        serializer = EntityListSerializer(entities, many=True, context={'request': request})
        return Response({'count': len(visible_ids), 'results': serializer.data})

    def retrieve(self, request, pk=None):
        """实体详情：显式鉴权（不可读返回 403）+ 可见来源文档"""
        try:
            entity = GraphEntity.objects.get(id=pk)
        except GraphEntity.DoesNotExist:
            raise NotFound('实体不存在')

        accessible = filter_accessible_entity_ids(request.user, [entity.id])
        if entity.id not in accessible:
            raise PermissionDenied('您没有权限查看该实体')

        source_docs = _build_entity_source_docs(request.user, entity)
        serializer = EntityDetailSerializer(
            entity, context={'request': request, 'source_docs': source_docs})
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='search')
    def search(self, request):
        """语义向量检索实体：query 向量召回 + 权限过滤 + 相似度得分

        q 必填；返回结果按相似度降序，前端取顶部实体渲染其关系子图。
        """
        q = (request.query_params.get('q') or '').strip()
        if not q:
            return Response({'detail': 'q 必填'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            top_k = min(int(request.query_params.get('top_k', 10)), MAX_SEARCH_TOP_K)
        except (TypeError, ValueError):
            top_k = 10

        # 可选按实体类型过滤（前端类型下拉联动）
        entity_types = None
        etype = request.query_params.get('type')
        if etype:
            entity_types = [etype]

        try:
            qvec = get_embedding_client().embed_one(q)
        except Exception as e:
            logger.error(f'[Graph Views] 实体语义检索向量生成失败: {e}')
            return Response({'detail': '向量生成失败，请稍后重试'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if not qvec or all(v == 0.0 for v in qvec):
            return Response({'results': []})

        hits = search_entities(qvec, top_k=top_k, entity_types=entity_types)
        if not hits:
            return Response({'results': []})

        # 权限过滤：无权限实体不出现在结果中
        accessible = filter_accessible_entity_ids(
            request.user, [h['entity_id'] for h in hits])
        hits = [h for h in hits if h['entity_id'] in accessible]
        return Response({'results': hits})

    @action(detail=True, methods=['get'], url_path='neighbors')
    def neighbors(self, request, pk=None):
        """实体邻居子图：从实体出发扩展 1~2 跳，返回可见节点 + 两端可见的边

        前端渲染关系子图并支持点击实体继续扩展邻居。
        """
        try:
            entity = GraphEntity.objects.get(id=pk)
        except GraphEntity.DoesNotExist:
            raise NotFound('实体不存在')

        # 邻居子图以中心实体可见为前提（不可见实体不提供任何扩展入口）
        accessible = filter_accessible_entity_ids(request.user, [entity.id])
        if entity.id not in accessible:
            raise PermissionDenied('您没有权限查看该实体')

        try:
            depth = min(int(request.query_params.get('depth', 2)), MAX_NEIGHBOR_DEPTH)
        except (TypeError, ValueError):
            depth = 2

        nodes, edges = _collect_subgraph(request.user, [entity.id], depth)

        node_data = [{
            'id': n.id, 'name': n.name, 'type': n.type,
            'type_label': n.get_type_display(),
            'description': n.description[:200],
            'is_center': n.id == entity.id,
        } for n in nodes]
        edge_data = [{
            'id': r.id, 'source': r.source_entity_id, 'target': r.target_entity_id,
            'relation_type': r.relation_type, 'weight': r.weight,
        } for r in edges]

        return Response({
            'center': entity.id,
            'depth': depth,
            'node_count': len(node_data),
            'edge_count': len(edge_data),
            'nodes': node_data,
            'edges': edge_data,
        })


class CommunityViewSet(viewsets.ReadOnlyModelViewSet):
    """图谱社区浏览与检测（列表 / 详情 / 手动触发检测）"""

    queryset = GraphCommunity.objects.all()
    permission_classes = [IsAuthenticated]
    # lookup 默认使用 pk（社区主键即 id），避免 detail 路由把 kwargs 传成 id 导致签名不匹配
    lookup_field = 'pk'

    def list(self, request, *args, **kwargs):
        """社区列表：q 关键词 + level 过滤 + 权限过滤（含任一可见实体）+ 分页"""
        params = request.query_params
        qs = GraphCommunity.objects.all()
        level = params.get('level')
        if level:
            qs = qs.filter(level=level)
        # 关键词搜索：匹配主题（metadata.topic）/ 摘要 / 关键词数组元素
        q_text = params.get('q', '').strip()
        if q_text:
            qs = qs.filter(
                Q(metadata__topic__icontains=q_text)
                | Q(summary__icontains=q_text)
                | Q(keywords__contains=[q_text])
            )
        qs = qs.order_by('-updated_at')

        candidates = list(qs.values('id', 'entity_ids'))
        if not candidates:
            return Response({'count': 0, 'results': []})

        all_entity_ids = set()
        for c in candidates:
            all_entity_ids.update(c['entity_ids'] or [])
        accessible = filter_accessible_entity_ids(request.user, all_entity_ids) if all_entity_ids else set()

        visible_ids = [
            c['id'] for c in candidates
            if any(e in accessible for e in (c['entity_ids'] or []))
        ]
        page_ids = _page_ids(visible_ids, params)

        objs = {c.id: c for c in GraphCommunity.objects.filter(id__in=page_ids)}
        communities = [objs[i] for i in page_ids if i in objs]
        serializer = CommunityListSerializer(communities, many=True, context={'request': request})
        return Response({'count': len(visible_ids), 'results': serializer.data})

    def retrieve(self, request, pk=None):
        """社区详情：显式鉴权（不可读返回 403）+ 社区内可见实体"""
        try:
            community = GraphCommunity.objects.get(id=pk)
        except GraphCommunity.DoesNotExist:
            raise NotFound('社区不存在')

        entity_ids = community.entity_ids or []
        accessible = filter_accessible_entity_ids(request.user, entity_ids) if entity_ids else set()
        if not any(e in accessible for e in entity_ids):
            raise PermissionDenied('您没有权限查看该社区')

        entities = GraphEntity.objects.filter(id__in=accessible).order_by('name')
        entity_data = [{
            'id': e.id, 'name': e.name, 'type': e.type,
            'type_label': e.get_type_display(),
            'description': e.description[:200],
        } for e in entities]
        serializer = CommunityDetailSerializer(
            community, context={'request': request, 'entities': entity_data})
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='detect')
    def detect(self, request):
        """手动触发社区检测：提交 community_detection_task 异步任务

        社区是图谱全量重建（Louvain + LLM 摘要），成本较高，仅限知识库
        管理员 / 超管触发。
        """
        user = request.user
        if not (user.is_super_admin or user.is_kb_admin):
            raise PermissionDenied('仅知识库管理员可触发社区检测')

        from apps.graph.tasks import community_detection_task
        try:
            community_detection_task.delay()
        except Exception as e:
            logger.exception('触发社区检测任务失败')
            return Response({'detail': f'任务触发失败: {str(e)[:200]}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'ok': True, 'detail': '社区检测任务已提交，检测完成后自动生成社区摘要'})
