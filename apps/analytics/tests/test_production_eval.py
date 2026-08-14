"""
apps.analytics.services.production_eval_service 单元测试 —— 生产对话自动评估（采样 + 分层限速）

覆盖范围：
- build_context_list：从 retrieval_scores 提取切片内容（Top5 + 500 字截断 / 空处理）
- _acquire_token / _acquire_hourly_token：Redis 原子计数器限速（超限 / 首次 EXPIRE / 异常保守跳过）
- check_daily_budget：数量上限（超限回退 DECR）+ 成本上限（DB 聚合）+ 聚合异常降级
- maybe_dispatch_eval：开关 → 无效对话过滤 → 采样 → 分层限速 → dispatch
- evaluate_sampled_qa：QA 不存在 / 预算拦截 / 无上下文 / 评估落库 / 评估异常

说明：Redis 全部 mock（apps.analytics.services.production_eval_service.get_redis），
evaluate_with_deepeval / run_low_score_analysis.delay 在源模块层 mock，
MultiDimensionScore 落库用真实 Django 测试库。
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from unittest.mock import patch, MagicMock

from apps.analytics.services import production_eval_service
from apps.analytics.models import MultiDimensionScore
from apps.chat.models import QaRecord
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk
from apps.memory.models import Session
from apps.users.models import User
from rag_project.config import AnalyticsConfig


# ============================================================================
# build_context_list —— 检索上下文提取
# ============================================================================
@pytest.mark.django_db
class TestBuildContextList:
    """检索切片内容提取测试（DB: DocumentChunk）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/节点/文档/切片"""
        self.user = User.objects.create_user(
            username='pe_user', password='pass12345', email='pe@test.com')
        self.node = KnowledgeNode.objects.create(
            name='pe_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.doc = Document.objects.create(
            node=self.node, owner=self.user, title='doc',
            file_name='d.txt', file_type='txt', file_hash='h',
            root_type='test_root', status='done', dept_id=1)
        self.chunk1 = DocumentChunk.objects.create(
            document=self.doc, chunk_index=0, content='内容' * 300)
        self.chunk2 = DocumentChunk.objects.create(
            document=self.doc, chunk_index=1, content='')

    def test_extracts_top5_contents_truncated(self):
        """取 Top5 切片内容并截断 500 字，空内容切片跳过"""
        qa = SimpleNamespace(retrieval_scores=[
            {'chunk_id': self.chunk1.id, 'rerank': 0.9},
            {'chunk_id': self.chunk2.id},          # 空内容 → 过滤
            {'chunk_id': 999999},                  # 不存在的切片 → 过滤
        ])
        ctx = production_eval_service.build_context_list(qa)
        assert len(ctx) == 1
        assert len(ctx[0]) == 500

    def test_empty_retrieval_scores(self):
        """无检索分数 → 空列表"""
        assert production_eval_service.build_context_list(SimpleNamespace(retrieval_scores=[])) == []

    def test_missing_chunk_id_skipped(self):
        """hit 缺 chunk_id → 跳过"""
        qa = SimpleNamespace(retrieval_scores=[{'rerank': 0.5}])
        assert production_eval_service.build_context_list(qa) == []

    def test_more_than_five_hits_limited(self):
        """超过 5 条命中只取前 5"""
        chunks = [DocumentChunk.objects.create(
            document=self.doc, chunk_index=i + 2, content=f'片段{i}') for i in range(6)]
        qa = SimpleNamespace(retrieval_scores=[
            {'chunk_id': c.id} for c in chunks])
        ctx = production_eval_service.build_context_list(qa)
        assert len(ctx) == 5


# ============================================================================
# _acquire_token / _acquire_hourly_token —— 令牌桶限速
# ============================================================================
class TestAcquireToken:
    """分钟级令牌桶测试"""

    @pytest.mark.unit
    def test_within_limit_returns_true(self):
        """计数 <= 上限 → True；首次计数还设置 EXPIRE"""
        import time as _t
        fake = MagicMock()
        fake.incr.return_value = 1
        with patch('apps.analytics.services.production_eval_service.get_redis', return_value=fake):
            assert production_eval_service._acquire_token(rate_per_min=5) is True
        fake.expire.assert_called_once_with(
            f'analytics:eval_rate:{int(_t.time() // 60)}', 65)

    @pytest.mark.unit
    def test_over_limit_returns_false(self):
        """计数 > 上限 → False，不设置 EXPIRE"""
        fake = MagicMock()
        fake.incr.return_value = 6
        with patch('apps.analytics.services.production_eval_service.get_redis', return_value=fake):
            assert production_eval_service._acquire_token(rate_per_min=5) is False
        fake.expire.assert_not_called()

    @pytest.mark.unit
    def test_redis_error_conservative_skip(self):
        """Redis 异常 → 保守返回 False（宁可少评估）"""
        fake = MagicMock()
        fake.incr.side_effect = Exception('redis down')
        with patch('apps.analytics.services.production_eval_service.get_redis', return_value=fake):
            assert production_eval_service._acquire_token(rate_per_min=5) is False


class TestAcquireHourlyToken:
    """小时级令牌桶测试"""

    @pytest.mark.unit
    def test_within_limit(self):
        """计数 <= 上限 → True"""
        fake = MagicMock()
        fake.incr.return_value = 2
        with patch('apps.analytics.services.production_eval_service.get_redis', return_value=fake):
            assert production_eval_service._acquire_hourly_token(rate_per_hour=50) is True

    @pytest.mark.unit
    def test_first_count_sets_expire(self):
        """首次计数（count=1）→ 设置 3700s EXPIRE"""
        fake = MagicMock()
        fake.incr.return_value = 1
        with patch('apps.analytics.services.production_eval_service.get_redis', return_value=fake):
            assert production_eval_service._acquire_hourly_token(rate_per_hour=50) is True
        fake.expire.assert_called_once()

    @pytest.mark.unit
    def test_over_limit(self):
        """计数 > 上限 → False"""
        fake = MagicMock()
        fake.incr.return_value = 51
        with patch('apps.analytics.services.production_eval_service.get_redis', return_value=fake):
            assert production_eval_service._acquire_hourly_token(rate_per_hour=50) is False

    @pytest.mark.unit
    def test_redis_error(self):
        """Redis 异常 → False"""
        fake = MagicMock()
        fake.incr.side_effect = Exception('redis down')
        with patch('apps.analytics.services.production_eval_service.get_redis', return_value=fake):
            assert production_eval_service._acquire_hourly_token(rate_per_hour=50) is False


# ============================================================================
# get_redis —— Analytics 专用连接
# ============================================================================
class TestGetRedis:
    """Redis 连接复用测试"""

    @pytest.mark.unit
    def test_reuses_analytics_connection(self):
        """复用 apps.analytics.services.realtime_service.get_redis_safe 的连接"""
        fake = MagicMock()
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake):
            assert production_eval_service.get_redis() is fake


# ============================================================================
# check_daily_budget —— 日预算检查（数量 + 成本）
# ============================================================================
@pytest.mark.django_db
class TestCheckDailyBudget:
    """日预算检查测试（DB: MultiDimensionScore 成本聚合）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='pe_user2', password='pass12345', email='pe2@test.com')
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='PE')
        self.qa = QaRecord.objects.create(
            session=self.session, user=self.user, question='q', answer='a',
            root_type='test_root')

    def test_daily_limit_exceeded(self):
        """数量超限 → (False, daily_limit_exceeded)，超限回退 DECR 不占配额"""
        fake = MagicMock()
        fake.incr.return_value = 501  # 大于默认 500
        with patch.object(AnalyticsConfig, 'eval_daily_limit', return_value=500), \
             patch.object(AnalyticsConfig, 'eval_cost_limit', return_value=1.0):
            passed, reason = production_eval_service.check_daily_budget(fake)
        assert (passed, reason) == (False, 'daily_limit_exceeded')
        fake.decr.assert_called_once()
        # count=501 非首次计数，TTL 已在首次 INCR 时设置，此处不重复 EXPIRE
        fake.expire.assert_not_called()

    def test_first_count_sets_expire(self):
        """首次计数（count=1）→ 设置 25h EXPIRE"""
        fake = MagicMock()
        fake.incr.return_value = 1
        with patch.object(AnalyticsConfig, 'eval_daily_limit', return_value=500), \
             patch.object(AnalyticsConfig, 'eval_cost_limit', return_value=1.0):
            passed, _ = production_eval_service.check_daily_budget(fake)
        assert passed is True
        fake.expire.assert_called_once()

    def test_cost_limit_exceeded(self):
        """今日累计成本 >= 上限 → (False, cost_limit_exceeded) 并回退计数"""
        MultiDimensionScore.objects.create(
            qa_record=self.qa, dimension='clarity', score=0.9, status='completed',
            eval_cost=Decimal('1.5'))
        fake = MagicMock()
        fake.incr.return_value = 2
        with patch.object(AnalyticsConfig, 'eval_daily_limit', return_value=500), \
             patch.object(AnalyticsConfig, 'eval_cost_limit', return_value=1.0):
            passed, reason = production_eval_service.check_daily_budget(fake)
        assert (passed, reason) == (False, 'cost_limit_exceeded')
        fake.decr.assert_called_once()

    def test_cost_aggregation_error_falls_back_to_quantity(self):
        """成本聚合查询异常 → 仅按数量限，不阻断评估

        代码走 filter().aggregate() 链，需让 filter 抛异常才能命中
        except 分支（patch manager.aggregate 不生效）。
        """
        fake = MagicMock()
        fake.incr.return_value = 1
        with patch.object(AnalyticsConfig, 'eval_daily_limit', return_value=500), \
             patch.object(AnalyticsConfig, 'eval_cost_limit', return_value=1.0), \
             patch.object(MultiDimensionScore.objects, 'filter',
                          side_effect=Exception('db down')), \
             patch('apps.analytics.services.production_eval_service.logger'):
            passed, reason = production_eval_service.check_daily_budget(fake)
        assert passed is True
        assert reason == ''


