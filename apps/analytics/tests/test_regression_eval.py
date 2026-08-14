"""
apps.analytics.services.regression_service 单元测试 —— 低分回归测试集（沉淀 + 全链路评估）

覆盖范围：
- _get_or_create_regression_dataset：按 root_type 获取/创建回归测试集
- siphon_low_score_qa_to_regression_set：无候选 / 成功沉淀 / 防重复 /
  空问题跳过 / root_type 兜底 / 容量淘汰
- _enforce_regression_capacity：未超容量 / 超容量按 pass_count 降序 + last_eval_at 升序淘汰
- run_regression_evaluation：无测试集 / 通过更新 pass_count / 失败重置 0 /
  评估异常不改动 pass_count / limit 截断 / 连续通过达到阈值标记建议移除

说明：检索与评估引擎全部在源模块导入处 mock
（apps.retrieval.hybrid.hybrid_search / apps.analytics.services.deepeval_service.evaluate_with_deepeval /
apps.analytics.services.offline_eval_service.generate_answer），GoldenDataset / GoldenQuestion 用真实 Django 测试库。
"""
import pytest
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.analytics.services import regression_service
from apps.analytics.models import (
    GoldenDataset, GoldenQuestion, MultiDimensionScore,
)
from apps.chat.models import QaRecord
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk
from apps.memory.models import Session
from apps.users.models import User
from rag_project.config import AnalyticsConfig


def _make_user(username='reg_user', email='reg@test.com'):
    """创建测试用户（各测试类用不同用户名避免冲突）"""
    return User.objects.create_user(
        username=username, password='pass12345', email=email)


class RegressionTestBase:
    """回归测试集相关测试的公共 fixture（用户/会话/知识节点/文档/切片）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/节点/文档/切片"""
        self.user = _make_user()
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='REG')
        self.node = KnowledgeNode.objects.create(
            name='reg_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.doc = Document.objects.create(
            node=self.node, owner=self.user, title='reg-doc',
            file_name='d.txt', file_type='txt', file_hash='h1',
            root_type='test_root', status='done', dept_id=1)
        self.chunk = DocumentChunk.objects.create(
            document=self.doc, chunk_index=0, content='低分问题对应的上下文内容' * 10)

    def _qa(self, question='低分问题', root_type='hr', **kw):
        """创建低分 QA（默认带一条 completed 低分评估记录）"""
        qa = QaRecord.objects.create(
            session=self.session, user=self.user, question=question,
            answer='回答', root_type=root_type, **kw)
        MultiDimensionScore.objects.create(
            qa_record=qa, dimension='clarity', score=0.2, status='completed')
        return qa


# ============================================================================
# _get_or_create_regression_dataset —— 回归测试集获取/创建
# ============================================================================
@pytest.mark.django_db
class TestGetOrCreateRegressionDataset:
    """回归测试集创建测试"""

    def test_creates_new_dataset(self):
        """首次调用 → 创建 dataset_type=regression_low_score 的测试集"""
        ds, created = regression_service._get_or_create_regression_dataset('hr')
        assert created is True
        assert ds.dataset_type == 'regression_low_score'
        assert ds.root_type == 'hr'
        assert ds.name == '低分回归-hr'
        assert ds.status == 'active'
        assert ds.version == 'auto'

    def test_returns_existing(self):
        """再次调用 → 返回已有测试集（不重复创建）"""
        ds1, _ = regression_service._get_or_create_regression_dataset('hr')
        ds2, created = regression_service._get_or_create_regression_dataset('hr')
        assert created is False
        assert ds1.id == ds2.id


