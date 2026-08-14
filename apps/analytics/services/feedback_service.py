"""
检索反馈闭环自动化 - 用户点击/反馈数据驱动关键词权重自动调整

替代管理员手动 KeywordWeight 加权：
1. 每日聚合：统计关键词命中 chunk 的 展示数/点击数/采纳数/负反馈数
2. 权重规则：采纳率低 → 降权；点击未采纳 → 半降权；负反馈 → 降权
3. 保护机制：单日调整幅度上限 + 最小样本数 + 人工复核开关（AUTO_APPLY=False 只记录不应用）
4. 全程审计：KeywordFeedbackAgg 记录调整前后权重与原因，幂等键 (report_date, keyword, root_type)

关键设计：
- 聚合口径：展示=QaRecord.retrieval_hits；采纳=citations.chunk_ids ∩ 展示；
  点击=ChunkClickLog 按 qa_record 归并 ∩ 展示；负反馈=QaFeedback.rating<0
- 幂等：同一日期同一关键词 get_or_create 命中后只刷新统计值，
  不重复应用权重，防止 Beat 重入或失败重跑导致权重被二次调整
- 手动覆盖优先：若同日已存在 adjust_type=manual 记录（管理员已接管），自动任务不再应用权重
"""
import time
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from loguru import logger

from apps.analytics.models import KeywordWeight, KeywordFeedbackAgg
from rag_project.config import AnalyticsConfig


