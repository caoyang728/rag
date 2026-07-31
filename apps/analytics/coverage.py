"""
知识库覆盖率评估 + 反馈闭环自动化

核心功能:
1. 热门问题覆盖率: 统计高频查询中有多少被知识库覆盖
2. 知识空白检测: 识别长期无相关文档的查询
3. 重复切片检测: 检测近似重复的 chunk
4. 反馈闭环: 差评自动关联问题 chunk，触发重新入库审核
5. 领域覆盖分析: 按部门/团队统计知识覆盖情况
"""
import time
import uuid
from datetime import timedelta
from typing import List, Dict, Any, Optional

from loguru import logger

from django.db.models import Count, Q, Avg
from django.utils import timezone


# ============================================================================
# 1. 热门问题覆盖率
# ============================================================================

def analyze_hot_query_coverage(days: int = 7) -> Dict[str, Any]:
    """分析热门问题的知识库覆盖率

    统计最近 N 天的高频查询，检查每个查询是否至少命中一个相关文档。

    Args:
        days: 统计周期（天）

    Returns:
        覆盖率报告数据
    """
    from apps.chat.models import QaRecord

    since = timezone.now() - timedelta(days=days)

    # 获取高频问题（按 question 去重计数）
    hot_queries = (
        QaRecord.objects
        .filter(created_at__date__gte=since.date())
        .exclude(is_hit_cache=True)
        .values('question')
        .annotate(query_count=Count('id'))
        .order_by('-query_count')[:200]
    )

    total = 0
    covered = 0
    uncovered_queries = []

    for hq in hot_queries:
        question = hq['question']
        count = hq['query_count']
        total += 1

        # 检查是否有至少一个命中
        qa_records = QaRecord.objects.filter(
            question=question,
            created_at__date__gte=since.date(),
            is_hit_cache=False,
        )
        has_hits = False
        for qa in qa_records:
            if qa.retrieval_hits and len(qa.retrieval_hits) > 0:
                has_hits = True
                break

        if has_hits:
            covered += 1
        else:
            uncovered_queries.append({
                'query': question[:100],
                'count': count,
                'suggestion': '建议检查相关文档是否已入库或解析是否成功',
            })

    coverage_rate = covered / max(total, 1)

    return {
        'period_days': days,
        'total_hot_queries': total,
        'covered_queries': covered,
        'uncovered_queries': total - covered,
        'hot_query_coverage_rate': round(coverage_rate, 4),
        'uncovered_examples': uncovered_queries[:20],
    }


# ============================================================================
# 2. 知识空白检测
# ============================================================================

def detect_knowledge_gaps(days: int = 7, min_count: int = 3) -> List[Dict[str, Any]]:
    """检测知识库中的空白区域

    识别高频查询中返回"无相关资料"的查询，作为知识空白候选。

    Args:
        days: 统计周期
        min_count: 最小查询次数（过滤偶然查询）

    Returns:
        知识空白查询列表
    """
    from apps.chat.models import QaRecord

    since = timezone.now() - timedelta(days=days)

    # 高频查询中 answer_type='refused' 的查询
    refused_queries = (
        QaRecord.objects
        .filter(
            created_at__date__gte=since.date(),
            answer_type='refused',
            is_hit_cache=False,
        )
        .values('question')
        .annotate(count=Count('id'))
        .filter(count__gte=min_count)
        .order_by('-count')[:50]
    )

    gaps = []
    for rq in refused_queries:
        gaps.append({
            'query': rq['question'][:200],
            'count': rq['count'],
            'suggestion': _generate_gap_suggestion(rq['question']),
        })

    return gaps


def _generate_gap_suggestion(query: str) -> str:
    """为知识空白查询生成补充建议"""
    # 简单的关键词提取建议
    keywords = []
    for word in query.split():
        if len(word) >= 2:
            keywords.append(word)

    if keywords:
        return f'建议补充关于"{" ".join(keywords[:5])}"相关的文档'
    return '建议检查知识库覆盖范围是否满足该查询需求'


