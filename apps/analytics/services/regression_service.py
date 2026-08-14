"""低分回归测试集 - 从生产低分对话沉淀,防止已知 bad case 在迭代中退化

核心流程:
1. siphon_low_score_qa_to_regression_set: 沉淀
   从 MultiDimensionScore 聚合 qa 均分,取低分 top N,按 root_type 分流到
   对应的回归测试集(GoldenDataset.dataset_type='regression_low_score'),
   超出容量上限时按 pass_count 降序 + last_eval_at 升序淘汰。
2. run_regression_evaluation: 全链路评估
   对回归测试集每个问题执行 检索→生成→12 维评估,
   均分 ≥ threshold 视为通过(pass_count += 1),否则重置为 0,
   达到 suggest_remove_passes 时标记建议人工移除(不自动删除)。

设计要点:
- 沉淀来源是 QaRecord(生产低分对话),不是 GoldenQuestion
- 同一 qa 不重复沉淀(source_qa_record_id 查重)
- 评估全链路复用 hybrid_search + generate_answer + evaluate_with_deepeval
- pass_count 仅作辅助提示,最终移除决策由人工 review
"""
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Avg, Max
from django.utils import timezone
from loguru import logger


def _get_or_create_regression_dataset(root_type: str):
    """获取或创建指定 root_type 的低分回归测试集

    每个 root_type 独立一个测试集,与现有 GoldenDataset.root_type 设计一致,
    评估时按 dataset.root_type 限定检索范围,避免跨领域噪音。

    Returns:
        (dataset, created)
    """
    from apps.analytics.models import GoldenDataset

    return GoldenDataset.objects.get_or_create(
        dataset_type='regression_low_score',
        root_type=root_type,
        defaults={
            'name': f'低分回归-{root_type}',
            'description': f'自动沉淀的低分回归测试集({root_type}),'
                           f'连续通过 N 次后建议人工 review 移除',
            'status': 'active',
            'version': 'auto',
        },
    )


def siphon_low_score_qa_to_regression_set(top_n: Optional[int] = None) -> dict:
    """从生产低分对话沉淀到回归测试集

    选取规则:
    1. 从 MultiDimensionScore 聚合每个 qa 的 12 维均分
    2. 排除已沉淀的 qa(GoldenQuestion.source_qa_record_id 已存在)
    3. 按均分升序取 top N(最低分的 N 个)
    4. 按 qa.root_type 分流到对应的回归测试集

    容量控制:每个测试集超出 capacity 时,按 pass_count 降序 +
    last_eval_at 升序淘汰(优先移除已多次通过的旧记录,它们最不需要保留)。

    Args:
        top_n: 本次沉淀取的最低分数量;None 用配置默认值

    Returns:
        {'siphoned': N, 'by_root': {root_type: count}, 'skipped': N}
    """
    from apps.analytics.models import (
        GoldenQuestion, MultiDimensionScore, QaRecord,
    )
    from rag_project.config import AnalyticsConfig

    if top_n is None:
        top_n = AnalyticsConfig.low_score_regression_top_n()
    capacity = AnalyticsConfig.low_score_regression_capacity()

    # 已沉淀的 qa_id 集合(防重复沉淀)
    existing_qa_ids = set(
        GoldenQuestion.objects
        .filter(source_qa_record_id__isnull=False)
        .values_list('source_qa_record_id', flat=True)
    )

    # 聚合每个 qa 的 12 维均分,按升序取 top N(最低分)
    # 只取 status=completed 的维度,排除评估失败的脏数据
    low_score_rows = list(
        MultiDimensionScore.objects
        .filter(status='completed')
        .exclude(qa_record_id__in=existing_qa_ids)
        .values('qa_record_id')
        .annotate(avg_score=Avg('score'))
        .order_by('avg_score')[:top_n]
    )

    if not low_score_rows:
        logger.info('[RegressionSiphon] 无候选低分对话')
        return {'siphoned': 0, 'by_root': {}, 'skipped': 0}

    qa_ids = [r['qa_record_id'] for r in low_score_rows]
    # 取 qa 详情(question + root_type),只查需要的字段
    qas = QaRecord.objects.filter(id__in=qa_ids).values('id', 'question', 'root_type')
    qa_map = {qa['id']: qa for qa in qas}

    # 按 root_type 分组,空 root_type 归入 company_doc 兜底
    by_root = {}
    skipped = 0
    for row in low_score_rows:
        qa = qa_map.get(row['qa_record_id'])
        # qa 已删除或问题为空则跳过(防止沉淀无效 case)
        if not qa or not (qa['question'] or '').strip():
            skipped += 1
            continue
        root_type = qa['root_type'] or 'company_doc'
        by_root.setdefault(root_type, []).append({
            'qa_id': qa['id'],
            'question': qa['question'],
            'avg_score': row['avg_score'],
        })

    total_siphoned = 0
    by_root_count = {}
    for root_type, items in by_root.items():
        ds, _ = _get_or_create_regression_dataset(root_type)
        with transaction.atomic():
            # 当前最大 order,新问题追加在末尾
            max_order = (
                GoldenQuestion.objects.filter(dataset=ds).aggregate(m=Max('order'))['m'] or 0
            )
            questions_to_create = [
                GoldenQuestion(
                    dataset=ds,
                    question=item['question'],
                    source_qa_record_id=item['qa_id'],
                    order=max_order + i + 1,
                )
                for i, item in enumerate(items)
            ]
            if questions_to_create:
                try:
                    GoldenQuestion.objects.bulk_create(questions_to_create)
                except IntegrityError:
                    # 并发场景(批量任务重复调度)下可能被同批其他 worker 抢先沉淀,
                    # 唯一约束已兜底,这里回退为逐条 get_or_create,跳过已存在的来源 QA
                    created_now = 0
                    for gq in questions_to_create:
                        _, c = GoldenQuestion.objects.get_or_create(
                            source_qa_record_id=gq.source_qa_record_id,
                            defaults={
                                'dataset': gq.dataset,
                                'question': gq.question,
                                'order': gq.order,
                            },
                        )
                        if c:
                            created_now += 1
                    total_siphoned += created_now
                    by_root_count[root_type] = by_root_count.get(root_type, 0) + created_now
                else:
                    total_siphoned += len(questions_to_create)
                    by_root_count[root_type] = len(questions_to_create)

            # 更新测试集问题计数(冗余字段,前端列表用)
            ds.question_count = GoldenQuestion.objects.filter(dataset=ds).count()
            ds.save(update_fields=['question_count', 'updated_at'])

        # 容量上限控制(独立于上面的事务,避免长事务持锁)
        _enforce_regression_capacity(ds, capacity)

    logger.info(
        f'[RegressionSiphon] 沉淀 {total_siphoned} 条低分对话,'
        f'by_root={by_root_count}, skipped={skipped}'
    )
    return {'siphoned': total_siphoned, 'by_root': by_root_count, 'skipped': skipped}