# ============================================================================
# maybe_dispatch_eval —— 评估入口（采样 + 分层限速 + dispatch）
# ============================================================================
class TestMaybeDispatchEval:
    """评估入口策略测试（全程 mock 配置与限速）"""

    def _qa(self, **kw):
        base = dict(id=1, is_success=True, answer_type='rag', is_hit_cache=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def _pass_all(self):
        """放行所有限速与预算检查（最后一个元素是 evaluate_sampled_qa mock）"""
        return [
            patch.object(AnalyticsConfig, 'production_eval_enabled', return_value=True),
            patch.object(AnalyticsConfig, 'production_eval_sample_rate', return_value=1.0),
            patch.object(AnalyticsConfig, 'production_eval_rate_per_min', return_value=5),
            patch.object(AnalyticsConfig, 'production_eval_rate_per_hour', return_value=50),
            patch('apps.analytics.services.production_eval_service.random.random', return_value=0.1),
            patch('apps.analytics.services.production_eval_service._acquire_token', return_value=True),
            patch('apps.analytics.services.production_eval_service._acquire_hourly_token', return_value=True),
            patch('apps.analytics.services.production_eval_service.get_redis', return_value=MagicMock()),
            patch('apps.analytics.services.production_eval_service.check_daily_budget', return_value=(True, '')),
            patch('apps.analytics.services.production_eval_service.evaluate_sampled_qa'),
        ]

    @pytest.mark.unit
    def test_disabled_skips(self):
        """开关关闭 → 直接返回"""
        with patch.object(AnalyticsConfig, 'production_eval_enabled', return_value=False), \
             patch('apps.analytics.services.production_eval_service.evaluate_sampled_qa') as mock_task:
            production_eval_service.maybe_dispatch_eval(self._qa())
        mock_task.delay.assert_not_called()

    @pytest.mark.unit
    def test_invalid_qa_filtered(self):
        """无效对话（失败/拒答/缓存命中）→ 不评估"""
        for qa in (self._qa(is_success=False),
                   self._qa(answer_type='refused'),
                   self._qa(is_hit_cache=True)):
            with patch.object(AnalyticsConfig, 'production_eval_enabled', return_value=True), \
                 patch('apps.analytics.services.production_eval_service.evaluate_sampled_qa') as mock_task:
                production_eval_service.maybe_dispatch_eval(qa)
            mock_task.delay.assert_not_called()

    @pytest.mark.unit
    def test_sample_miss_skips(self):
        """采样未命中（random >= sample_rate）→ 跳过"""
        with patch.object(AnalyticsConfig, 'production_eval_enabled', return_value=True), \
             patch.object(AnalyticsConfig, 'production_eval_sample_rate', return_value=0.05), \
             patch('apps.analytics.services.production_eval_service.random.random', return_value=0.5), \
             patch('apps.analytics.services.production_eval_service.evaluate_sampled_qa') as mock_task:
            production_eval_service.maybe_dispatch_eval(self._qa())
        mock_task.delay.assert_not_called()

    @pytest.mark.unit
    def test_rate_limit_skips(self):
        """分钟/小时限速不通过 → 跳过"""
        mocks = self._pass_all()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6], \
             mocks[7], mocks[8], mocks[9]:
            with patch('apps.analytics.services.production_eval_service._acquire_token', return_value=False):
                production_eval_service.maybe_dispatch_eval(self._qa())
            with patch('apps.analytics.services.production_eval_service._acquire_hourly_token', return_value=False):
                production_eval_service.maybe_dispatch_eval(self._qa())

    @pytest.mark.unit
    def test_daily_budget_skips(self):
        """日预算超限 → 跳过"""
        mocks = self._pass_all()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6], \
             mocks[7], mocks[8], mocks[9]:
            with patch('apps.analytics.services.production_eval_service.check_daily_budget',
                       return_value=(False, 'cost_limit_exceeded')):
                production_eval_service.maybe_dispatch_eval(self._qa())

    @pytest.mark.unit
    def test_daily_budget_error_conservative_skip(self):
        """日预算检查抛异常 → 保守跳过，不影响主对话流程"""
        mocks = self._pass_all()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6], \
             mocks[7], mocks[8], mocks[9] as mock_task:
            with patch('apps.analytics.services.production_eval_service.check_daily_budget',
                       side_effect=Exception('redis down')), \
                 patch('apps.analytics.services.production_eval_service.logger'):
                production_eval_service.maybe_dispatch_eval(self._qa())
        mock_task.delay.assert_not_called()

    @pytest.mark.unit
    def test_dispatch_called(self):
        """全部通过 → dispatch evaluate_sampled_qa.delay(qa_id)"""
        mocks = self._pass_all()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6], \
             mocks[7], mocks[8], mocks[9] as mock_task:
            production_eval_service.maybe_dispatch_eval(self._qa())
        mock_task.delay.assert_called_once_with(1)

    @pytest.mark.unit
    def test_inner_exception_ignored(self):
        """配置读取异常 → 记录日志不抛错（不影响主对话流程）"""
        with patch.object(AnalyticsConfig, 'production_eval_enabled',
                          side_effect=Exception('config down')), \
             patch('apps.analytics.services.production_eval_service.evaluate_sampled_qa') as mock_task:
            production_eval_service.maybe_dispatch_eval(self._qa())  # 不应抛出
        mock_task.delay.assert_not_called()