# ============================================================================
# 3. 重复切片检测
# ============================================================================

def detect_duplicate_chunks(similarity_threshold: float = 0.9) -> Dict[str, Any]:
    """检测近似重复的 chunk

    使用简单的文本相似度检测（Jaccard 相似度），识别重复切片。

    Args:
        similarity_threshold: 相似度阈值（0-1）

    Returns:
        重复检测结果
    """
    from apps.knowledge.models import DocumentChunk

    chunks = DocumentChunk.objects.filter(
        document__is_deleted=False,
        document__status='done',
    ).exclude(content='').only('id', 'document_id', 'content', 'section_path')

    total = chunks.count()
    if total == 0:
        return {'total': 0, 'duplicate_rate': 0.0, 'duplicate_groups': []}

    # 采样检测（避免全量计算开销）
    sample_size = min(total, 5000)
    chunks_list = list(chunks[:sample_size])

    duplicates = []
    seen_hashes = {}

    # 使用 n-gram Jaccard 相似度
    for chunk in chunks_list:
        content = (chunk.content or '')[:500].lower()
        if not content:
            continue

        # 简单哈希分桶
        words = set(content.split())
        if len(words) < 5:
            continue

        # 检查是否与已处理的 chunk 相似
        for prev_id, prev_words in seen_hashes.items():
            intersection = words & prev_words
            union = words | prev_words
            if len(union) == 0:
                continue
            similarity = len(intersection) / len(union)
            if similarity >= similarity_threshold:
                duplicates.append({
                    'chunk_a_id': prev_id,
                    'chunk_b_id': chunk.id,
                    'similarity': round(similarity, 3),
                    'content_preview': content[:100],
                })
                break

        seen_hashes[chunk.id] = words

    dup_rate = len(duplicates) / max(sample_size, 1)

    return {
        'total_chunks_checked': sample_size,
        'duplicate_count': len(duplicates),
        'duplicate_rate': round(dup_rate, 4),
        'duplicate_examples': duplicates[:20],
    }


# ============================================================================
# 4. 领域覆盖分析
# ============================================================================

