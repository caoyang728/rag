"""
Wiki 页面质量评估 —— 对发布的 WikiPage 按源文档 chunks 做 LLM-as-Judge

指标（复用 DeepEval 框架，与 deepeval_metrics 同一套模型接入）：
- faithfulness(忠实度): 页面内容是否忠于源文档切片（无幻觉）
- completeness(完整性): 页面是否完整覆盖源文档的关键要点

评估对象为 node 挂载型页面（基于节点下文档生成）；
community 挂载型页面无源文档切片，跳过评估。
成本控制：单页面 2 次 LLM 调用，批量任务默认只处理近期更新或从未评估的页面。
"""
import time
from typing import List, Optional

from loguru import logger


# 源文档切片采样上限（控制单次评估的 token 成本）
MAX_SOURCE_CHUNKS = 20
MAX_CHUNK_CHARS = 500
# 页面正文参与评估的最大字符数（生成上限约 4096 token，截断采样足够）
MAX_CONTENT_CHARS = 4000


def build_wiki_source_chunks(page) -> List[str]:
    """收集 Wiki 页面的源文档切片（node 挂载型）。

    从页面挂载节点下的已完成文档取切片，截断控制 token。
    社区挂载型页面（community 型）无文档切片，返回空列表。

    Args:
        page: WikiPage 实例

    Returns:
        list[str]：源文档切片内容（每片截断）
    """
    if not page.node:
        return []
    chunks: List[str] = []
    docs = page.node.documents.filter(is_deleted=False, status='done')[:5]
    for doc in docs:
        for c in doc.chunks.all()[:10]:
            if c.content:
                chunks.append(c.content[:MAX_CHUNK_CHARS])
            if len(chunks) >= MAX_SOURCE_CHUNKS:
                break
        if len(chunks) >= MAX_SOURCE_CHUNKS:
            break
    return chunks


def evaluate_wiki_page(page_id: int, model: Optional[str] = None) -> dict:
    """对单个 Wiki 页面执行忠实度/完整性评估 → 落 WikiPageQualityScore

    流程：
    1. 取页面并校验（node 挂载型 + 已发布 + 有源切片 + 非空正文）
    2. 构建 DeepEval 指标（faithfulness 预置 / completeness 自定义 G-Eval）
    3. 逐指标 measure，update_or_create 落库（同页同维度幂等覆盖）

    Args:
        page_id: WikiPage.id
        model: 评估模型名;None 用 SystemConfig.EVAL_MODEL

    Returns:
        {'ok': bool, 'page_id': int, 'evaluated': [维度...], 'failed': [维度...]}
        skipped 场景返回 {'ok': False, 'skipped': 原因}
    """
    from apps.analytics.models import WikiPageQualityScore
    from apps.analytics.deepeval_metrics import get_deepeval_model
    from apps.wiki.models import WikiPage
    from deepeval.metrics import FaithfulnessMetric, GEval
    from deepeval.test_case import LLMTestCase, SingleTurnParams
    from rag_project.config import AnalyticsConfig

    try:
        page = WikiPage.objects.select_related('node').get(id=page_id)
    except WikiPage.DoesNotExist:
        return {'ok': False, 'page_id': page_id, 'skipped': 'page_not_found'}

    if not page.node:
        return {'ok': False, 'page_id': page_id, 'skipped': 'community_page'}

    source_chunks = build_wiki_source_chunks(page)
    if not source_chunks:
        return {'ok': False, 'page_id': page_id, 'skipped': 'no_source_chunks'}

    content = (page.content or '').strip()
    if not content:
        return {'ok': False, 'page_id': page_id, 'skipped': 'empty_content'}

    deepeval_model = get_deepeval_model(model)
    eval_model_name = model or AnalyticsConfig.eval_model()

    # 正文截断，控制评估成本（忠实度需要逐句核对，正文过长会放大 LLM 调用）
    eval_content = content[:MAX_CONTENT_CHARS]

    # 完整性判断的 input 携带源文档切片，G-Eval 据此核对是否遗漏关键要点
    source_text = '\n\n'.join(source_chunks)
    test_case = LLMTestCase(
        input=f'参考资料：\n{source_text}\n\n请评估下面的 Wiki 页面是否完整覆盖了上述参考资料中的所有关键信息。',
        actual_output=eval_content,
        retrieval_context=source_chunks,
    )
    # DeepEval 4.x HallucinationMetric 需要 test_case.context，faithfulness 同源
    try:
        test_case.context = source_text
    except AttributeError:
        pass

    metrics = [
        ('faithfulness', FaithfulnessMetric(
            threshold=0.5, model=deepeval_model, include_reason=True, async_mode=False,
        )),
        ('completeness', GEval(
            name='WikiCompleteness',
            criteria=('判断 Wiki 页面是否完整覆盖了参考资料中的所有关键要点，'
                      '是否存在遗漏重要信息的情况；参考资料在 INPUT 中提供。'),
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            model=deepeval_model, async_mode=False,
        )),
    ]

    evaluated: List[str] = []
    failed: List[str] = []
    for dim_name, metric in metrics:
        t0 = time.time()
        try:
            metric.measure(test_case)
            score = float(metric.score) if metric.score is not None else 0.0
            reason = str(metric.reason or '')
            WikiPageQualityScore.objects.update_or_create(
                page_id=page_id,
                dimension=dim_name,
                defaults={
                    'score': round(score, 4),
                    'reason': reason,
                    'eval_model': f'deepeval-{eval_model_name}',
                    'eval_latency_ms': int((time.time() - t0) * 1000),
                    'status': 'completed',
                    'error_message': '',
                },
            )
            evaluated.append(dim_name)
        except Exception as e:
            logger.warning(f'[WikiEval] 维度 {dim_name} 评估失败 page_id={page_id}: {e}')
            failed.append(dim_name)
            # 失败也落一条 failed 记录，前端能定位问题页面
            WikiPageQualityScore.objects.update_or_create(
                page_id=page_id,
                dimension=dim_name,
                defaults={
                    'score': 0.0,
                    'reason': '',
                    'status': 'failed',
                    'error_message': str(e)[:500],
                },
            )

    logger.info(f'[WikiEval] page_id={page_id} evaluated={evaluated} failed={failed}')
    return {'ok': not failed, 'page_id': page_id,
            'evaluated': evaluated, 'failed': failed}
