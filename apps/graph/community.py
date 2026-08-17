"""
社区检测与摘要生成（GraphRAG 的 Global Search 数据基础）
- build_graph: 从数据库构建 networkx 图
- detect_communities: Louvain 社区发现（多粒度 level 0/1/2）
- generate_community_summary: LLM 生成社区摘要
- run_community_detection: 完整流程（检测 + 摘要）
"""
import json
import re
from typing import List, Dict, Optional

from django.db.models import Q
from loguru import logger
import networkx as nx
from networkx.algorithms.community import louvain_communities

from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity

COMMUNITY_SUMMARY_PROMPT = """你是一个知识图谱分析师。以下是某个社区内的实体和关系列表。

实体列表：
{entities_text}

关系列表：
{relations_text}

请分析这个社区，生成以下内容（JSON格式，不要添加额外内容）：
{{
  "topic": "社区主题名称（一句话概括，不超过20字）",
  "summary": "社区摘要（200字以内，描述社区的核心主题和关键实体）",
  "keywords": ["关键词1", "关键词2", "关键词3"]
}}
"""

# 实体列表过长时截断，避免摘要 prompt 超出上下文
MAX_ENTITIES_TEXT = 60
MAX_RELATIONS_TEXT = 100


def build_graph() -> nx.Graph:
    """从数据库构建 networkx 图。

    以实体 ID 为节点、关系为边（weight 作为边权），
    同时确保仅出现在关系端点中的实体也作为孤立节点入图。

    Returns:
        无向图（Louvain 基于无向图）
    """
    G = nx.Graph()

    relations = GraphRelation.objects.select_related('source_entity', 'target_entity').all()
    for rel in relations:
        G.add_edge(
            rel.source_entity_id,
            rel.target_entity_id,
            weight=rel.weight,
            relation_type=rel.relation_type,
        )

    # 确保所有有关系的实体都在图中
    all_entity_ids = set()
    all_entity_ids.update(GraphRelation.objects.values_list('source_entity_id', flat=True))
    all_entity_ids.update(GraphRelation.objects.values_list('target_entity_id', flat=True))
    for eid in all_entity_ids:
        if eid not in G:
            G.add_node(eid)

    return G


def detect_communities(level: int = 0) -> List[Dict]:
    """社区检测，使用 Louvain 算法。

    Args:
        level: 社区粒度，0=细粒度, 1=中等, 2=粗粒度

    Returns:
        [{'community_id': int, 'entity_ids': List[int], 'level': int}]
    """
    G = build_graph()
    if G.number_of_nodes() == 0:
        return []

    # resolution 控制社区粒度：值越大社区越细
    resolution = {0: 1.5, 1: 1.0, 2: 0.5}.get(level, 1.0)
    communities = louvain_communities(G, resolution=resolution, seed=42)

    results = []
    for cid, community in enumerate(communities):
        results.append({
            'community_id': cid,
            'entity_ids': sorted(list(community)),
            'level': level,
        })

    return results


def generate_community_summary(community: "GraphCommunity", llm) -> str:
    """用 LLM 生成社区摘要，并落库。

    直接传入刚创建的社区实例（而非按 community_id+level 反查）：整体重建期间
    旧社区尚未删除，按 community_id+level 查询会命中新旧多条记录，只能按 pk 定位。

    Args:
        community: 待生成摘要的社区实例（含 entity_ids）
        llm: LLM 实例

    Returns:
        生成的摘要文本
    """
    entity_ids = community.entity_ids

    entities = GraphEntity.objects.filter(id__in=entity_ids)
    relations = GraphRelation.objects.filter(
        Q(source_entity_id__in=entity_ids) & Q(target_entity_id__in=entity_ids)
    ).select_related('source_entity', 'target_entity')

    entities_text = '\n'.join([
        f"- {e.name} ({e.get_type_display()}): {e.description[:100]}"
        for e in entities[:MAX_ENTITIES_TEXT]
    ]) or "无"

    relations_text = '\n'.join([
        f"- {r.source_entity.name} --[{r.relation_type}]--> {r.target_entity.name}"
        for r in relations[:MAX_RELATIONS_TEXT]
    ]) or "无"

    prompt = COMMUNITY_SUMMARY_PROMPT.format(
        entities_text=entities_text,
        relations_text=relations_text,
    )

    resp = llm.chat([{'role': 'user', 'content': prompt}],
                    temperature=0.3, max_tokens=1024)
    content = resp.get('content', '')

    # 提取 JSON（兼容 ```json 代码块）
    try:
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
        if json_match:
            data = json.loads(json_match.group(1))
        else:
            data = json.loads(content)

        community.summary = data.get('summary', content[:300])
        community.keywords = data.get('keywords', [])
        community.metadata['topic'] = data.get('topic', '')
        community.save(update_fields=['summary', 'keywords', 'metadata', 'updated_at'])
    except (json.JSONDecodeError, ValueError, TypeError):
        # LLM 未返回合法 JSON 时，降级直接用原始文本作为摘要
        community.summary = content[:500]
        community.save(update_fields=['summary', 'updated_at'])

    return community.summary


def run_community_detection(llm, levels: List[int] = None) -> int:
    """运行完整社区检测流程：检测 + 摘要生成。

    Args:
        llm: LLM 实例
        levels: 要检测的层级，默认 [0, 1, 2]

    Returns:
        创建的社区数量
    """
    if levels is None:
        levels = [0, 1, 2]

    # 旧社区不先删：全部新社区创建成功后才清理旧数据，任务中途被杀（超时/崩溃）时
    # 旧数据得以保留，避免社区列表被清空（曾因检测任务被杀后社区表为空）。
    # 重建期间新旧数据短暂共存，成功完成后用 pk 排除法删除全部旧记录。
    new_pks = []

    total_count = 0
    for level in levels:
        communities = detect_communities(level)
        for comm in communities:
            obj = GraphCommunity.objects.create(
                community_id=comm['community_id'],
                level=comm['level'],
                entity_ids=comm['entity_ids'],
            )
            new_pks.append(obj.pk)
            total_count += 1
            # 摘要生成失败不阻断整体流程
            try:
                generate_community_summary(obj, llm)
            except Exception as e:
                logger.error(f'[Graph Community] 摘要生成失败 community_id={obj.community_id}: {e}')

    # 新社区全部创建（并尽力生成摘要）后，删除旧社区记录
    GraphCommunity.objects.exclude(pk__in=new_pks).delete()

    logger.info(f'[Graph Community] 检测完成，共 {total_count} 个社区（levels={levels}）')
    return total_count