def analyze_domain_coverage(days: int = 30) -> Dict[str, Any]:
    """分析各部门/团队的知识覆盖情况

    由于所有文档的 root_type 都是 knowledge_base，仅按 root_type 分组无意义。
    改为按 部门(dept_node) → 团队(team_node) 层级拆分，展示各组织的：
    文档数、切片数、查询总数、查询命中率、文档占比。

    Args:
        days: 查询命中率的统计周期

    Returns:
        领域覆盖数据（按部门分组，每组包含下属团队明细）
    """
    from apps.knowledge.models import Document, DocumentChunk, KnowledgeNode
    from apps.chat.models import QaRecord

    since = timezone.now() - timedelta(days=days)

    # 预加载部门/团队节点信息，用于名称查找
    dept_nodes = {
        n.id: n.name for n in KnowledgeNode.objects.filter(
            node_level=2, is_deleted=False
        )
    }
    team_nodes = {
        n.id: n.name for n in KnowledgeNode.objects.filter(
            node_level=3, is_deleted=False
        )
    }

    # 按 (dept_node_id, team_node_id) 统计文档和切片
    # 注意: Count('id') 必须加 distinct=True，否则 Document 与 DocumentChunk 的 JOIN
    # 会导致 doc_count 被切片数膨胀（例如每文档 5 个切片，doc_count 就会是实际的 5 倍）
    doc_chunk_stats = (
        Document.objects
        .filter(is_deleted=False, status='done')
        .values('dept_node_id', 'team_node_id')
        .annotate(
            doc_count=Count('id', distinct=True),
            chunk_count=Count('chunks', distinct=True),
        )
    )

    # 按 QA 记录的命中情况统计（通过 retrieval_hits 关联的 document 计算部门/团队覆盖）
    # QaRecord 自身不含 dept/team 字段，但可以通过关联文档推断。
    # 简化方案：直接统计各部门文档被命中的次数
    query_stats = (
        QaRecord.objects
        .filter(created_at__date__gte=since.date(), is_hit_cache=False)
        .exclude(retrieval_hits=[])
        .values('root_type')
        .annotate(total_queries=Count('id'))
    )
    # 如果 QaRecord 没有按 dept 过滤的字段，退化为全局命中率
    total_queries_global = QaRecord.objects.filter(
        created_at__date__gte=since.date(), is_hit_cache=False
    ).count()
    hit_queries_global = QaRecord.objects.filter(
        created_at__date__gte=since.date(),
        is_hit_cache=False,
    ).exclude(retrieval_hits=[]).count()
    global_hit_rate = hit_queries_global / max(total_queries_global, 1)

    # 汇总为"部门 → 团队"的嵌套结构
    dept_map = {}
    total_docs = 0

    for ds in doc_chunk_stats:
        dept_id = ds['dept_node_id']
        team_id = ds['team_node_id']
        doc_count = ds['doc_count']
        chunk_count = ds['chunk_count']
        total_docs += doc_count

        dept_name = dept_nodes.get(dept_id, f'未知部门(#{dept_id})') if dept_id else '未分配部门'
        team_name = team_nodes.get(team_id, f'未知团队(#{team_id})') if team_id else '未分配团队'

        if dept_name not in dept_map:
            dept_map[dept_name] = {
                'dept_node_id': dept_id,
                'doc_count': 0,
                'chunk_count': 0,
                'teams': {},
            }
        dept_map[dept_name]['doc_count'] += doc_count
        dept_map[dept_name]['chunk_count'] += chunk_count

        if team_name not in dept_map[dept_name]['teams']:
            dept_map[dept_name]['teams'][team_name] = {
                'team_node_id': team_id,
                'doc_count': 0,
                'chunk_count': 0,
            }
        dept_map[dept_name]['teams'][team_name]['doc_count'] += doc_count
        dept_map[dept_name]['teams'][team_name]['chunk_count'] += chunk_count

    # 计算占比和覆盖率（查询命中率在部门级别共享全局值）
    domain_coverage = []
    for dept_name, dept_data in dept_map.items():
        dept_doc_count = dept_data['doc_count']
        dept_coverage_rate = round(dept_doc_count / max(total_docs, 1), 4)
        domain_coverage.append({
            'name': dept_name,
            'dept_node_id': dept_data['dept_node_id'],
            'doc_count': dept_doc_count,
            'chunk_count': dept_data['chunk_count'],
            '占比': dept_coverage_rate,
            'query_hit_rate': round(global_hit_rate, 4),
            'total_queries': total_queries_global,
            'hit_queries': hit_queries_global,
            'teams': list(dept_data['teams'].items()),
        })

    # 按文档数降序排列
    domain_coverage.sort(key=lambda x: x['doc_count'], reverse=True)

    return {
        'period_days': days,
        'total_docs': total_docs,
        'total_queries': total_queries_global,
        'hit_queries': hit_queries_global,
        'global_hit_rate': round(global_hit_rate, 4),
        'domain_coverage': domain_coverage,
    }


# ============================================================================
# 5. 反馈闭环自动化
# ============================================================================

