"""
检索反馈闭环自动化测试

覆盖 plan.md【4】P0 · 检索反馈闭环自动化的验收标准：
- 连续模拟点击/反馈后关键词权重自动变化且幅度受控（max_delta 钳位）
- 手动覆盖优先于自动（同日 manual 记录存在时自动任务跳过应用）
- 审计可追溯（KeywordFeedbackAgg 记录 old/new/delta/reason/status/applied_at）
- 每日任务幂等（重复执行不重复调整权重）
- 无反馈关键词默认排序不受影响（样本不足 / 无降权规则时跳过）

实现说明：
- 聚合口径依赖 jieba 分词，测试统一 patch apps.retrieval.bm25.tokenize，
  让所有问题都稳定切出目标关键词 KEYWORD，避免依赖 jieba 词典的解析结果
- 数据构造复用 test_views.py 的 _create_test_user / _get_auth_token 模式
"""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.analytics.services.feedback_service import (
    _compute_delta,
    _parse_report_date,
    apply_pending_adjustment,
    record_manual_adjustment,
    run_keyword_feedback_loop,
)
from apps.analytics.models import ChunkClickLog, KeywordFeedbackAgg, KeywordWeight
from apps.chat.models import QaFeedback, QaRecord
from apps.memory.models import Session
from rag_project.config import AnalyticsConfig

# 复用 test_views 的用户 / 认证辅助，保持测试数据构造一致
from apps.analytics.tests.test_views import _create_test_user, _get_auth_token

KEYWORD = '工资'


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------
def _backdate(instance, day):
    """绕过 auto_now_add 回填 created_at 到指定业务日期"""
    dt = timezone.make_aware(datetime.combine(day, time(12, 0)))
    type(instance).objects.filter(pk=instance.pk).update(created_at=dt)


def _make_qa(fb_session, fb_user, day, hits, citations=None, rating=0,
             root_type='test_root'):
    """创建一条 QaRecord（含检索命中/引用），并按需关联负反馈"""
    qa = QaRecord.objects.create(
        session=fb_session, user=fb_user,
        question=f'{KEYWORD}相关问题', answer='测试回答',
        answer_type='rag', root_type=root_type,
        retrieval_hits=hits,
        citations=citations if citations is not None else [],
        is_hit_cache=False, is_success=True, error_type='',
        tokens_prompt=100, tokens_completion=50,
        cost_estimate=Decimal('0.010000'),
        latency_total_ms=500, latency_llm_ms=300,
        latency_retrieval_ms=100, latency_ttfb_ms=200,
        tokens_per_second=10.0,
    )
    _backdate(qa, day)
    if rating:
        QaFeedback.objects.create(qa_record=qa, user=fb_user, rating=rating)
    return qa


def _make_click(fb_user, qa, chunk_id, day):
    """创建一条点击日志并回填到指定日期"""
    log = ChunkClickLog.objects.create(
        user=fb_user, qa_record=qa, chunk_id=chunk_id, root_type=qa.root_type)
    _backdate(log, day)
    return log


@pytest.fixture
def fb_user():
    return _create_test_user(username='fb_user', password='pass12345')


@pytest.fixture
def fb_session(fb_user):
    return Session.objects.create(
        user=fb_user, root_type='test_root', title='反馈闭环测试会话')


@pytest.fixture
def yesterday():
    return timezone.localdate() - timedelta(days=1)


# ---------------------------------------------------------------------------
# 纯逻辑单元测试：日期解析 / 调整规则（无 DB）
# ---------------------------------------------------------------------------
class TestParseReportDate:
    """_parse_report_date 日期解析"""

    def test_default_returns_yesterday(self):
        assert _parse_report_date(None) == timezone.localdate() - timedelta(days=1)

    def test_accepts_date_object(self):
        d = date(2026, 8, 1)
        assert _parse_report_date(d) == d

    def test_accepts_string(self):
        assert _parse_report_date('2026-08-01') == date(2026, 8, 1)

    def test_invalid_string_raises_valueerror(self):
        with pytest.raises(ValueError):
            _parse_report_date('bad-date')