# ============================================================================
# siphon_low_score_qa_to_regression_set —— 低分对话沉淀
# ============================================================================
@pytest.mark.django_db
class TestSiphonLowScoreQA(RegressionTestBase):
    """低分对话沉淀到回归测试集测试"""

    def _siphon(self, top_n=10, capacity=200):
        """执行沉淀并展开配置 mock"""
        with patch.object(AnalyticsConfig, 'low_score_regression_top_n',
                          return_value=top_n), \
             patch.object(AnalyticsConfig, 'low_score_regression_capacity',
                          return_value=capacity):
            return regression_service.siphon_low_score_qa_to_regression_set()

    def test_no_candidates(self):
        """无低分评估记录 → 返回空统计"""
        result = regression_service.siphon_low_score_qa_to_regression_set(top_n=10)
        assert result == {'siphoned': 0, 'by_root': {}, 'skipped': 0}

    def test_siphon_success(self):
        """有低分 QA → 按 root_type 沉淀，GoldenQuestion 记录来源 QA"""
        qa = self._qa(question='社保如何补缴', root_type='hr')
        result = self._siphon()
        assert result['siphoned'] == 1
        assert result['by_root'] == {'hr': 1}
        assert result['skipped'] == 0

        ds = GoldenDataset.objects.get(
            dataset_type='regression_low_score', root_type='hr')
        q = GoldenQuestion.objects.get(dataset=ds)
        assert q.question == '社保如何补缴'
        assert q.source_qa_record_id == qa.id
        assert q.order == 1
        assert ds.question_count == 1

    def test_duplicate_prevented(self):
        """已沉淀的 QA（source_qa_record_id 已存在）→ 不重复沉淀"""
        self._qa(question='重复问题', root_type='hr')
        # 先沉淀一次
        self._siphon()
        # 再次沉淀 → 同一 QA 被排除
        result = self._siphon()
        assert result['siphoned'] == 0
        assert GoldenQuestion.objects.filter(
            dataset__dataset_type='regression_low_score').count() == 1

    def test_empty_question_skipped(self):
        """问题为空 → 跳过（不计入沉淀）"""
        self._qa(question='   ', root_type='hr')
        result = self._siphon()
        assert result['siphoned'] == 0
        assert result['skipped'] == 1

    def test_empty_root_type_falls_back(self):
        """root_type 为空字符串 → 兜底 company_doc

        数据库 root_type 列 NOT NULL，无法存 None；空字符串与 None 在源码
        `qa['root_type'] or 'company_doc'` 兜底逻辑中走同一分支。
        """
        self._qa(question='无领域问题', root_type='')
        result = self._siphon()
        assert result['by_root'] == {'company_doc': 1}
        assert GoldenDataset.objects.filter(
            dataset_type='regression_low_score', root_type='company_doc').exists()

    def test_capacity_enforced(self):
        """超出容量 → 淘汰 pass_count 高的旧记录（已多次通过最不需要保留）"""
        ds, _ = regression_service._get_or_create_regression_dataset('hr')
        old_pass = GoldenQuestion.objects.create(dataset=ds, question='旧通过', pass_count=2)
        GoldenQuestion.objects.create(dataset=ds, question='旧未通过', pass_count=0)
        self._qa(question='新低分问题', root_type='hr')
        # 容量=2：沉淀 1 条后共 3 条，淘汰 1 条（pass_count=2 的优先）
        result = self._siphon(capacity=2)
        assert result['siphoned'] == 1
        assert not GoldenQuestion.objects.filter(id=old_pass.id).exists()
        assert GoldenQuestion.objects.filter(dataset=ds).count() == 2
        ds.refresh_from_db()
        assert ds.question_count == 2