def _enforce_regression_capacity(dataset, capacity: int) -> int:
    """容量上限控制:超出时按 pass_count 降序 + last_eval_at 升序淘汰

    淘汰优先级:pass_count 高的先淘汰(已多次通过,最不需要保留) >
    last_eval_at 旧的先淘汰(长期未评估的陈旧记录) > id 小的先淘汰

    Returns:
        实际删除的记录数
    """
    # TODO: 后续可考虑对长期通过的问题做"归档"而非直接删除,
    # 归档后移出活跃回归集但保留记录用于回溯;当前先直接淘汰控制容量
    from apps.analytics.models import GoldenQuestion

    total = GoldenQuestion.objects.filter(dataset=dataset).count()
    if total <= capacity:
        return 0

    to_remove = total - capacity
    # 取待淘汰的 id 列表,再批量删除(避免 ORM 迭代开销)
    stale_ids = list(
        GoldenQuestion.objects
        .filter(dataset=dataset)
        .order_by('-pass_count', 'last_eval_at', 'id')
        .values_list('id', flat=True)[:to_remove]
    )
    deleted, _ = GoldenQuestion.objects.filter(id__in=stale_ids).delete()

    # 同步更新测试集计数
    dataset.question_count = GoldenQuestion.objects.filter(dataset=dataset).count()
    dataset.save(update_fields=['question_count', 'updated_at'])

    logger.info(
        f'[RegressionCapacity] dataset={dataset.id}({dataset.root_type}) '
        f'超容量,淘汰 {deleted} 条(pass_count 降序优先)'
    )
    return deleted


