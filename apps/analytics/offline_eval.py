"""
离线评估 Pipeline - 黄金测试集管理 + 离线检索/回答质量评估

核心功能:
1. GoldenDataset 管理: 创建/导入/导出测试集
2. 离线检索评估: 对测试集中的每个问题执行检索，计算 Recall@K/MRR/NDCG
3. 离线回答评估: 对测试集中的每个问题执行完整 QA，评估回答质量
4. 各阶段增益分析: 量化向量/BM25/RRF/Rerank 各阶段的精度贡献

使用场景:
- 评估检索参数变更前/后的质量对比
- 定期（如每周）评估知识库质量基线
- 模型/Reranker 变更后的回归测试
"""
import csv
import json
import time
import uuid
from typing import List, Dict, Any, Optional, Tuple

from loguru import logger

from django.db import transaction
from django.utils import timezone


# ============================================================================
# 1. 黄金测试集管理
# ============================================================================

def create_golden_dataset(
    name: str,
    root_type: str = 'company_doc',
    description: str = '',
    version: str = 'v1',
    created_by_id: Optional[int] = None,
) -> 'GoldenDataset':
    """创建空的黄金测试集

    Args:
        name: 测试集名称
        root_type: 覆盖的知识库类型
        description: 描述
        version: 版本号
        created_by_id: 创建者用户 ID

    Returns:
        GoldenDataset 实例
    """
    from apps.analytics.models import GoldenDataset

    ds = GoldenDataset.objects.create(
        name=name,
        root_type=root_type,
        description=description,
        version=version,
        created_by_id=created_by_id,
    )
    logger.info(f'[GoldenDataset] Created: {ds.id} {ds.name}')
    return ds