class TestComputeDelta:
    """_compute_delta 权重调整规则（含幅度保护）"""

    def _cfg(self):
        return dict(adopt_threshold=0.3, bad_threshold=2, min_show_count=5,
                    base_delta=0.1, max_delta=0.2)

    def test_when_shown_below_min_then_zero(self):
        delta, reasons = _compute_delta(
            {'shown_count': 4, 'adopt_count': 0, 'click_count': 0, 'bad_count': 0},
            self._cfg())
        assert delta == 0.0
        assert reasons == []

    def test_when_low_adoption_then_base_delta(self):
        delta, reasons = _compute_delta(
            {'shown_count': 10, 'adopt_count': 1, 'click_count': 0, 'bad_count': 0},
            self._cfg())
        assert delta == pytest.approx(-0.1)
        assert '采纳率低' in reasons

    def test_when_click_not_adopted_then_extra_half_delta(self):
        delta, reasons = _compute_delta(
            {'shown_count': 10, 'adopt_count': 1, 'click_count': 5, 'bad_count': 0},
            self._cfg())
        assert delta == pytest.approx(-0.15)
        assert '点击未采纳' in reasons

    def test_when_all_rules_then_clamped_to_max_delta(self):
        delta, reasons = _compute_delta(
            {'shown_count': 10, 'adopt_count': 0, 'click_count': 5, 'bad_count': 2},
            self._cfg())
        # 原始 -0.25 被单日上限钳制到 -0.2
        assert delta == pytest.approx(-0.2)
        assert len(reasons) == 3