# ============================================================================
# _enforce_regression_capacity —— 容量上限控制
# ============================================================================
@pytest.mark.django_db
class TestEnforceRegressionCapacity:
    """容量淘汰策略测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/节点/文档/回归数据集"""
        self.user = _make_user(username='cap_user', email='cap@test.com')
        self.node = KnowledgeNode.objects.create(
            name='cap_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.doc = Document.objects.create(
            node=self.node, owner=self.user, title='cap-doc',
            file_name='c.txt', file_type='txt', file_hash='h2',
            root_type='test_root', status='done', dept_id=1)
        self.ds, _ = regression_service._get_or_create_regression_dataset('hr')

    def _mk_question(self, question, pass_count=0):
        """创建指定通过次数的问题"""
        return GoldenQuestion.objects.create(
            dataset=self.ds, question=question, pass_count=pass_count)

    def test_under_capacity_no_removal(self):
        """未超容量 → 不删除"""
        self._mk_question('q1')
        assert regression_service._enforce_regression_capacity(self.ds, 10) == 0
        assert GoldenQuestion.objects.filter(dataset=self.ds).count() == 1

    def test_over_capacity_removes_highest_pass_count(self):
        """超容量 → 按 pass_count 降序淘汰（优先移除已多次通过的）"""
        q_low = self._mk_question('q_low', pass_count=0)
        q_mid = self._mk_question('q_mid', pass_count=1)
        self._mk_question('q_high', pass_count=2)
        deleted = regression_service._enforce_regression_capacity(self.ds, 1)
        assert deleted == 2
        # 保留 pass_count=0 的记录
        remaining = list(GoldenQuestion.objects.filter(dataset=self.ds))
        assert [q.id for q in remaining] == [q_low.id]
        assert GoldenQuestion.objects.filter(id=q_mid.id).exists() is False
        self.ds.refresh_from_db()
        assert self.ds.question_count == 1

    def test_tie_breaks_by_last_eval_at_asc(self):
        """pass_count 相同 → last_eval_at 旧的先淘汰"""
        now = timezone.now()
        q_new = self._mk_question('q_new', pass_count=1)
        q_old = self._mk_question('q_old', pass_count=1)
        # last_eval_at 是手动字段，用 update 回拨模拟"长期未评估"
        GoldenQuestion.objects.filter(id=q_old.id).update(
            last_eval_at=now - timedelta(days=30))
        GoldenQuestion.objects.filter(id=q_new.id).update(last_eval_at=now)
        deleted = regression_service._enforce_regression_capacity(self.ds, 1)
        assert deleted == 1
        assert GoldenQuestion.objects.filter(id=q_old.id).exists() is False
        assert GoldenQuestion.objects.filter(id=q_new.id).exists()