def _parse_report_date(report_date):
    """解析聚合日期：接受 date/str(YYYY-MM-DD)，默认昨天（本地业务日期）

    与 SystemMetrics 等日报任务保持一致：凌晨执行时用本地日期而非 UTC 日期，
    避免 __date 查询在凌晨时段错天。
    """
    if report_date is None:
        return timezone.localdate() - timedelta(days=1)
    if hasattr(report_date, 'year'):
        return report_date
    try:
        return timezone.datetime.strptime(str(report_date), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        raise ValueError(f'非法日期参数 {report_date}，应为 YYYY-MM-DD')


def _collect_day_stats(report_date):
    """聚合指定日期内 QaRecord 的 关键词 → 展示/点击/采纳/负反馈 统计

    返回 {(keyword, root_type): {shown_count, click_count, adopt_count, bad_count}}。

    口径说明：
    - 只处理 retrieval_hits 非空的 QA（无展示则对该关键词无贡献）
    - 关键词取自问题分词（与 BM25 检索分词一致，保证闭环口径统一）
    - 采纳 = citations 中的 chunk_ids 落在展示集合内（历史数据兼容单数 chunk_id）
    - 点击 = ChunkClickLog 按 qa_record 归并后与展示集合的交集
    """
    from apps.chat.models import QaRecord, QaFeedback
    from apps.retrieval.bm25 import tokenize
    from apps.analytics.models import ChunkClickLog

    # 负反馈映射：qa_record_id → rating（差评即 rating<0）
    feedback_map = dict(
        QaFeedback.objects
        .filter(qa_record__created_at__date=report_date)
        .values_list('qa_record_id', 'rating')
    )
    # 点击映射：qa_record_id → set(chunk_id)；无 qa_record 关联的点击不参与聚合
    click_map = defaultdict(set)
    for qa_id, chunk_id in (
        ChunkClickLog.objects
        .filter(created_at__date=report_date, qa_record__isnull=False)
        .values_list('qa_record_id', 'chunk_id')
    ):
        click_map[qa_id].add(chunk_id)

    stats = defaultdict(lambda: {
        'shown_count': 0, 'click_count': 0, 'adopt_count': 0, 'bad_count': 0,
    })

    qas = (QaRecord.objects
           .filter(created_at__date=report_date)
           .exclude(retrieval_hits=[])
           .values('id', 'question', 'root_type', 'retrieval_hits', 'citations'))
    for qa in qas:
        shown = set(qa['retrieval_hits'])
        if not shown:
            continue
        adopted = set()
        for c in (qa['citations'] or []):
            for cid in (c.get('chunk_ids') or []):
                if cid in shown:
                    adopted.add(cid)
            # 兼容历史 citations 仅有单数 chunk_id 字段的数据
            cid = c.get('chunk_id')
            if cid and cid in shown:
                adopted.add(cid)
        clicked = click_map.get(qa['id'], set()) & shown
        bad = 1 if feedback_map.get(qa['id'], 0) < 0 else 0
        root_type = qa['root_type'] or 'all'
        for kw in set(tokenize(qa['question'])):
            s = stats[(kw, root_type)]
            s['shown_count'] += len(shown)
            s['adopt_count'] += len(adopted)
            s['click_count'] += len(clicked)
            s['bad_count'] += bad
    return stats


def _compute_delta(stats_row, cfg):
    """按规则计算单关键词当日调整量（受单日幅度上限保护）

    规则（与 plan 对齐）：
    - 采纳率低于阈值 → 基础降权
    - 点击未采纳（点击率高于采纳率）→ 半降权
    - 负反馈达到阈值 → 基础降权
    - 幅度保护：|delta| <= max_delta；样本不足（shown < min_show）返回 0

    Args:
        stats_row: 单关键词聚合统计 dict
        cfg: {adopt_threshold, bad_threshold, min_show_count, base_delta, max_delta}

    Returns:
        (delta, reason_list)
    """
    shown = stats_row['shown_count']
    if shown < cfg['min_show_count']:
        return 0.0, []

    adopt_rate = stats_row['adopt_count'] / shown
    click_rate = stats_row['click_count'] / shown
    delta = 0.0
    reasons = []
    if adopt_rate < cfg['adopt_threshold']:
        delta -= cfg['base_delta']
        reasons.append('采纳率低')
    if click_rate > 0 and click_rate > adopt_rate:
        delta -= cfg['base_delta'] / 2
        reasons.append('点击未采纳')
    if stats_row['bad_count'] >= cfg['bad_threshold']:
        delta -= cfg['base_delta']
        reasons.append('负反馈')
    # 保护机制：无论命中多少条规则，单日调整幅度不超过上限
    delta = max(delta, -cfg['max_delta'])
    return delta, reasons


def run_keyword_feedback_loop(report_date=None):
    """执行检索反馈闭环每日聚合 + 权重调整

    Args:
        report_date: 聚合日期（date 或 YYYY-MM-DD 字符串）；None 用昨天

    Returns:
        {'ok': bool, 'report_date': str, 'total': int, 'applied': int,
         'pending': int, 'skipped': int}
    """
    from django.utils import timezone as tz

    date = _parse_report_date(report_date)
    cfg = {
        'adopt_threshold': AnalyticsConfig.feedback_loop_adopt_threshold(),
        'bad_threshold': AnalyticsConfig.feedback_loop_bad_threshold(),
        'min_show_count': AnalyticsConfig.feedback_loop_min_show_count(),
        'base_delta': AnalyticsConfig.feedback_loop_base_delta(),
        'max_delta': AnalyticsConfig.feedback_loop_max_delta(),
    }
    auto_apply = AnalyticsConfig.feedback_loop_auto_apply()

    t0 = time.time()
    stats = _collect_day_stats(date)
    logger.info(f'[FeedbackLoop] {date} 聚合到 {len(stats)} 个关键词')

    total = applied = pending = skipped = 0
    now = tz.now()
    for (keyword, root_type), row in stats.items():
        delta, reasons = _compute_delta(row, cfg)
        if delta == 0:
            skipped += 1
            continue
        total += 1

        # 聚合记录创建与权重应用必须原子:auto_apply 时先落聚合再改权重,
        # 若中途失败会留下"已聚合未应用"的半截状态,重跑时 created=False
        # 会走 _refresh_stats_only 跳过应用,权重永远不生效
        with transaction.atomic():
            # 读当前权重（存在则用现值，否则按默认 1.0 初始化，get_or_create 幂等）
            kw, _ = KeywordWeight.objects.get_or_create(
                keyword=keyword, root_type=root_type,
                defaults={'weight_score': 1.0},
            )
            old_score = kw.weight_score
            # 与手动调整一致的钳位区间 0.1~5.0（模型层已约束 default 1.0）
            new_score = max(0.1, min(5.0, old_score + delta))
            actual_delta = round(new_score - old_score, 4)

            agg, created = KeywordFeedbackAgg.objects.get_or_create(
                report_date=date, keyword=keyword, root_type=root_type,
                defaults={
                    'shown_count': row['shown_count'],
                    'click_count': row['click_count'],
                    'adopt_count': row['adopt_count'],
                    'bad_count': row['bad_count'],
                    'click_rate': round(row['click_count'] / row['shown_count'], 4),
                    'adopt_rate': round(row['adopt_count'] / row['shown_count'], 4),
                    'old_score': old_score,
                    'new_score': new_score,
                    'delta': actual_delta,
                    'reason': ';'.join(reasons),
                    'adjust_type': 'auto',
                    'status': 'applied' if auto_apply else 'pending',
                    'applied_at': now if auto_apply else None,
                },
            )

            if not created:
                # 幂等：已处理过的日期只刷新统计值，不重复应用权重；
                # 若同日已被人工接管（manual），同样跳过应用，保证手动覆盖优先
                _refresh_stats_only(agg, row)
                skipped += 1
                continue

            if auto_apply:
                # 权重应用 + 累计计数（hit/bad）保持 KeywordWeight 历史统计可用
                kw.weight_score = new_score
                kw.hit_count += row['shown_count']
                kw.bad_feedback += row['bad_count']
                kw.save(update_fields=['weight_score', 'hit_count', 'bad_feedback'])
                applied += 1
                logger.info(
                    f'[FeedbackLoop] auto {keyword}({root_type}) {old_score:+.2f}→{new_score:+.2f} '
                    f'delta={actual_delta:+.2f} reasons={reasons}'
                )
            else:
                pending += 1
                logger.info(
                    f'[FeedbackLoop] pending {keyword}({root_type}) 建议 delta={actual_delta:+.2f} '
                    f'reasons={reasons}（等待人工复核）'
                )

    logger.info(
        f'[FeedbackLoop] {date} done: total={total} applied={applied} '
        f'pending={pending} skipped={skipped} '
        f'latency={int((time.time() - t0) * 1000)}ms'
    )
    return {
        'ok': True, 'report_date': str(date), 'total': total,
        'applied': applied, 'pending': pending, 'skipped': skipped,
    }


def _refresh_stats_only(agg, row):
    """幂等重跑时仅刷新聚合统计值，不触碰已应用的权重调整记录"""
    shown = row['shown_count']
    agg.shown_count = row['shown_count']
    agg.click_count = row['click_count']
    agg.adopt_count = row['adopt_count']
    agg.bad_count = row['bad_count']
    agg.click_rate = round(row['click_count'] / shown, 4) if shown else 0.0
    agg.adopt_rate = round(row['adopt_count'] / shown, 4) if shown else 0.0
    agg.save(update_fields=[
        'shown_count', 'click_count', 'adopt_count', 'bad_count',
        'click_rate', 'adopt_rate',
    ])


def apply_pending_adjustment(agg_id, action='apply', user=None):
    """人工复核：应用/忽略一条待复核(pending)的聚合调整

    仅允许处理 pending 状态记录；应用时把目标权重写入 KeywordWeight，
    并更新记录状态与生效时间。手动覆盖优先：若权重已被人工改过，仍以
    当前权重为基础按 delta 叠加（保持增量语义）。

    Args:
        agg_id: KeywordFeedbackAgg.id
        action: 'apply' 应用 / 'ignore' 忽略
        user: 操作人 User（审计留痕）

    Returns:
        (ok, message)
    """
    from django.db import transaction
    from django.utils import timezone as tz

    # 事务 + 行锁：并发管理员同时对同一 pending 记录点击"应用"时，
    # 若不做互斥，两人都会读到 pending 状态并基于同一 old_score 叠加 delta，
    # 造成权重被重复调整且无法被幂等键拦截
    with transaction.atomic():
        try:
            agg = KeywordFeedbackAgg.objects.select_for_update().get(id=agg_id)
        except KeywordFeedbackAgg.DoesNotExist:
            return False, '聚合记录不存在'
        if agg.status != 'pending':
            return False, f'该记录当前状态为 {agg.get_status_display()}，不可操作'

        if action == 'ignore':
            agg.status = 'ignored'
            agg.save(update_fields=['status', 'updated_at'])
            logger.info(f'[FeedbackLoop] ignore agg_id={agg.id} keyword={agg.keyword}')
            return True, '已忽略该调整'

        # apply：以当前权重为基准叠加 delta（兼容应用前权重被手动改动的场景）
        kw, _ = KeywordWeight.objects.select_for_update().get_or_create(
            keyword=agg.keyword, root_type=agg.root_type,
            defaults={'weight_score': 1.0},
        )
        old_score = kw.weight_score
        new_score = max(0.1, min(5.0, old_score + agg.delta))
        kw.weight_score = new_score
        kw.save(update_fields=['weight_score'])

        agg.old_score = old_score
        agg.new_score = new_score
        agg.status = 'applied'
        agg.applied_at = tz.now()
        agg.actor = user
        agg.save(update_fields=['old_score', 'new_score', 'status', 'applied_at', 'actor', 'updated_at'])
        logger.info(
            f'[FeedbackLoop] apply agg_id={agg.id} keyword={agg.keyword} '
            f'{old_score:+.2f}→{new_score:+.2f} actor={getattr(user, "username", None)}'
        )
        return True, f'已应用调整 {agg.delta:+.2f}'


def record_manual_adjustment(kw, old_score, user):
    """记录一次手动权重调整（运营在关键词列表中 +/- 按钮），用于统一审计展示

    以当天为 report_date 写入 KeywordFeedbackAgg（update_or_create，同日多次
    手动调整保留最新一次）。自动任务处理的是昨天，正常不会与该记录冲突；
    若冲突（同日），自动任务按 created=False 跳过应用，即手动覆盖优先。

    Args:
        kw: 已更新后的 KeywordWeight 实例
        old_score: 调整前权重
        user: 操作人
    """
    from django.utils import timezone as tz

    today = tz.localdate()
    agg, created = KeywordFeedbackAgg.objects.update_or_create(
        report_date=today, keyword=kw.keyword, root_type=kw.root_type,
        defaults={
            'old_score': old_score,
            'new_score': kw.weight_score,
            'delta': round(kw.weight_score - old_score, 4),
            'reason': '人工调整',
            'adjust_type': 'manual',
            'status': 'applied',
            'actor': user,
            'applied_at': tz.now(),
        },
    )
    logger.info(
        f'[FeedbackLoop] manual {kw.keyword}({kw.root_type}) '
        f'{old_score:+.2f}→{kw.weight_score:+.2f} agg_id={agg.id} '
        f'created={created} user={getattr(user, "username", None)}'
    )
    return agg