# ---------------------------------------------------------------------------
# 聚合任务主流程（DB）
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestFeedbackLoopRun:
    """run_keyword_feedback_loop 每日聚合 + 权重应用"""

    @pytest.fixture(autouse=True)
    def _tokenize_patch(self):
        # 稳定分词：所有问题都切出 KEYWORD，保证聚合口径确定
        with patch('apps.retrieval.bm25.tokenize', return_value=[KEYWORD]):
            yield

    def test_when_low_adoption_then_weight_decreases_and_audited(
            self, fb_user, fb_session, yesterday):
        for _ in range(10):
            _make_qa(fb_session, fb_user, yesterday, hits=[1, 2, 3])

        result = run_keyword_feedback_loop(report_date=yesterday)
        assert result['ok'] is True
        assert result['applied'] == 1

        kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
        assert kw.weight_score == pytest.approx(0.9)
        assert kw.hit_count == 30

        # 审计可追溯：记录调整前后权重、原因与生效时间
        agg = KeywordFeedbackAgg.objects.get(report_date=yesterday, keyword=KEYWORD)
        assert agg.adjust_type == 'auto'
        assert agg.status == 'applied'
        assert agg.old_score == pytest.approx(1.0)
        assert agg.new_score == pytest.approx(0.9)
        assert agg.delta == pytest.approx(-0.1)
        assert '采纳率低' in agg.reason
        assert agg.applied_at is not None

    def test_when_clicks_not_adopted_then_extra_half_delta(
            self, fb_user, fb_session, yesterday):
        for _ in range(10):
            qa = _make_qa(fb_session, fb_user, yesterday, hits=[1, 2, 3])
            _make_click(fb_user, qa, 1, yesterday)

        run_keyword_feedback_loop(report_date=yesterday)
        kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
        assert kw.weight_score == pytest.approx(0.85)
        agg = KeywordFeedbackAgg.objects.get(report_date=yesterday, keyword=KEYWORD)
        assert agg.delta == pytest.approx(-0.15)
        assert '点击未采纳' in agg.reason

    def test_when_all_rules_then_delta_clamped_to_max_delta(
            self, fb_user, fb_session, yesterday):
        qas = []
        for i in range(10):
            rating = -1 if i < 2 else 0
            qas.append(_make_qa(fb_session, fb_user, yesterday,
                                hits=[1, 2, 3], rating=rating))
        for qa in qas:
            _make_click(fb_user, qa, 1, yesterday)

        result = run_keyword_feedback_loop(report_date=yesterday)
        assert result['applied'] == 1
        kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
        # 三条规则原始 -0.25，被单日上限钳制到 -0.2
        assert kw.weight_score == pytest.approx(0.8)
        agg = KeywordFeedbackAgg.objects.get(report_date=yesterday, keyword=KEYWORD)
        assert agg.delta == pytest.approx(-0.2)

    def test_when_min_show_not_met_then_no_change(
            self, fb_user, fb_session, yesterday):
        _make_qa(fb_session, fb_user, yesterday, hits=[1])

        result = run_keyword_feedback_loop(report_date=yesterday)
        assert result['total'] == 0
        assert not KeywordWeight.objects.filter(keyword=KEYWORD).exists()
        assert not KeywordFeedbackAgg.objects.filter(
            report_date=yesterday, keyword=KEYWORD).exists()

    def test_idempotent_when_run_twice(self, fb_user, fb_session, yesterday):
        for _ in range(10):
            _make_qa(fb_session, fb_user, yesterday, hits=[1, 2, 3])

        first = run_keyword_feedback_loop(report_date=yesterday)
        second = run_keyword_feedback_loop(report_date=yesterday)
        assert first['applied'] == 1
        assert second['applied'] == 0
        # 权重只被调整一次，且只保留一条聚合记录
        kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
        assert kw.weight_score == pytest.approx(0.9)
        assert KeywordFeedbackAgg.objects.filter(
            report_date=yesterday, keyword=KEYWORD).count() == 1

    def test_manual_record_wins_over_auto(self, fb_user, fb_session, yesterday):
        for _ in range(10):
            _make_qa(fb_session, fb_user, yesterday, hits=[1, 2, 3])
        # 预置同日 manual 记录，模拟管理员已接管该关键词
        KeywordFeedbackAgg.objects.create(
            report_date=yesterday, keyword=KEYWORD, root_type='test_root',
            adjust_type='manual', status='applied',
            old_score=1.0, new_score=1.5, delta=0.5, reason='人工调整',
        )

        result = run_keyword_feedback_loop(report_date=yesterday)
        assert result['applied'] == 0
        # 自动任务未应用权重（权重保持 get_or_create 的默认 1.0）
        kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
        assert kw.weight_score == pytest.approx(1.0)
        agg = KeywordFeedbackAgg.objects.get(report_date=yesterday, keyword=KEYWORD)
        assert agg.adjust_type == 'manual'
        assert agg.new_score == pytest.approx(1.5)

    def test_auto_apply_disabled_then_pending(self, fb_user, fb_session, yesterday):
        for _ in range(10):
            _make_qa(fb_session, fb_user, yesterday, hits=[1, 2, 3])

        with patch.object(AnalyticsConfig, 'feedback_loop_auto_apply',
                          return_value=False):
            result = run_keyword_feedback_loop(report_date=yesterday)
        assert result['applied'] == 0
        assert result['pending'] == 1
        agg = KeywordFeedbackAgg.objects.get(report_date=yesterday, keyword=KEYWORD)
        assert agg.status == 'pending'
        assert agg.applied_at is None
        # 权重未被自动修改
        kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
        assert kw.weight_score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 人工复核
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestPendingAdjustment:
    """apply_pending_adjustment 应用 / 忽略 pending 记录"""

    def _make_pending(self, fb_user, fb_session, yesterday):
        for _ in range(10):
            _make_qa(fb_session, fb_user, yesterday, hits=[1, 2, 3])
        with patch('apps.retrieval.bm25.tokenize', return_value=[KEYWORD]), \
             patch.object(AnalyticsConfig, 'feedback_loop_auto_apply',
                          return_value=False):
            run_keyword_feedback_loop(report_date=yesterday)
        return KeywordFeedbackAgg.objects.get(report_date=yesterday, keyword=KEYWORD)

    def test_apply_applies_weight(self, fb_user, fb_session, yesterday):
        agg = self._make_pending(fb_user, fb_session, yesterday)
        ok, _ = apply_pending_adjustment(agg.id, action='apply', user=fb_user)
        assert ok is True
        agg.refresh_from_db()
        assert agg.status == 'applied'
        assert agg.actor_id == fb_user.id
        kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
        assert kw.weight_score == pytest.approx(agg.new_score)

    def test_ignore_marks_ignored(self, fb_user, fb_session, yesterday):
        agg = self._make_pending(fb_user, fb_session, yesterday)
        ok, _ = apply_pending_adjustment(agg.id, action='ignore', user=fb_user)
        assert ok is True
        agg.refresh_from_db()
        assert agg.status == 'ignored'

    def test_apply_when_not_pending_then_error(self, fb_user, fb_session, yesterday):
        agg = self._make_pending(fb_user, fb_session, yesterday)
        apply_pending_adjustment(agg.id, action='apply', user=fb_user)
        ok, message = apply_pending_adjustment(agg.id, action='apply', user=fb_user)
        assert ok is False
        assert '不可操作' in message

    def test_apply_unknown_id_then_error(self, fb_user):
        ok, message = apply_pending_adjustment(999999, action='apply', user=fb_user)
        assert ok is False
        assert '不存在' in message