# ============================================================================
# run_regression_evaluation —— 回归全链路评估
# ============================================================================
@pytest.mark.django_db
class TestRunRegressionEvaluation(RegressionTestBase):
    """回归测试集全链路评估测试"""

    def _eval_mocks(self, pass_threshold=0.7, suggest_passes=3):
        """标准 mock：通过阈值 0.7 + 建议移除阈值 3"""
        return [
            patch.object(AnalyticsConfig, 'low_score_regression_pass_threshold',
                         return_value=pass_threshold),
            patch.object(AnalyticsConfig, 'low_score_regression_suggest_remove_passes',
                         return_value=suggest_passes),
            patch.object(AnalyticsConfig, 'eval_model', return_value='test-model'),
            patch('apps.retrieval.hybrid.hybrid_search',
                  return_value={'chunks': [{'chunk_id': self.chunk.id}]}),
            patch('apps.analytics.services.offline_eval_service.generate_answer', return_value='生成回答'),
        ]

    def _make_regression_question(self, question='回归问题', pass_count=0):
        """创建一条回归测试问题"""
        ds, _ = regression_service._get_or_create_regression_dataset('hr')
        return ds, GoldenQuestion.objects.create(
            dataset=ds, question=question, pass_count=pass_count)

    def _run(self, mocks, dataset_id, deepeval_return=None, deepeval_effect=None):
        """按序展开 mock 执行评估，返回结果"""
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], \
             patch('apps.analytics.services.deepeval_service.evaluate_with_deepeval',
                   return_value=deepeval_return, side_effect=deepeval_effect):
            return regression_service.run_regression_evaluation(
                dataset_id=dataset_id, user=self.user)

    def test_no_dataset(self):
        """无回归测试集 → 返回 no_dataset"""
        result = regression_service.run_regression_evaluation(user=self.user)
        assert result['evaluated'] == 0
        assert result['reason'] == 'no_dataset'

    def test_passed_increments_pass_count(self):
        """均分 >= 阈值 → pass_count + 1，标记 passed"""
        ds, q = self._make_regression_question()
        mocks = self._eval_mocks()
        eval_result = [{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}]
        result = self._run(mocks, ds.id, deepeval_return=eval_result)
        assert result['evaluated'] == 1
        assert result['passed'] == 1
        assert result['failed'] == 0
        q.refresh_from_db()
        assert q.pass_count == 1
        assert q.last_eval_at is not None
        entry = result['results'][0]
        assert entry['status'] == 'passed'
        assert entry['pass_count'] == 1
        assert entry['suggest_remove'] is False
        assert entry['avg_score'] == 0.8

    def test_failed_resets_pass_count(self):
        """均分 < 阈值 → pass_count 重置为 0，标记 failed"""
        ds, q = self._make_regression_question(pass_count=2)
        mocks = self._eval_mocks()
        eval_result = [{'dimension': 'clarity', 'score': 0.3, 'reason': '差'}]
        result = self._run(mocks, ds.id, deepeval_return=eval_result)
        assert result['failed'] == 1
        q.refresh_from_db()
        assert q.pass_count == 0
        assert result['results'][0]['status'] == 'failed'

    def test_suggest_remove_flagged(self):
        """连续通过达到 suggest_remove_passes → 标记建议人工移除（不自动删除）"""
        ds, q = self._make_regression_question(pass_count=3)
        mocks = self._eval_mocks(pass_threshold=0.5)
        eval_result = [{'dimension': 'clarity', 'score': 0.9, 'reason': 'ok'}]
        result = self._run(mocks, ds.id, deepeval_return=eval_result)
        q.refresh_from_db()
        assert q.pass_count == 4
        assert result['results'][0]['suggest_remove'] is True
        # 不自动删除，问题仍然存在
        assert GoldenQuestion.objects.filter(id=q.id).exists()

    def test_eval_exception_keeps_pass_count(self):
        """评估异常 → 记录 error 不改动 pass_count（避免临时故障误伤）"""
        ds, q = self._make_regression_question(pass_count=1)
        mocks = self._eval_mocks()
        result = self._run(mocks, ds.id, deepeval_effect=RuntimeError('llm down'))
        assert result['evaluated'] == 0
        q.refresh_from_db()
        assert q.pass_count == 1
        assert result['results'][0]['error'].startswith('llm down')

    def test_limit_truncates(self):
        """limit 限制每个测试集最多评估的问题数"""
        ds, _ = self._make_regression_question('问题A')
        GoldenQuestion.objects.create(dataset=ds, question='问题B', order=1)
        mocks = self._eval_mocks()
        eval_result = [{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}]
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], \
             patch('apps.analytics.services.deepeval_service.evaluate_with_deepeval',
                   return_value=eval_result) as mock_eval:
            result = regression_service.run_regression_evaluation(
                dataset_id=ds.id, user=self.user, limit=1)
        assert result['evaluated'] == 1
        assert mock_eval.call_count == 1

    def test_multi_dimension_avg_score(self):
        """多维度结果 → 均分取各维度平均"""
        ds, _ = self._make_regression_question('多维度问题')
        mocks = self._eval_mocks(pass_threshold=0.7)
        eval_result = [
            {'dimension': 'clarity', 'score': 1.0, 'reason': 'a'},
            {'dimension': 'professionalism', 'score': 0.6, 'reason': 'b'},
        ]
        result = self._run(mocks, ds.id, deepeval_return=eval_result)
        # avg = (1.0 + 0.6) / 2 = 0.8 >= 0.7 通过
        assert result['passed'] == 1
        assert result['results'][0]['avg_score'] == 0.8
