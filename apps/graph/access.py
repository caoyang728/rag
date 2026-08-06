"""apps.graph.access - 图谱数据访问权限判定

图谱数据（实体/关系/社区）没有独立的权限维度，可见性锚定在来源文档上：
- 实体可见 = 实体 source_doc_ids 中存在任一用户可读文档（resolve_doc_access）
- 关系可见 = 两个端点实体均可见（避免通过边泄露不可见实体）
- 社区可见 = 社区内存在任一可见实体

权限判定统一复用 apps.knowledge.access.filter_accessible_doc_ids（含黑名单
Deny Override 铁律 / super_admin 快路径 / 可见范围 / 共享白名单），避免与
文档检索层权限口径漂移。
"""
from apps.knowledge.access import filter_accessible_doc_ids
from apps.graph.models import GraphEntity


def filter_accessible_entity_ids(user, entity_ids=None) -> set:
    """过滤用户可读的实体 ID 集合

    实体任一来源文档用户可读即视为可见（source_doc_ids → resolve_doc_access）。
    source_doc_ids 为空的实体无权限锚点，一律不可见（系统正常流程不会产生
    此类实体，仅在文档清理/重建的中间态短暂存在）。

    Args:
        user: 当前用户
        entity_ids: 候选实体 ID 列表；None 表示全部实体

    Returns:
        可见实体 ID 集合
    """
    qs = GraphEntity.objects.all()
    if entity_ids:
        qs = qs.filter(id__in=list(entity_ids))

    # 只取 id + source_doc_ids，避免 description/embedding 大字段拖慢权限判定
    pairs = list(qs.values_list('id', 'source_doc_ids'))
    if not pairs:
        return set()

    doc_ids = set()
    for _, docs in pairs:
        doc_ids.update(docs or [])
    if not doc_ids:
        return set()

    accessible_docs = set(filter_accessible_doc_ids(user, sorted(doc_ids)))
    return {
        eid for eid, docs in pairs
        if docs and any(d in accessible_docs for d in docs)
    }