def run_regression_evaluation(
    dataset_id: Optional[int] = None,
    user=None,
    limit: Optional[int] = None,
) -> dict:
    """对低分回归测试集执行全链路评估,更新 pass_count

    全链路:检索(hybrid_search) → 生成(generate_answer) → 12 维评估(evaluate_with_deepeval)
    通过(均分 ≥ threshold): pass_count += 1
    失败(均分 < threshold):  pass_count = 0
    评估异常: 不改动 pass_count(避免临时故障误伤)

    成本提示: 全链路 12 维评估每个问题约 90~180s + LLM 调用费用,
    大测试集(200 条)单次评估成本较高,建议配合 limit 或在低峰期执行。

    Args:
        dataset_id: 指定测试集;None 评估所有 regression_low_score 测试集
        user: 执行检索的用户(权限过滤);None 时调用方需保证有默认用户
        limit: 每个测试集最多评估的问题数;None 评估全部

    Returns:
        {'evaluated': N, 'passed': N, 'failed': N, 'results': [...]}
    """
    from apps.analytics.models import GoldenDataset, GoldenQuestion
    from apps.knowledge.models import DocumentChunk
    from apps.retrieval.hybrid import hybrid_search
    from apps.analytics.services.deepeval_service import evaluate_with_deepeval
    from apps.analytics.services.offline_eval_service import generate_answer
    from rag_project.config import AnalyticsConfig

    threshold = AnalyticsConfig.low_score_regression_pass_threshold()
    suggest_passes = AnalyticsConfig.low_score_regression_suggest_remove_passes()
    eval_model = AnalyticsConfig.eval_model()

    # 选定回归测试集
    qs = GoldenDataset.objects.filter(
        dataset_type='regression_low_score',
        status='active',
    )
    if dataset_id:
        qs = qs.filter(id=dataset_id)
    datasets = list(qs)
    if not datasets:
        return {'evaluated': 0, 'passed': 0, 'failed': 0, 'results': [], 'reason': 'no_dataset'}

    total_evaluated = 0
    total_passed = 0
    total_failed = 0
    results = []

    for ds in datasets:
        questions = GoldenQuestion.objects.filter(dataset=ds).order_by('order')
        if limit:
            questions = questions[:limit]

        for q in questions:
            try:
                # 1. 检索:按测试集 root_type 限定范围,复用线上同款 hybrid_search
                # root_type='all' 时不限领域(跨全域检索),其余按测试集领域过滤
                rt = ds.root_type
                search_result = hybrid_search(
                    query=q.question,
                    user=user,
                    root_types=[rt] if rt and rt != 'all' else None,
                    # 离线评估衡量基线检索质量，跳过个性化加权，保证指标与用户画像无关
                    personalize=False,
                )
                chunks = search_result.get('chunks', [])
                chunk_ids = [c.get('chunk_id') for c in chunks[:5] if c.get('chunk_id')]
                chunk_objs = DocumentChunk.objects.filter(id__in=chunk_ids)
                chunk_map = {c.id: c.content for c in chunk_objs if c.content}
                contexts = [chunk_map[cid][:500] for cid in chunk_ids if cid in chunk_map]

                # 2. 生成:复用离线评估的同款 prompt 与模型,保证评估可比性
                context_str = '\n\n'.join(contexts) if contexts else ''
                answer = generate_answer(q.question, context_str, eval_model)

                # 3. 12 维评估:与生产评估同引擎,结果不落 MultiDimensionScore
                # (回归评估的对象是 GoldenQuestion 不是 QaRecord,落库无意义)
                eval_results = evaluate_with_deepeval(
                    question=q.question,
                    answer=answer,
                    contexts=contexts,
                    model=eval_model,
                )
                avg_score = sum(r['score'] for r in eval_results) / max(len(eval_results), 1)

                # 4. 更新 pass_count:通过 +1,失败重置 0
                now = timezone.now()
                if avg_score >= threshold:
                    q.pass_count = (q.pass_count or 0) + 1
                    status = 'passed'
                    total_passed += 1
                else:
                    q.pass_count = 0
                    status = 'failed'
                    total_failed += 1
                q.last_eval_at = now
                q.save(update_fields=['pass_count', 'last_eval_at'])

                results.append({
                    'question_id': q.id,
                    'source_qa_record_id': q.source_qa_record_id,
                    'avg_score': round(avg_score, 4),
                    'status': status,
                    'pass_count': q.pass_count,
                    'suggest_remove': q.pass_count >= suggest_passes,
                })
                total_evaluated += 1
            except Exception as e:
                # 评估异常不算通过也不算失败,不改动 pass_count,避免临时故障误伤
                logger.warning(f'[RegressionEval] question={q.id} 评估失败: {e}')
                results.append({
                    'question_id': q.id,
                    'source_qa_record_id': q.source_qa_record_id,
                    'error': str(e)[:200],
                })

    logger.info(
        f'[RegressionEval] evaluated={total_evaluated} '
        f'passed={total_passed} failed={total_failed}'
    )
    return {
        'evaluated': total_evaluated,
        'passed': total_passed,
        'failed': total_failed,
        'results': results,
    }
