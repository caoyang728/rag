"""
文档质量报告 - 量化评估文档的解析/切分/向量化质量

评估指标:
- 解析质量: 解析状态、文本提取完整率、表格保留率
- 切分质量: 切片数量、平均大小、大小分布均匀性
- 向量化质量: 向量化成功率、Embedding一致性
- 综合评分: 加权计算文档整体质量评分

触发时机:
- 文档解析完成后（在 parse_document 任务末尾调用）
- 手动触发重新评估
- 定期批量评估（每日对新入库文档评估）
"""
import statistics
from datetime import timedelta
from typing import List, Dict, Any, Optional

from loguru import logger

from django.utils import timezone


# ============================================================================
# 1. 单文档质量评估
# ============================================================================

def evaluate_document_quality(document_id: int) -> 'DocumentQualityReport':
    """评估单个文档的入库质量

    分析该文档的所有 chunk，计算解析/切分/向量化质量指标，
    生成综合质量评分。

    Args:
        document_id: 文档 ID

    Returns:
        DocumentQualityReport 实例
    """
    from apps.analytics.models import DocumentQualityReport
    from apps.knowledge.models import Document, DocumentChunk
    from apps.retrieval.models import DocumentVector

    try:
        doc = Document.objects.get(id=document_id)
    except Document.DoesNotExist:
        raise ValueError(f'Document {document_id} not found')

    # 一次性加载所有 chunk,避免 count() + 迭代 + table 子查询三次往返
    chunk_list = list(DocumentChunk.objects.filter(document=doc))
    chunk_count = len(chunk_list)

    if chunk_count == 0:
        # 无切片，解析可能失败
        report = DocumentQualityReport.objects.update_or_create(
            document=doc,
            defaults={
                'parse_status': 'failed',
                'parse_error_rate': 1.0,
                'text_extraction_chars': 0,
                'text_extraction_rate': 0.0,
                'chunk_count': 0,
                'avg_chunk_chars': 0,
                'chunk_size_stddev': 0.0,
                'min_chunk_chars': 0,
                'max_chunk_chars': 0,
                'table_chunk_count': 0,
                'embedding_success_rate': 0.0,
                'failed_chunk_count': 0,
                'quality_score': 0.0,
                'quality_issues': [{'level': 'error', 'type': 'no_chunks', 'detail': '文档解析后无切片生成'}],
                'evaluated_at': timezone.now(),
            },
        )[0]
        logger.warning(f'[DocQuality] Document {document_id} has 0 chunks (parse failed)')
        return report

    # --- 解析质量 ---
    chunk_contents = [c.content or '' for c in chunk_list]
    text_chars = sum(len(c) for c in chunk_contents)

    # 估算预期字符数（基于文件大小的粗略估算）
    expected_chars = _estimate_expected_chars(doc.file_path)
    text_extraction_rate = min(1.0, text_chars / max(expected_chars, 1))

    # 表格切片数(从已加载的 chunk_list 统计,避免额外查询)
    table_chunk_count = sum(1 for c in chunk_list if c.chunk_type == 'table')

    # --- 切分质量 ---
    chunk_sizes = [len(c) for c in chunk_contents]
    avg_chunk_chars = int(statistics.mean(chunk_sizes)) if chunk_sizes else 0
    chunk_size_stddev = statistics.stdev(chunk_sizes) if len(chunk_sizes) > 1 else 0.0
    min_chunk_chars = min(chunk_sizes) if chunk_sizes else 0
    max_chunk_chars = max(chunk_sizes) if chunk_sizes else 0

    # --- 向量化质量 ---
    vector_count = DocumentVector.objects.filter(document=doc).count()
    embedding_success_rate = vector_count / max(chunk_count, 1)
    failed_chunk_count = chunk_count - vector_count

    # --- 计算综合质量分 ---
    parse_score = _calc_parse_score(text_extraction_rate, doc)
    chunk_score = _calc_chunk_score(chunk_sizes, chunk_count)
    embed_score = _calc_embed_score(embedding_success_rate)
    quality_score = round(parse_score * 0.4 + chunk_score * 0.3 + embed_score * 0.3, 1)

    # --- 收集质量问题 ---
    issues = _collect_quality_issues(
        text_extraction_rate=text_extraction_rate,
        avg_chunk_chars=avg_chunk_chars,
        chunk_size_stddev=chunk_size_stddev,
        min_chunk_chars=min_chunk_chars,
        embedding_success_rate=embedding_success_rate,
        table_chunk_count=table_chunk_count,
    )

    report, created = DocumentQualityReport.objects.update_or_create(
        document=doc,
        defaults={
            'parse_status': 'success' if text_extraction_rate > 0.1 else 'partial',
            'parse_error_rate': round(1.0 - text_extraction_rate, 4),
            'text_extraction_chars': text_chars,
            'expected_chars': expected_chars,
            'text_extraction_rate': round(text_extraction_rate, 4),
            'chunk_count': chunk_count,
            'avg_chunk_chars': avg_chunk_chars,
            'chunk_size_stddev': round(chunk_size_stddev, 2),
            'min_chunk_chars': min_chunk_chars,
            'max_chunk_chars': max_chunk_chars,
            'table_chunk_count': table_chunk_count,
            'embedding_success_rate': round(embedding_success_rate, 4),
            'failed_chunk_count': failed_chunk_count,
            'quality_score': quality_score,
            'quality_issues': issues,
            'evaluated_at': timezone.now(),
        },
    )

    logger.info(
        f'[DocQuality] {doc.file_name}: score={quality_score}, '
        f'chunks={chunk_count}, avg_size={avg_chunk_chars}chars, '
        f'embed_rate={embedding_success_rate:.1%}'
    )
    return report