# ============================================================================
# evaluate_sampled_qa —— 异步评估单条对话
# ============================================================================
@pytest.mark.django_db
class TestEvaluateSampledQA:
    """单条 QA 异步评估测试（DB: MultiDimensionScore 落库）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='pe_user3', password='pass12345', email='pe3@test.com')
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='PE3')
        self.qa = QaRecord.objects.create(
            session=self.session, user=self.user, question='问题', answer='回答',
            root_type='test_root',
            retrieval_scores=[{'chunk_id': 1, 'rerank': 0.8}])

    def _mocks(self, evaluate_return):
        """标准 mock 集合（evaluate_sampled_qa 成功路径所需）"""
        return [
            patch.object(AnalyticsConfig, 'eval_model', return_value='test-model'),
            patch('apps.analytics.services.production_eval_service.get_redis', return_value=MagicMock()),
            patch('apps.analytics.services.production_eval_service.check_daily_budget', return_value=(True, '')),
            patch('apps.analytics.services.production_eval_service.build_context_list', return_value=['上下文']),
            patch('apps.analytics.services.deepeval_service.evaluate_with_deepeval',
                  return_value=evaluate_return),
            patch('apps.analytics.tasks.run_low_score_analysis'),
        ]

    def _run(self, mocks, **kwargs):
        """按序展开 mock 上下文执行任务，返回 (结果, 低分归因 mock)"""
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5] as mock_low:
            return production_eval_service.evaluate_sampled_qa(self.qa.id, **kwargs), mock_low

    def test_qa_not_found(self):
        """QA 不存在 → {'ok': False, 'reason': 'qa_not_found'}"""
        with patch.object(AnalyticsConfig, 'eval_model', return_value='m'), \
             patch('apps.analytics.services.production_eval_service.get_redis', return_value=MagicMock()), \
             patch('apps.analytics.services.production_eval_service.check_daily_budget', return_value=(True, '')):
            result = production_eval_service.evaluate_sampled_qa(999999)
        assert result == {'ok': False, 'reason': 'qa_not_found'}

    def test_budget_blocked(self):
        """日预算二次检查不通过（非手动场景）→ skipped"""
        mocks = self._mocks([{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            with patch('apps.analytics.services.production_eval_service.check_daily_budget',
                       return_value=(False, 'cost_limit_exceeded')):
                result = production_eval_service.evaluate_sampled_qa(self.qa.id)
        assert result == {'ok': False, 'skipped': True, 'reason': 'cost_limit_exceeded'}

    def test_skip_budget_check_bypasses(self):
        """手动评估（skip_budget_check=True）→ 跳过预算检查直接评估"""
        result, _ = self._run(self._mocks(
            [{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}]),
            skip_budget_check=True)
        assert result['ok'] is True

    def test_no_context_skipped(self):
        """无检索上下文 → {'ok': False, 'reason': 'no_context'}"""
        mocks = self._mocks([{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            with patch('apps.analytics.services.production_eval_service.build_context_list', return_value=[]):
                result = production_eval_service.evaluate_sampled_qa(self.qa.id)
        assert result == {'ok': False, 'reason': 'no_context'}

    def test_success_persists_and_dispatches_low_score(self):
        """评估成功 → 逐维度落库 + 派发低分归因 + 返回统计"""
        results = [
            {'dimension': 'clarity', 'score': 0.8, 'reason': '清晰',
             'tokens_used': 10, 'latency_ms': 5},
            {'dimension': 'professionalism', 'score': 0.0, 'reason': '失败', 'tokens_used': 0},
        ]
        result, mock_low = self._run(self._mocks(results))
        assert result['ok'] is True
        assert result['evaluated'] == 1   # 仅 score>0 的维度
        assert result['total'] == 2
        assert result['eval_batch_id'].startswith('prod_sampled_')

        # 落库断言
        scores = MultiDimensionScore.objects.filter(qa_record=self.qa).order_by('dimension')
        assert scores.count() == 2
        clarity = scores.get(dimension='clarity')
        assert clarity.score == 0.8
        assert clarity.eval_model == 'deepeval-test-model'
        assert clarity.eval_tokens_used == 10
        assert clarity.status == 'completed'
        # 低分归因异步派发
        mock_low.delay.assert_called_once_with(self.qa.id)

    def test_eval_failure_returns_reason(self):
        """评估抛异常 → {'ok': False, 'reason': 'eval_failed: ...'}"""
        mocks = self._mocks(None)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            with patch('apps.analytics.services.deepeval_service.evaluate_with_deepeval',
                       side_effect=RuntimeError('llm down')):
                result = production_eval_service.evaluate_sampled_qa(self.qa.id)
        assert result['ok'] is False
        assert result['reason'].startswith('eval_failed:')

    def test_budget_check_error_continues(self):
        """预算检查异常 → 记录警告继续评估（不阻塞）"""
        mocks = self._mocks([{'dimension': 'clarity', 'score': 0.9, 'reason': 'ok'}])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            with patch('apps.analytics.services.production_eval_service.check_daily_budget',
                       side_effect=Exception('redis down')):
                result = production_eval_service.evaluate_sampled_qa(self.qa.id)
        assert result['ok'] is True

    def test_provided_batch_id_used(self):
        """手动评估传入 eval_batch_id 时应原样使用（不自动生成）"""
        mocks = self._mocks([{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            result = production_eval_service.evaluate_sampled_qa(self.qa.id, eval_batch_id='manual_001')
        assert result['eval_batch_id'] == 'manual_001'

    def test_low_score_dispatch_failure_ignored(self):
        """低分归因派发异常 → 忽略，评估结果不受影响"""
        mocks = self._mocks([{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5] as mock_low, \
             patch('apps.analytics.services.production_eval_service.logger'):
            mock_low.delay.side_effect = Exception('celery down')
            result = production_eval_service.evaluate_sampled_qa(self.qa.id)
        assert result['ok'] is True


# ============================================================================
# 路由分支 —— wiki / graphrag 上下文重建（build_context_list 分流）
# ============================================================================
class TestRouteContexts:
    """三层路由回答的评估上下文重建测试"""

    @pytest.mark.unit
    def test_context_list_wiki_route(self):
        """route_source='wiki' 时走 wiki 上下文重建"""
        with patch('apps.analytics.services.production_eval_service._build_wiki_route_context',
                   return_value=['wiki 内容']) as m:
            ctx = production_eval_service.build_context_list(SimpleNamespace(
                route_source='wiki', question='问题'))
        m.assert_called_once_with('问题')
        assert ctx == ['wiki 内容']

    @pytest.mark.unit
    def test_context_list_graphrag_route(self):
        """route_source 以 graphrag 开头时走图谱上下文重建"""
        with patch('apps.analytics.services.production_eval_service._build_graphrag_route_context',
                   return_value=['图上下文']) as m:
            ctx = production_eval_service.build_context_list(SimpleNamespace(
                route_source='graphrag_default', question='q', user_id=7))
        m.assert_called_once_with('q', 7)
        assert ctx == ['图上下文']

    @pytest.mark.unit
    def test_wiki_context_success_truncated(self):
        """wiki 检索成功返回正文并截断 500 字"""
        with patch('apps.wiki.retriever.search_wiki',
                   return_value=[{'content': 'W' * 600}]):
            ctx = production_eval_service._build_wiki_route_context('q')
        assert len(ctx) == 1
        assert len(ctx[0]) == 500

    @pytest.mark.unit
    def test_wiki_context_search_error_returns_empty(self):
        """wiki 检索异常 → 空列表"""
        with patch('apps.wiki.retriever.search_wiki', side_effect=Exception('down')), \
             patch('apps.analytics.services.production_eval_service.logger'):
            assert production_eval_service._build_wiki_route_context('q') == []

    @pytest.mark.unit
    def test_wiki_context_no_results(self):
        """wiki 无命中结果 → 空列表"""
        with patch('apps.wiki.retriever.search_wiki', return_value=[]):
            assert production_eval_service._build_wiki_route_context('q') == []

    @pytest.mark.unit
    def test_wiki_context_empty_content(self):
        """wiki 结果正文为空 → 空列表"""
        with patch('apps.wiki.retriever.search_wiki', return_value=[{'content': '  '}]):
            assert production_eval_service._build_wiki_route_context('q') == []

    @pytest.mark.django_db
    def test_graphrag_context_uses_user(self):
        """graphrag 检索应使用指定 user 做权限过滤"""
        user = User.objects.create_user(
            username='g_user', password='x', email='g@test.com')
        with patch('apps.graph.retriever.graphrag_search',
                   return_value={'context': '图上下文内容'}) as m:
            ctx = production_eval_service._build_graphrag_route_context('q', user.id)
        m.assert_called_once()
        assert m.call_args[0][1].id == user.id
        assert ctx == ['图上下文内容']

    @pytest.mark.django_db
    def test_graphrag_context_user_missing_falls_back_to_system(self):
        """原用户不存在 → 回退系统用户"""
        User.objects.create_user(username='system', password='x', email='sys@test.com')
        with patch('apps.graph.retriever.graphrag_search',
                   return_value={'context': 'c'}) as m:
            ctx = production_eval_service._build_graphrag_route_context('q', 999999)
        assert m.call_args[0][1].username == 'system'
        assert ctx == ['c']

    @pytest.mark.unit
    def test_graphrag_context_no_user_returns_empty(self):
        """系统用户也不存在 → 空列表"""
        mock_qs = MagicMock()
        mock_qs.first.return_value = None
        with patch('apps.users.models.User.objects.filter', return_value=mock_qs):
            assert production_eval_service._build_graphrag_route_context('q', 0) == []

    @pytest.mark.unit
    def test_graphrag_context_user_lookup_error_falls_back(self):
        """原用户查询异常 → 捕获后回退系统用户"""
        system = SimpleNamespace(id=2, username='system')
        mock_qs = MagicMock()
        mock_qs.first.return_value = system
        with patch('apps.users.models.User.objects.filter',
                   side_effect=[Exception('db down'), mock_qs]), \
             patch('apps.graph.retriever.graphrag_search',
                   return_value={'context': 'c'}) as m, \
             patch('apps.analytics.services.production_eval_service.logger'):
            ctx = production_eval_service._build_graphrag_route_context('q', 1)
        assert m.call_args[0][1].username == 'system'
        assert ctx == ['c']

    @pytest.mark.unit
    def test_graphrag_context_search_error_returns_empty(self):
        """图谱检索异常 → 空列表"""
        user = SimpleNamespace(id=1, username='u')
        mock_qs = MagicMock()
        mock_qs.first.return_value = user
        with patch('apps.users.models.User.objects.filter', return_value=mock_qs), \
             patch('apps.graph.retriever.graphrag_search', side_effect=Exception('down')), \
             patch('apps.analytics.services.production_eval_service.logger'):
            assert production_eval_service._build_graphrag_route_context('q', 1) == []

    @pytest.mark.unit
    def test_graphrag_context_empty_result_returns_empty(self):
        """图谱检索结果无上下文 → 空列表"""
        user = SimpleNamespace(id=1, username='u')
        mock_qs = MagicMock()
        mock_qs.first.return_value = user
        with patch('apps.users.models.User.objects.filter', return_value=mock_qs), \
             patch('apps.graph.retriever.graphrag_search', return_value={'context': ' '}):
            assert production_eval_service._build_graphrag_route_context('q', 1) == []