def import_questions_from_json(
    dataset_id: int,
    questions_data: List[Dict[str, Any]],
    created_by_id: Optional[int] = None,
) -> Dict[str, int]:
    """从 JSON 数据批量导入测试问题

    JSON 格式:
    [
        {
            "question": "问题文本",
            "question_type": "factoid",
            "difficulty": "medium",
            "tags": ["HR"],
            "relevant_doc_ids": [1, 2, 3],
            "reference_answer": "参考答案",
            "key_points": ["关键点1", "关键点2"]
        },
        ...
    ]

    Args:
        dataset_id: 目标测试集 ID
        questions_data: 问题数据列表
        created_by_id: 创建者用户 ID

    Returns:
        {'created': N, 'updated': M}
    """
    from apps.analytics.models import GoldenDataset, GoldenQuestion, GoldenRelevantDoc, GoldenReferenceAnswer

    try:
        dataset = GoldenDataset.objects.get(id=dataset_id)
    except GoldenDataset.DoesNotExist:
        raise ValueError(f'Dataset {dataset_id} not found')

    created_count = 0
    updated_count = 0

    with transaction.atomic():
        for idx, q_data in enumerate(questions_data):
            question_text = q_data.get('question', '').strip()
            if not question_text:
                continue

            question_type = q_data.get('question_type', 'factoid')
            difficulty = q_data.get('difficulty', 'medium')
            tags = q_data.get('tags', [])

            # 创建或更新问题
            question, created = GoldenQuestion.objects.update_or_create(
                dataset=dataset,
                question=question_text,
                defaults={
                    'question_type': question_type,
                    'difficulty': difficulty,
                    'tags': tags,
                    'order': idx,
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

            # 相关文档标注
            relevant_doc_ids = q_data.get('relevant_doc_ids', [])
            GoldenRelevantDoc.objects.filter(question=question).delete()
            for doc_id in relevant_doc_ids:
                GoldenRelevantDoc.objects.create(
                    question=question,
                    document_id=doc_id,
                    relevance_level=q_data.get('relevance_level', 'medium'),
                )

            # 参考答案
            reference_answer = q_data.get('reference_answer', '')
            key_points = q_data.get('key_points', [])
            if reference_answer:
                GoldenReferenceAnswer.objects.update_or_create(
                    question=question,
                    defaults={
                        'reference_answer': reference_answer,
                        'key_points': key_points,
                        'created_by_id': created_by_id,
                    },
                )

        # 更新测试集问题计数
        dataset.question_count = GoldenQuestion.objects.filter(dataset=dataset).count()
        dataset.save(update_fields=['question_count', 'updated_at'])

    logger.info(f'[GoldenDataset] Imported {created_count} created, {updated_count} updated into dataset {dataset_id}')
    return {'created': created_count, 'updated': updated_count}


def export_dataset_to_json(dataset_id: int) -> List[Dict[str, Any]]:
    """将黄金测试集导出为 JSON

    Args:
        dataset_id: 测试集 ID

    Returns:
        问题数据列表
    """
    from apps.analytics.models import GoldenQuestion, GoldenReferenceAnswer

    # prefetch 预加载相关文档与参考答案,避免循环内 N+1 查询
    questions = (
        GoldenQuestion.objects
        .filter(dataset_id=dataset_id)
        .order_by('order')
        .prefetch_related('relevant_docs', 'reference_answer')
    )
    result = []
    for q in questions:
        rel_doc_ids = [rd.document_id for rd in q.relevant_docs.all()]
        # 反向 OneToOne:预取后无关联对象时访问会抛 DoesNotExist,需安全兜底
        try:
            reference = q.reference_answer
        except GoldenReferenceAnswer.DoesNotExist:
            reference = None

        item = {
            'id': q.id,
            'question': q.question,
            'question_type': q.question_type,
            'difficulty': q.difficulty,
            'tags': q.tags,
            'relevant_doc_ids': rel_doc_ids,
            'reference_answer': reference.reference_answer if reference else '',
            'key_points': reference.key_points if reference else [],
        }
        result.append(item)

    return result


# ============================================================================
# 2. 离线检索评估
# ============================================================================

def run_retrieval_evaluation(
    dataset_id: int,
    user=None,
    vector_top_k: int = 30,
    bm25_top_k: int = 30,
    rerank_top_k: int = 5,
) -> 'RetrievalQualityReport':
    """对黄金测试集执行离线检索评估

    对每个问题执行 hybrid_search，然后与 GoldenRelevantDoc 中标注的相关文档对比，
    计算 Recall@K、MRR、NDCG 等指标。

    同时分析各阶段增益（纯向量/纯BM25/混合/Rerank）。

    Args:
        dataset_id: 测试集 ID
        user: 执行检索的用户（用于权限过滤）
        vector_top_k: 向量召回数
        bm25_top_k: BM25 召回数
        rerank_top_k: Rerank 后保留数

    Returns:
        RetrievalQualityReport 实例
    """
    from apps.analytics.models import (
        GoldenQuestion, RetrievalQualityReport
    )
    from apps.retrieval.hybrid import hybrid_search

    eval_batch_id = str(uuid.uuid4())[:8]

    # select_related('dataset') 避免 q.dataset.root_type 触发 N+1;
    # prefetch_related('relevant_docs') 避免每题一次 GoldenRelevantDoc 查询
    questions = (
        GoldenQuestion.objects
        .filter(dataset_id=dataset_id)
        .select_related('dataset')
        .prefetch_related('relevant_docs')
    )
    if not questions.exists():
        raise ValueError(f'Dataset {dataset_id} has no questions')

    total = questions.count()
    recall_at_5_list = []
    recall_at_10_list = []
    recall_at_20_list = []
    mrr_list = []
    ndcg_at_5_list = []
    ndcg_at_10_list = []

    # 各阶段增益分析
    vector_hits_at_10 = 0
    bm25_hits_at_10 = 0
    hybrid_hits_at_10 = 0
    rerank_hits_at_10 = 0

    questions_with_hits = 0
    questions_without_hits = 0
    total_latency = 0

    for q in questions:
        # 获取标注的相关文档 ID(使用预取数据,避免 N+1)
        relevant_doc_ids = set(rd.document_id for rd in q.relevant_docs.all())

        if not relevant_doc_ids:
            # 无标注相关文档，跳过该问题的精确率计算
            continue

        t0 = time.time()
        try:
            # 执行完整混合检索
            result = hybrid_search(
                query=q.question,
                user=user,
                root_types=[q.dataset.root_type],
                vector_top_k=vector_top_k,
                bm25_top_k=bm25_top_k,
                rerank_top_k=rerank_top_k,
            )
        except Exception as e:
            logger.warning(f'[RetrievalEval] Search failed for question {q.id}: {e}')
            continue

        latency_ms = int((time.time() - t0) * 1000)
        total_latency += latency_ms

        chunks = result.get('chunks', [])
        raw = result.get('raw', {})

        # 检索命中的文档 ID
        retrieved_doc_ids = []
        for c in chunks:
            doc_id = c.get('document_id')
            if doc_id:
                retrieved_doc_ids.append(doc_id)

        retrieved_doc_ids_set = set(retrieved_doc_ids)

        # --- Recall@K ---
        recall_at_5 = _calc_recall_at_k(relevant_doc_ids, retrieved_doc_ids, k=5)
        recall_at_10 = _calc_recall_at_k(relevant_doc_ids, retrieved_doc_ids, k=10)
        recall_at_20 = _calc_recall_at_k(relevant_doc_ids, retrieved_doc_ids, k=20)
        recall_at_5_list.append(recall_at_5)
        recall_at_10_list.append(recall_at_10)
        recall_at_20_list.append(recall_at_20)

        if recall_at_20 > 0:
            questions_with_hits += 1
        else:
            questions_without_hits += 1

        # --- MRR ---
        mrr = _calc_mrr(relevant_doc_ids, retrieved_doc_ids)
        mrr_list.append(mrr)

        # --- NDCG@K ---
        ndcg_at_5 = _calc_ndcg_at_k(relevant_doc_ids, retrieved_doc_ids, k=5)
        ndcg_at_10 = _calc_ndcg_at_k(relevant_doc_ids, retrieved_doc_ids, k=10)
        ndcg_at_5_list.append(ndcg_at_5)
        ndcg_at_10_list.append(ndcg_at_10)

        # --- 各阶段增益分析 ---
        # 向量阶段
        vector_doc_ids = set(c.get('document_id') for c in raw.get('vector', [])[:10] if c.get('document_id'))
        if vector_doc_ids & relevant_doc_ids:
            vector_hits_at_10 += 1

        # BM25 阶段
        bm25_doc_ids = set(c.get('document_id') for c in raw.get('bm25', [])[:10] if c.get('document_id'))
        if bm25_doc_ids & relevant_doc_ids:
            bm25_hits_at_10 += 1

        # 混合（RRF）阶段
        rrf_doc_ids = set(c.get('document_id') for c in raw.get('rrf', [])[:10] if c.get('document_id'))
        if rrf_doc_ids & relevant_doc_ids:
            hybrid_hits_at_10 += 1

        # Rerank 后
        final_top10_doc_ids = set(retrieved_doc_ids[:10])
        if final_top10_doc_ids & relevant_doc_ids:
            rerank_hits_at_10 += 1

    # 计算平均指标
    report = RetrievalQualityReport.objects.create(
        dataset_id=dataset_id,
        eval_batch_id=eval_batch_id,
        recall_at_5=_avg(recall_at_5_list),
        recall_at_10=_avg(recall_at_10_list),
        recall_at_20=_avg(recall_at_20_list),
        mrr=_avg(mrr_list),
        ndcg_at_5=_avg(ndcg_at_5_list),
        ndcg_at_10=_avg(ndcg_at_10_list),
        vector_recall_at_10=vector_hits_at_10 / max(total, 1),
        bm25_recall_at_10=bm25_hits_at_10 / max(total, 1),
        hybrid_recall_at_10=hybrid_hits_at_10 / max(total, 1),
        rerank_recall_at_10=rerank_hits_at_10 / max(total, 1),
        total_questions=total,
        questions_with_hits=questions_with_hits,
        questions_without_hits=questions_without_hits,
        avg_latency_ms=total_latency // max(total, 1) if total > 0 else 0,
        config_snapshot={
            'vector_top_k': vector_top_k,
            'bm25_top_k': bm25_top_k,
            'rerank_top_k': rerank_top_k,
        },
        status='completed',
    )

    logger.info(
        f'[RetrievalEval] Completed for dataset {dataset_id}: '
        f'Recall@5={report.recall_at_5:.3f} Recall@10={report.recall_at_10:.3f} '
        f'MRR={report.mrr:.3f} NDCG@10={report.ndcg_at_10:.3f}'
    )
    return report


def _calc_recall_at_k(relevant_ids: set, retrieved_ids: List[int], k: int) -> float:
    """计算 Recall@K = 前 K 个结果中相关文档数 / 相关文档总数"""
    if not relevant_ids:
        return 0.0
    top_k = set(retrieved_ids[:k])
    hits = len(top_k & relevant_ids)
    return hits / len(relevant_ids)


def _calc_mrr(relevant_ids: set, retrieved_ids: List[int]) -> float:
    """计算 MRR (Mean Reciprocal Rank)

    MRR = 1 / rank_of_first_relevant
    若无相关文档命中则为 0
    """
    for rank, doc_id in enumerate(retrieved_ids, 1):
        if doc_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def _calc_ndcg_at_k(relevant_ids: set, retrieved_ids: List[int], k: int) -> float:
    """计算 NDCG@K (简化版，假设所有相关文档的相关度相同)"""
    if not relevant_ids:
        return 0.0

    # DCG
    dcg = 0.0
    for i, doc_id in enumerate(retrieved_ids[:k]):
        if doc_id in relevant_ids:
            rel = 1.0  # 命中
        else:
            rel = 0.0
        # 使用 DCG 公式: sum(2^rel - 1) / log2(i+1)
        dcg += (2 ** rel - 1) / max(1, __import__('math').log2(i + 2))

    # IDCG: 理想排序下的 DCG
    ideal_relevant_count = min(k, len(relevant_ids))
    idcg = 0.0
    for i in range(ideal_relevant_count):
        idcg += 1.0 / max(1, __import__('math').log2(i + 2))

    if idcg == 0:
        return 0.0
    return dcg / idcg


def _avg(values: List[float]) -> float:
    """计算列表平均值（安全处理空列表）"""
    if not values:
        return 0.0
    return sum(values) / len(values)


# ============================================================================
# 3. 离线回答质量评估
# ============================================================================

def run_answer_quality_evaluation(
    dataset_id: int,
    user=None,
    model: str = 'deepseek-chat',
    max_questions: int = 50,
) -> List[Dict[str, Any]]:
    """对黄金测试集执行离线回答质量评估

    对每个问题执行完整 QA（检索 + 回答生成），然后用 DeepEval 12 维指标评估回答质量。
    结果不落库（离线评估无 QaRecord），仅返回给前端展示；
    生产对话评估由 production_eval.py 落 MultiDimensionScore 表。

    Args:
        dataset_id: 测试集 ID
        user: 执行检索的用户
        model: 评估模型
        max_questions: 最多评估的问题数（限制成本）

    Returns:
        每个问题的评估结果汇总
    """
    from apps.analytics.models import GoldenQuestion, GoldenReferenceAnswer
    from apps.knowledge.models import DocumentChunk
    from apps.retrieval.hybrid import hybrid_search
    from apps.llm.factory import get_llm
    from apps.analytics.deepeval_metrics import evaluate_with_deepeval

    eval_batch_id = str(uuid.uuid4())[:8]

    questions = GoldenQuestion.objects.filter(dataset_id=dataset_id)[:max_questions]
    results = []

    for q in questions:
        try:
            search_result = hybrid_search(
                query=q.question,
                user=user,
                root_types=[q.dataset.root_type],
            )
            chunks = search_result.get('chunks', [])

            # DeepEval retrieval_context 需要 list[str]
            chunk_ids = [c.get('chunk_id') for c in chunks[:5] if c.get('chunk_id')]
            chunk_objs = DocumentChunk.objects.filter(id__in=chunk_ids)
            chunk_map = {c.id: c.content for c in chunk_objs if c.content}
            contexts = [chunk_map[cid][:500] for cid in chunk_ids if cid in chunk_map]

            context_str = '\n\n'.join(contexts) if contexts else ''
            answer = _generate_answer(q.question, context_str, model)

            # DeepEval 12 维评估(无 reference 也能算,reference 仅用于人工对照)
            eval_results = evaluate_with_deepeval(
                question=q.question,
                answer=answer,
                contexts=contexts,
                model=model,
            )

            # 计算该问题的平均分
            avg_score = sum(r['score'] for r in eval_results) / max(len(eval_results), 1)

            results.append({
                'question_id': q.id,
                'question': q.question[:100],
                'answer': answer[:200],
                'avg_score': round(avg_score, 3),
                'dimension_scores': {r['dimension']: r['score'] for r in eval_results},
                'eval_batch_id': eval_batch_id,
            })

        except Exception as e:
            logger.warning(f'[AnswerEval] Failed for question {q.id}: {e}')
            results.append({
                'question_id': q.id,
                'question': q.question[:100],
                'error': str(e)[:200],
            })

    # 汇总
    if results:
        scored = [r for r in results if 'avg_score' in r]
        if scored:
            total_avg = sum(r['avg_score'] for r in scored) / len(scored)
            logger.info(
                f'[AnswerEval] Completed: {len(results)} questions, '
                f'avg_score={total_avg:.3f}, batch={eval_batch_id}'
            )

    return results


def _generate_answer(question: str, context: str, model: str) -> str:
    """使用指定模型生成回答（用于离线评估）

    Args:
        question: 用户问题
        context: 检索上下文
        model: 使用的模型

    Returns:
        生成的回答文本
    """
    llm = get_llm(model=model)

    system_prompt = (
        '你是一名知识库问答助手。请严格基于提供的上下文回答用户问题。'
        '如果上下文中没有相关信息，请明确说明"根据现有资料无法回答该问题"。'
    )

    user_prompt = f'问题：{question}\n\n参考资料：\n{context}\n\n请给出回答：'

    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_prompt},
    ]

    try:
        response = llm.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=1000,
            temperature=0.3,
        )
        return response.choices[0].message.content or ''
    except Exception as e:
        logger.warning(f'[AnswerEval] Answer generation failed: {e}')
        return f'[回答生成失败] {str(e)[:100]}'
