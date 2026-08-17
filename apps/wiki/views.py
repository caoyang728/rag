"""apps.wiki views —— Wiki 页面浏览与管理 API

- GET    /api/v1/wiki/pages/              Wiki 列表（按节点/领域/状态过滤 + 搜索 + 分页）
- GET    /api/v1/wiki/pages/<id>/         Wiki 详情（含正文 / 章节 / 链接）
- POST   /api/v1/wiki/pages/generate/     手动触发生成（{node_id} → 异步任务）
- POST   /api/v1/wiki/pages/<id>/refresh/ 刷新页面（基于最新文档重新生成）
- POST   /api/v1/wiki/pages/<id>/expire/  标记过期（内容失效待刷新）

权限口径：
- 浏览：对齐来源知识节点下文档的访问判定（apps.wiki.access.can_read_wiki）；
  community 页面仅系统管理员 / 知识库管理员可见。
- 生成 / 刷新 / 过期：对齐文档上传权限（apps.wiki.access.can_manage_wiki）。
"""
from django.db import models
from loguru import logger
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from django.utils import timezone

from apps.knowledge.models import KnowledgeNode
from apps.wiki.access import can_read_wiki, can_manage_wiki, get_accessible_node_ids
from apps.wiki.models import WikiPage
from apps.wiki.serializers import WikiPageDetailSerializer, WikiPageListSerializer