def auto_link_feedback_to_chunks(days: int = 7) -> Dict[str, Any]:
    """将差评反馈自动关联到问题的命中 chunk

    对最近的差评进行分析：
    1. 获取差评对应的 QA 记录
    2. 提取检索命中的 chunk
    3. 标记可能有问题的 chunk（差评标签含"不准确"/"过时"等）
    4. 生成问题报告供运营审核

    Args:
        days: 统计周期

    Returns:
        反馈闭环处理结果
    """
    from apps.chat.models import QaFeedback, QaRecord
    from apps.analytics.models import CoverageReport

    since = timezone.now() - timedelta(days=days)

    bad_feedbacks = (
        QaFeedback.objects
        .filter(
            rating__lt=0,
            status__in=['pending', 'processing'],
            created_at__date__gte=since.date(),
        )
        .select_related('qa_record', 'user')
    )

    linked_count = 0
    issue_chunks = []

    for fb in bad_feedbacks:
        qa = fb.qa_record
        if not qa:
            continue

        # 差评标签分析
        tags = fb.tags or []
        comment = fb.comment or ''

        # 判断是否需要关联 chunk
        need_investigation = any(
            tag in tags for tag in ['不准确', '过时', '不相关', '无引用']
        ) or len(comment) > 20

        if need_investigation and qa.retrieval_hits:
            linked_count += 1
            issue_chunks.append({
                'feedback_id': fb.id,
                'qa_record_id': qa.id,
                'question': qa.question[:100],
                'chunk_ids': qa.retrieval_hits[:5],
                'rating': fb.rating,
                'tags': tags,
                'comment': comment[:200],
                'suggestion': _suggest_resolution(tags, comment),
            })

    return {
        'period_days': days,
        'total_bad_feedbacks': bad_feedbacks.count(),
        'linked_count': linked_count,
        'issue_chunks': issue_chunks[:50],
    }


def _suggest_resolution(tags: List[str], comment: str) -> str:
    """根据差评标签和评论生成处理建议"""
    suggestions = []
    tag_set = set(tags)

    if '不准确' in tag_set or '不相关' in tag_set:
        suggestions.append('检查命中的切片是否与问题相关，可能需要调整切片策略或补充相关文档')
    if '过时' in tag_set:
        suggestions.append('文档可能已过期，建议检查并更新相关文档')
    if '无引用' in tag_set or '引用错误' in tag_set:
        suggestions.append('检查回答的引用来源是否正确，可能需要优化引用逻辑')
    if '回答慢' in tag_set or '速度' in tag_set:
        suggestions.append('响应速度问题，建议检查 LLM 配置和缓存策略')

    if not suggestions:
        suggestions.append('通用反馈，建议人工审核具体问题场景')

    return '；'.join(suggestions)


# ============================================================================
# 6. 生成覆盖率报告
# ============================================================================

def generate_coverage_report(days: int = 7) -> 'CoverageReport':
    """生成每日覆盖率报告

    将所有覆盖率指标汇总为一条 CoverageReport 记录。

    Args:
        days: 统计周期

    Returns:
        CoverageReport 实例
    """
    from apps.analytics.models import CoverageReport

    report_date = timezone.now().date()

    # 执行各项分析
    coverage_data = analyze_hot_query_coverage(days)
    gap_data = detect_knowledge_gaps(days)
    duplicate_data = detect_duplicate_chunks()
    domain_data = analyze_domain_coverage(days)
    feedback_data = auto_link_feedback_to_chunks(days)

    report = CoverageReport.objects.update_or_create(
        report_date=report_date,
        defaults={
            'total_hot_queries': coverage_data['total_hot_queries'],
            'covered_queries': coverage_data['covered_queries'],
            'uncovered_queries': coverage_data['uncovered_queries'],
            'hot_query_coverage_rate': coverage_data['hot_query_coverage_rate'],
            'gap_queries': gap_data,
            'gap_count': len(gap_data),
            'duplicate_chunk_rate': duplicate_data['duplicate_rate'],
            'duplicate_chunk_count': duplicate_data['duplicate_count'],
            'domain_coverage': domain_data['domain_coverage'],
            'feedback_loop_count': feedback_data['linked_count'],
            'feedback_resolved_count': 0,  # 需要人工确认后更新
        },
    )[0]

    logger.info(
        f'[CoverageReport] Generated for {report_date}: '
        f'coverage={coverage_data["hot_query_coverage_rate"]:.1%}, '
        f'gaps={len(gap_data)}, duplicates={duplicate_data["duplicate_count"]}'
    )
    return report