def _estimate_expected_chars(file_path: str) -> int:
    """基于文件路径粗略估算预期字符数

    对于无法直接获取文件大小的情况，返回一个合理的估算值。
    PDF/DOCX 等二进制格式按文件大小 * 0.7 估算可提取文字比例。
    """
    import os
    try:
        if file_path.startswith('oss://'):
            # OSS 文件无法直接获取大小，返回默认值
            return 5000
        size_bytes = os.path.getsize(file_path)
        # 粗略估算：二进制文件 1 字节 ≈ 0.7 个可提取字符
        return int(size_bytes * 0.7)
    except (OSError, ValueError):
        return 5000


def _calc_parse_score(extraction_rate: float, doc) -> float:
    """计算解析质量分（0-100）

    基准：text_extraction_rate > 0.8 为满分
    """
    return min(100.0, extraction_rate * 100)


def _calc_chunk_score(chunk_sizes: List[int], chunk_count: int) -> float:
    """计算切分质量分（0-100）

    考量:
    - chunk 数量是否合理（至少 1 个）
    - chunk 大小是否在合理范围（200-1000 字符为优）
    - 大小分布是否均匀
    """
    if chunk_count == 0:
        return 0.0

    score = 100.0

    # 太小或太大的 chunk 扣分
    small_chunks = sum(1 for s in chunk_sizes if s < 100)
    large_chunks = sum(1 for s in chunk_sizes if s > 2000)
    score -= (small_chunks + large_chunks) * 5

    # 标准差过大扣分
    if len(chunk_sizes) > 1:
        avg = statistics.mean(chunk_sizes)
        stddev = statistics.stdev(chunk_sizes)
        cv = stddev / max(avg, 1)  # 变异系数
        if cv > 0.8:
            score -= 20
        elif cv > 0.5:
            score -= 10

    return max(0.0, min(100.0, score))


def _calc_embed_score(success_rate: float) -> float:
    """计算向量化质量分（0-100）"""
    return success_rate * 100


def _collect_quality_issues(**kwargs) -> List[Dict]:
    """收集质量问题列表"""
    issues = []

    text_rate = kwargs.get('text_extraction_rate', 1.0)
    if text_rate < 0.5:
        issues.append({
            'level': 'error',
            'type': 'low_extraction',
            'detail': f'文本提取完整率仅 {text_rate:.0%}，可能存在解析问题',
        })
    elif text_rate < 0.8:
        issues.append({
            'level': 'warning',
            'type': 'moderate_extraction',
            'detail': f'文本提取完整率 {text_rate:.0%}，建议检查解析器配置',
        })

    avg_size = kwargs.get('avg_chunk_chars', 0)
    if avg_size < 100:
        issues.append({
            'level': 'warning',
            'type': 'too_small_chunks',
            'detail': f'平均切片大小 {avg_size} 字符，可能过于碎片化',
        })
    elif avg_size > 2000:
        issues.append({
            'level': 'warning',
            'type': 'too_large_chunks',
            'detail': f'平均切片大小 {avg_size} 字符，可能影响检索精度',
        })

    embed_rate = kwargs.get('embedding_success_rate', 1.0)
    if embed_rate < 0.8:
        issues.append({
            'level': 'error',
            'type': 'low_embed_rate',
            'detail': f'向量化成功率仅 {embed_rate:.0%}，部分切片无法被检索',
        })

    return issues


# ============================================================================
# 2. 批量文档质量评估
# ============================================================================