class WikiPageViewSet(viewsets.ReadOnlyModelViewSet):
    """Wiki 页面只读浏览（列表 + 详情）+ 刷新 / 过期管理动作"""

    queryset = WikiPage.objects.select_related('node').all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return WikiPageDetailSerializer
        return WikiPageListSerializer

    def get_queryset(self):
        """按业务参数过滤后，再做节点级权限过滤，缩小权限计算范围

        非管理员只保留"节点下存在用户可读文档"的 Wiki 页面（community 页面
        仅管理员可见，直接过滤掉）；管理员可见全部。
        """
        user = self.request.user
        qs = super().get_queryset()

        # ---- 业务过滤（先收窄候选，避免对全量节点做权限计算）----
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        node_id = self.request.query_params.get('node_id')
        if node_id:
            qs = qs.filter(node_id=node_id)
        root_type = self.request.query_params.get('root_type')
        if root_type:
            qs = qs.filter(node__root_type=root_type)
        q = self.request.query_params.get('q')
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(summary__icontains=q)
                | Q(tags__contains=[q])
            )

        # ---- 权限过滤 ----
        if not (getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False)):
            candidate_node_ids = set(
                qs.filter(node_id__isnull=False)
                .values_list('node_id', flat=True).distinct()
            )
            accessible_node_ids = get_accessible_node_ids(user, candidate_node_ids)
            qs = qs.filter(node_id__in=accessible_node_ids)

        return qs.order_by('-updated_at')

    def retrieve(self, request, *args, **kwargs):
        """详情：显式鉴权（不可读返回 403），阅读计数 +1（尽力而为）"""
        try:
            page = WikiPage.objects.select_related('node').get(id=kwargs['pk'])
        except WikiPage.DoesNotExist:
            raise NotFound('Wiki 页面不存在')

        if not can_read_wiki(request.user, page):
            raise PermissionDenied('您没有权限浏览该 Wiki 页面')

        # 阅读计数使用原子自增，失败不影响主流程
        WikiPage.objects.filter(id=page.id).update(
            view_count=models.F('view_count') + 1)
        page.refresh_from_db(fields=['view_count'])

        serializer = self.get_serializer(page)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='refresh')
    def refresh(self, request, pk=None):
        """重新生成页面：基于节点下最新文档异步刷新（update_or_create 覆盖）"""
        page = self.get_object()
        if not page.node_id:
            return Response({'detail': '仅知识节点 Wiki 支持刷新'}, status=status.HTTP_400_BAD_REQUEST)
        if not can_manage_wiki(request.user, page.node):
            raise PermissionDenied('您没有权限刷新该 Wiki 页面')

        try:
            from apps.wiki.tasks import generate_wiki_for_node
            generate_wiki_for_node.delay(page.node_id)
        except Exception as e:
            logger.exception('触发 Wiki 刷新任务失败')
            return Response({'detail': f'任务触发失败: {str(e)[:200]}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'ok': True, 'detail': 'Wiki 刷新任务已提交，完成后自动更新页面'})

    @action(detail=True, methods=['post'], url_path='expire')
    def expire(self, request, pk=None):
        """标记页面过期：记录操作人 / 时间 / 原因，幂等

        权限对齐刷新（can_manage_wiki，即节点管理者 / 管理员）；仅知识节点 Wiki 支持，
        community 页面无文档权限维度，不允许标记过期。
        过期后页面仍可浏览（页首提示内容可能已过时），刷新重建后自动恢复为已发布。
        """
        page = self.get_object()
        if not page.node_id:
            return Response({'detail': '仅知识节点 Wiki 支持标记过期'}, status=status.HTTP_400_BAD_REQUEST)
        if not can_manage_wiki(request.user, page.node):
            raise PermissionDenied('您没有权限操作该 Wiki 页面')

        # 已是过期状态：幂等返回，不重复覆盖操作人 / 原因
        if page.status == 'expired':
            return Response({'ok': True, 'detail': '该页面已标记为过期'})

        reason = (request.data.get('reason') or '')[:500]
        WikiPage.objects.filter(id=page.id).update(
            status='expired',
            expire_reason=reason,
            expired_by=request.user,
            expired_at=timezone.now(),
        )
        logger.info(f'[Wiki] 页面 {page.id} 被 {request.user.username} 标记过期: {reason}')
        return Response({'ok': True, 'detail': '已标记为过期'})

    @action(detail=True, methods=['get'], url_path='resolve_doc')
    def resolve_doc(self, request, pk=None):
        """按链接文字（文件名 / 标题）在节点下解析来源文档

        正文参考资料链接（[文件名](#)）点击时，前端可能因 source_docs 上限
        （20 条）或标题/文件名差异匹配不到，这里提供服务端兜底：在节点全部
        已完成文档中按 file_name / title 精确匹配（不设上限），返回文档 id 与
        当前用户可访问标记，前端据此直接预览或提示申请权限。
        community 页面无文档来源，直接返回 not_found。
        """
        page = self.get_object()
        if not page.node_id:
            return Response({'found': False})
        name = (request.query_params.get('name') or '').strip()
        if not name:
            return Response({'found': False, 'detail': '缺少 name 参数'},
                            status=status.HTTP_400_BAD_REQUEST)

        from apps.knowledge.access import filter_accessible_doc_ids
        from apps.knowledge.models import Document

        doc = (
            Document.objects.filter(
                node_id=page.node_id, is_deleted=False, status='done'
            ).filter(Q(file_name=name) | Q(title=name)).order_by('id').first()
        )
        if not doc:
            return Response({'found': False})

        user = request.user
        if getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False):
            can_access = True
        else:
            can_access = doc.id in set(filter_accessible_doc_ids(user, [doc.id]))
        return Response({
            'found': True,
            'id': doc.id,
            'title': doc.title,
            'file_name': doc.file_name,
            'file_type': doc.file_type,
            'can_access': can_access,
        })


class WikiPageGenerateView(APIView):
    """POST /api/v1/wiki/pages/generate/ —— 手动为节点触发生成 Wiki

    独立路由（不放在 ViewSet 上）避免与 /pages/<pk>/ 的路径冲突。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        node_id = request.data.get('node_id')
        if not node_id:
            return Response({'detail': 'node_id 必填'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            node = KnowledgeNode.objects.get(id=node_id, is_deleted=False)
        except KnowledgeNode.DoesNotExist:
            return Response({'detail': '节点不存在'}, status=status.HTTP_404_NOT_FOUND)

        if not can_manage_wiki(request.user, node):
            raise PermissionDenied('您没有权限为该节点生成 Wiki')

        try:
            from apps.wiki.tasks import generate_wiki_for_node
            generate_wiki_for_node.delay(node.id)
        except Exception as e:
            logger.exception('触发 Wiki 生成任务失败')
            return Response({'detail': f'任务触发失败: {str(e)[:200]}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'ok': True, 'detail': 'Wiki 生成任务已提交，生成完成后即可浏览'})