# ---------------------------------------------------------------------------
# 手动权重调整审计
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestManualAdjustment:
    """record_manual_adjustment 手动调整审计落库"""

    def test_record_manual_adjustment_writes_audit(self, fb_user):
        kw, _ = KeywordWeight.objects.get_or_create(
            keyword=KEYWORD, root_type='test_root', defaults={'weight_score': 1.0})
        old_score = kw.weight_score
        kw.weight_score = 1.3
        kw.save(update_fields=['weight_score'])

        record_manual_adjustment(kw, old_score, fb_user)
        agg = KeywordFeedbackAgg.objects.get(
            report_date=timezone.localdate(), keyword=KEYWORD, root_type='test_root')
        assert agg.adjust_type == 'manual'
        assert agg.status == 'applied'
        assert agg.old_score == pytest.approx(1.0)
        assert agg.new_score == pytest.approx(1.3)
        assert agg.delta == pytest.approx(0.3)
        assert agg.reason == '人工调整'
        assert agg.actor_id == fb_user.id


# ---------------------------------------------------------------------------
# 每日定时任务
# ---------------------------------------------------------------------------
def test_aggregate_task_when_disabled_then_skipped():
    """总开关关闭时任务直接跳过，不产生任何聚合"""
    from apps.analytics.tasks import aggregate_keyword_feedback_daily
    with patch.object(AnalyticsConfig, 'feedback_loop_enabled', return_value=False):
        result = aggregate_keyword_feedback_daily()
    assert result['ok'] is True
    assert result['skipped'] is True


@pytest.mark.django_db
def test_aggregate_task_full_flow(fb_user, fb_session, yesterday):
    """每日任务端到端：聚合昨日数据并自动应用权重"""
    from apps.analytics.tasks import aggregate_keyword_feedback_daily
    with patch('apps.retrieval.bm25.tokenize', return_value=[KEYWORD]):
        for _ in range(10):
            _make_qa(fb_session, fb_user, yesterday, hits=[1, 2, 3])
        result = aggregate_keyword_feedback_daily(report_date=str(yesterday))
    assert result['ok'] is True
    assert result['applied'] == 1
    kw = KeywordWeight.objects.get(keyword=KEYWORD, root_type='test_root')
    assert kw.weight_score == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# API 冒烟测试
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestFeedbackLoopAPI:
    """反馈闭环 API：点击埋点 / 手动触发 / 复核 / 聚合记录列表"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self.client = Client()
        self.normal_user = _create_test_user(username='fb_normal', password='pass12345')
        self.reader = _create_test_user(
            username='fb_reader', password='pass12345',
            perms=['analytics.system.read'])
        self.writer = _create_test_user(
            username='fb_writer', password='pass12345',
            perms=['analytics.system.read', 'analytics.system.write'])
        self.anon_headers = {}
        self.normal_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.reader_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.reader)}'}
        self.writer_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.writer)}'}

    def test_chunk_click_anonymous_401(self):
        resp = self.client.post('/api/v1/analytics/chunk-clicks/',
                                data=json.dumps({'chunk_id': 1}),
                                content_type='application/json', **self.anon_headers)
        assert resp.status_code in [401, 403]

    def test_chunk_click_creates_log(self):
        resp = self.client.post('/api/v1/analytics/chunk-clicks/',
                                data=json.dumps({'chunk_id': 100, 'document_id': 7}),
                                content_type='application/json', **self.normal_headers)
        assert resp.status_code == 200
        log = ChunkClickLog.objects.latest('id')
        assert log.chunk_id == 100
        assert log.document_id == 7
        assert log.user_id == self.normal_user.id

    def test_chunk_click_missing_chunk_id_400(self):
        resp = self.client.post('/api/v1/analytics/chunk-clicks/',
                                data=json.dumps({}),
                                content_type='application/json', **self.normal_headers)
        assert resp.status_code == 400

    def test_run_loop_requires_write_perm(self):
        resp = self.client.post('/api/v1/analytics/feedback-loop/run/',
                                data=json.dumps({'date': '2026-01-01'}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403

    def test_run_loop_writer_200(self):
        resp = self.client.post('/api/v1/analytics/feedback-loop/run/',
                                data=json.dumps({'date': '2026-01-01'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['ok'] is True

    def test_run_loop_bad_date_400(self):
        resp = self.client.post('/api/v1/analytics/feedback-loop/run/',
                                data=json.dumps({'date': 'bad-date'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_aggregations_list_reader_200(self):
        resp = self.client.get('/api/v1/analytics/feedback-loop/aggregations/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert 'rows' in resp.json()

    def test_apply_requires_write_perm(self):
        resp = self.client.post('/api/v1/analytics/feedback-loop/apply/',
                                data=json.dumps({'id': 1, 'action': 'apply'}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403

    def test_apply_writer_missing_id_400(self):
        resp = self.client.post('/api/v1/analytics/feedback-loop/apply/',
                                data=json.dumps({'action': 'apply'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400