def batch_evaluate_document_quality(
    days: int = 7,
    root_type: Optional[str] = None,
    min_chunks: int = 1,
) -> Dict[str, Any]:
    """批量评估最近 N 天入库的文档质量

    Args:
        days: 评估最近几天的文档
        root_type: 可选，仅评估指定类型的文档
        min_chunks: 最少切片数阈值

    Returns:
        评估汇总结果
    """
    from apps.analytics.models import DocumentQualityReport
    from apps.knowledge.models import Document

    since = timezone.now() - timedelta(days=days)
    docs = Document.objects.filter(
        created_at__date__gte=since.date(),
        status='done',
        is_deleted=False,
    )
    if root_type:
        docs = docs.filter(root_type=root_type)

    total = docs.count()
    evaluated = 0
    failed = 0
    scores = []

    for doc in docs.iterator():
        try:
            report = evaluate_document_quality(doc.id)
            evaluated += 1
            scores.append(report.quality_score)
        except Exception as e:
            logger.warning(f'[DocQuality] Failed to evaluate document {doc.id}: {e}')
            failed += 1

    summary = {
        'period_days': days,
        'total_documents': total,
        'evaluated': evaluated,
        'failed': failed,
        'avg_quality_score': round(statistics.mean(scores), 1) if scores else 0,
        'min_score': round(min(scores), 1) if scores else 0,
        'max_score': round(max(scores), 1) if scores else 0,
    }

    logger.info(f'[DocQuality] Batch evaluation: {summary}')
    return summary


# ============================================================================
# 3. 文档质量报告查询
# ============================================================================

def get_document_quality_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    root_type: Optional[str] = None,
    dept_id: Optional[int] = None,
    team_id: Optional[int] = None,
) -> Dict[str, Any]:
    """获取文档质量汇总数据（供 Dashboard 使用）

    组织筛选优先:team 有值时忽略 dept,按 Document.dept_id/team_id 归属过滤。
    root_type 保留兼容(目前文档 root_type 多为 knowledge_base,区分度弱)。

    Returns:
        {
            'total_docs': N,
            'avg_score': float,
            'score_distribution': {'excellent': N, 'good': N, 'fair': N, 'poor': N},
            'common_issues': [{'type': '...', 'count': N}],
            'recent_reports': [...]
        }
    """
    from apps.analytics.models import DocumentQualityReport

    qs = DocumentQualityReport.objects.all()
    if start_date:
        qs = qs.filter(created_at__date__gte=start_date)
    if end_date:
        qs = qs.filter(created_at__date__lte=end_date)
    if root_type:
        qs = qs.filter(document__root_type=root_type)
    # 组织筛选:按 Document 归属过滤(DocumentQualityReport 通过 document FK 关联)
    if team_id:
        qs = qs.filter(document__team_id=team_id)
    elif dept_id:
        qs = qs.filter(document__dept_id=dept_id)

    total = qs.count()
    scores = list(qs.values_list('quality_score', flat=True))
    avg_score = round(statistics.mean(scores), 1) if scores else 0

    # 评分分布
    distribution = {'excellent': 0, 'good': 0, 'fair': 0, 'poor': 0}
    for s in scores:
        if s >= 85:
            distribution['excellent'] += 1
        elif s >= 70:
            distribution['good'] += 1
        elif s >= 50:
            distribution['fair'] += 1
        else:
            distribution['poor'] += 1

    # 常见问题统计:按 type 聚合计数,同时保留每类问题的最高严重级别
    # level(error/warning) 映射为前端 severity(high/mid/low),供图标着色
    issue_map = {}
    for report in qs.values_list('quality_issues', flat=True):
        if report:
            for issue in report:
                issue_type = issue.get('type', 'unknown')
                level = issue.get('level', 'warning')
                if issue_type not in issue_map:
                    issue_map[issue_type] = {'count': 0, 'level': level}
                issue_map[issue_type]['count'] += 1
                # error 优先级高于 warning
                if level == 'error' and issue_map[issue_type]['level'] != 'error':
                    issue_map[issue_type]['level'] = 'error'

    # level → severity 映射,与前端 sevClass 期望值一致
    level_to_severity = {'error': 'high', 'warning': 'mid'}
    common_issues = sorted(
        [
            {
                'type': k,
                'count': v['count'],
                'severity': level_to_severity.get(v['level'], 'low'),
            }
            for k, v in issue_map.items()
        ],
        key=lambda x: x['count'],
        reverse=True,
    )[:10]

    # 最近报告
    recent = list(qs.order_by('-created_at')[:20].values(
        'id', 'document_id', 'quality_score', 'parse_status',
        'chunk_count', 'embedding_success_rate', 'quality_issues',
        'created_at',
    ))

    return {
        'total_docs': total,
        'avg_score': avg_score,
        'score_distribution': distribution,
        'common_issues': common_issues,
        'recent_reports': recent,
    }
