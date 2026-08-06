"""
Analytics 评估视图测试：黄金测试集管理 / 低分回归 / 离线检索与回答评估 / 文档质量 / 多维度评估

覆盖范围（与 views.py 逐视图对齐）：
- 黄金测试集管理全套（list/detail/import/export/questions）
- 低分回归沉淀 / 回归评估派发
- 离线检索/回答评估（mock 重型计算）与检索报告列表
- 文档质量汇总 / 触发文档质量评估（单文档 / 批量）/ 文档质量报告列表（min_score 过滤 / 组织筛选）
- 多维度评分列表（total / dimension_summary / 过滤参数）/ 手动多维度评估派发（异步派发 + 上下文预检）

说明：
- 所有重型后端调用（ragas/deepeval/LLM/Redis/离线评估）均在视图源码导入处 mock
- 权限模型与 test_views.py 保持一致：super_admin 快路径 / permission_key 判定
"""
import json
from datetime import timedelta
from decimal import Decimal

from unittest.mock import patch, MagicMock

import pytest

from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import (
    User, Role, UserRoleRel, RolePermissionRel, Permission, GrantStatus,
    Department, Team, UserDeptScopeRel, UserTeamScopeRel,
)
from apps.chat.models import QaRecord, QaFeedback
from apps.memory.models import Session
from apps.knowledge.models import KnowledgeNode, Document
from apps.analytics.models import (
    KeywordWeight, SystemMetricsReport, OrgUsageReport, QueueDepthLog,
    GoldenDataset, GoldenQuestion, GoldenReferenceAnswer,
    MultiDimensionScore, DocumentQualityReport, RetrievalQualityReport,
    CoverageReport, LowScoreAnalysis,
)


# ============================================================================
# 本地工具函数（不依赖其他测试文件，按 test_views.py 模式重新定义）
# ============================================================================

def _create_test_user(username='extra_user', password='pass12345',
                      is_super_admin=False, perms=None):
    """创建测试用户并分配权限（super_admin 角色或 permission_key 角色）"""
    user = User.objects.create_user(
        username=username, password=password,
        email=f'{username}@test.com',
    )
    if is_super_admin:
        admin_role, _ = Role.objects.get_or_create(
            role_key='super_admin',
            defaults={'name': '超级管理员', 'is_builtin': True}
        )
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role,
            defaults={'status': GrantStatus.ACTIVE}
        )
    if perms:
        role = Role.objects.create(
            name=f'{username}_role', role_key=f'role_{username}')
        UserRoleRel.objects.create(
            user=user, role=role, status=GrantStatus.ACTIVE)
        for perm_key in perms:
            perm, _ = Permission.objects.get_or_create(
                permission_key=perm_key, defaults={'permission_name': perm_key})
            RolePermissionRel.objects.create(
                role=role, permission=perm, is_active=True)
    return user


def _get_auth_token(user):
    """获取 JWT access token"""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


@pytest.mark.django_db
class AnalyticsViewsBase:
    """视图测试基类：用户 / 组织架构 / 认证头 / 数据构造辅助（子类自动继承 django_db）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/认证头/组织架构"""
        self.client = Client()
        self.today = timezone.now().date()
        self.yesterday = self.today - timedelta(days=1)

        # --- 用户 ---
        self.normal_user = _create_test_user('extra_normal')
        self.super_admin = _create_test_user('extra_admin', is_super_admin=True)
        self.reader = _create_test_user(
            'extra_reader', perms=['analytics.system.read'])
        self.writer = _create_test_user(
            'extra_writer', perms=['analytics.system.read', 'analytics.system.write'])
        self.org_reader = _create_test_user(
            'extra_org', perms=['analytics.org.read'])

        # --- 认证头 ---
        self.anon_headers = {}
        self.normal_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.admin_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin)}'}
        self.reader_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.reader)}'}
        self.writer_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.writer)}'}
        self.org_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.org_reader)}'}

        # --- 组织架构：两个部门，A 下两个团队，B 下一个团队 ---
        self.dept_a = Department.objects.create(name='补充测试部门A')
        self.dept_b = Department.objects.create(name='补充测试部门B')
        self.team_a1 = Team.objects.create(name='团队A1', department=self.dept_a)
        self.team_a2 = Team.objects.create(name='团队A2', department=self.dept_a)
        self.team_b1 = Team.objects.create(name='团队B1', department=self.dept_b)

        # --- 知识节点根（文档归属用）---
        self.root_node = KnowledgeNode.objects.create(
            name='extra_root', node_type='root', root_type='test_root',
            created_by=self.super_admin)

    # ------------------------------------------------------------------
    # 数据构造辅助
    # ------------------------------------------------------------------
    def _make_qa(self, **kw):
        """创建一条 QaRecord（含默认字段），返回实例"""
        session = kw.pop('session', None)
        if session is None:
            session = Session.objects.create(
                user=self.normal_user, root_type='test_root', title='extra_session')
        defaults = dict(
            session=session, user=self.normal_user,
            question='补充测试问题', answer='补充测试回答',
            answer_type='rag', root_type='test_root',
            is_hit_cache=False, is_success=True, error_type='',
            tokens_prompt=100, tokens_completion=50,
            cost_estimate=Decimal('0.010000'),
            latency_total_ms=500, latency_llm_ms=300,
            latency_retrieval_ms=100, latency_ttfb_ms=200,
            tokens_per_second=10.0,
        )
        defaults.update(kw)
        return QaRecord.objects.create(**defaults)

    @staticmethod
    def _set_created_at(obj, dt):
        """绕过 auto_now_add 直接修改 created_at（update 不触发 pre_save）"""
        type(obj).objects.filter(pk=obj.pk).update(created_at=dt)

    def _make_feedback(self, qa, rating=1, **kw):
        """创建 QaFeedback"""
        defaults = dict(qa_record=qa, user=self.normal_user, rating=rating)
        defaults.update(kw)
        return QaFeedback.objects.create(**defaults)

    def _make_doc(self, **kw):
        """创建 Document（team/dept 二选一归属）"""
        defaults = dict(
            node=self.root_node, owner=self.normal_user, title='文档',
            file_name='doc.txt', file_type='txt', file_hash='h',
            root_type='test_root', status='done', dept_id=self.dept_a.id,
        )
        defaults.update(kw)
        return Document.objects.create(**defaults)

    def _make_score(self, qa, dimension, score, **kw):
        """创建一条 MultiDimensionScore"""
        defaults = dict(
            qa_record=qa, dimension=dimension, score=score,
            reason='reason', eval_model='deepseek-chat', status='completed',
        )
        defaults.update(kw)
        return MultiDimensionScore.objects.create(**defaults)

    def _make_low_analysis(self, qa, **kw):
        """创建一条 LowScoreAnalysis"""
        defaults = dict(
            qa_record=qa, avg_score=0.3, threshold=0.5,
            root_cause_category='retrieval_recall', root_cause_detail='detail',
            affected_layer='retrieval', low_dimensions=[{'dimension': 'faithfulness', 'score': 0.3}],
            diagnosis='diagnosis', suggestions=[{'type': 'short_term', 'action': 'a'}],
            analysis_method='rule', status='completed',
        )
        defaults.update(kw)
        return LowScoreAnalysis.objects.create(**defaults)


# ============================================================================
# 黄金测试集管理（list/detail/import/export/questions 全套）
# ============================================================================
class TestGoldenDatasetAPI(AnalyticsViewsBase):
    """GoldenDataset 系列视图测试（重型后端均在导入处 mock）"""

    def _make_dataset(self, **kw):
        defaults = dict(name='补充测试集', root_type='company_doc',
                        status='active', dataset_type='custom')
        defaults.update(kw)
        return GoldenDataset.objects.create(**defaults)

    def test_list_empty_200(self):
        resp = self.client.get('/api/v1/analytics/golden-datasets/', **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 0

    def test_list_with_data(self):
        # 列表行含 dataset_type_label 中文展示名
        self._make_dataset(name='数据集A')
        resp = self.client.get('/api/v1/analytics/golden-datasets/', **self.reader_headers)
        assert resp.status_code == 200
        row = resp.json()['rows'][0]
        assert row['dataset_type_label'] == '自定义'
        for key in ['id', 'name', 'description', 'root_type', 'status',
                    'dataset_type', 'question_count', 'version', 'created_at', 'updated_at']:
            assert key in row

    def test_list_status_filter(self):
        self._make_dataset(name='草稿集', status='draft')
        self._make_dataset(name='启用集', status='active')
        resp = self.client.get('/api/v1/analytics/golden-datasets/?status=draft',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 1

    def test_list_dataset_type_filter(self):
        self._make_dataset(name='自定义集')
        self._make_dataset(name='回归集', dataset_type='regression_low_score')
        resp = self.client.get('/api/v1/analytics/golden-datasets/?dataset_type=regression_low_score',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 1
        assert resp.json()['rows'][0]['dataset_type'] == 'regression_low_score'

    def test_post_missing_name_400(self):
        resp = self.client.post('/api/v1/analytics/golden-datasets/',
                                data=json.dumps({}), content_type='application/json',
                                **self.writer_headers)
        assert resp.status_code == 400

    def test_post_read_only_403(self):
        # POST 写操作需要 write 权限（get_permissions 按方法切换）
        resp = self.client.post('/api/v1/analytics/golden-datasets/',
                                data=json.dumps({'name': 'x'}), content_type='application/json',
                                **self.reader_headers)
        assert resp.status_code == 403

    def test_post_happy_200(self):
        # create_golden_dataset 在视图内部导入，patch 模块级符号
        mock_ds = MagicMock(id=42, name='新数据集', root_type='company_doc',
                            status='draft', version='v1')
        with patch('apps.analytics.offline_eval.create_golden_dataset',
                   return_value=mock_ds) as m:
            resp = self.client.post('/api/v1/analytics/golden-datasets/',
                                    data=json.dumps({'name': '新数据集'}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['id'] == 42
        m.assert_called_once()
        # 调用参数：name/root_type 默认 company_doc/version 默认 v1
        assert m.call_args[1]['name'] == '新数据集'

    def test_detail_200_with_questions(self):
        # 详情含 questions（has_reference=True / relevant_doc_count）
        ds = self._make_dataset(name='详情集')
        q = GoldenQuestion.objects.create(dataset=ds, question='问题1')
        GoldenReferenceAnswer.objects.create(question=q, reference_answer='参考答案')
        resp = self.client.get(f'/api/v1/analytics/golden-datasets/{ds.id}/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['dataset_type_label'] == '自定义'
        q_data = data['questions'][0]
        assert q_data['has_reference']
        assert q_data['relevant_doc_count'] == 0
        assert 'suggest_remove_passes' in data

    def test_detail_404(self):
        resp = self.client.get('/api/v1/analytics/golden-datasets/99999/',
                               **self.reader_headers)
        assert resp.status_code == 404

    def test_detail_get_with_read_perm_200(self):
        # GET 详情只需 read 权限
        ds = self._make_dataset()
        resp = self.client.get(f'/api/v1/analytics/golden-datasets/{ds.id}/',
                               **self.reader_headers)
        assert resp.status_code == 200

    def test_put_update_200(self):
        ds = self._make_dataset(name='旧名')
        resp = self.client.put(f'/api/v1/analytics/golden-datasets/{ds.id}/',
                               data=json.dumps({'name': '新名', 'status': 'active'}),
                               content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        ds.refresh_from_db()
        assert ds.name == '新名'
        assert ds.status == 'active'

    def test_put_invalid_status_400(self):
        ds = self._make_dataset()
        resp = self.client.put(f'/api/v1/analytics/golden-datasets/{ds.id}/',
                               data=json.dumps({'status': 'bad_status'}),
                               content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_put_404(self):
        resp = self.client.put('/api/v1/analytics/golden-datasets/99999/',
                               data=json.dumps({'name': 'x'}),
                               content_type='application/json', **self.writer_headers)
        assert resp.status_code == 404

    def test_delete_200(self):
        ds = self._make_dataset()
        resp = self.client.delete(f'/api/v1/analytics/golden-datasets/{ds.id}/',
                                  **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['ok'] == True

    def test_delete_404(self):
        resp = self.client.delete('/api/v1/analytics/golden-datasets/99999/',
                                  **self.writer_headers)
        assert resp.status_code == 404

    def test_import_empty_400(self):
        ds = self._make_dataset()
        resp = self.client.post(f'/api/v1/analytics/golden-datasets/{ds.id}/import/',
                                data=json.dumps({'questions': []}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_import_happy_200(self):
        ds = self._make_dataset()
        with patch('apps.analytics.offline_eval.import_questions_from_json',
                   return_value={'ok': True, 'imported': 1}) as m:
            resp = self.client.post(
                f'/api/v1/analytics/golden-datasets/{ds.id}/import/',
                data=json.dumps({'questions': [{'question': 'q1'}]}),
                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['imported'] == 1
        assert m.call_args[1]['dataset_id'] == ds.id

    def test_export_happy_200(self):
        ds = self._make_dataset()
        with patch('apps.analytics.offline_eval.export_dataset_to_json',
                   return_value=[{'question': 'q1'}]) as m:
            resp = self.client.get(f'/api/v1/analytics/golden-datasets/{ds.id}/export/',
                                   **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['dataset_id'] == ds.id
        assert len(resp.json()['questions']) == 1
        m.assert_called_once_with(ds.id)

    def test_question_post_happy_200(self):
        ds = self._make_dataset()
        with patch('apps.analytics.offline_eval.import_questions_from_json',
                   return_value={'ok': True, 'imported': 1}):
            resp = self.client.post(f'/api/v1/analytics/golden-datasets/{ds.id}/questions/',
                                    data=json.dumps({'question': 'q1'}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200

    def test_question_delete_missing_400(self):
        ds = self._make_dataset()
        resp = self.client.delete(f'/api/v1/analytics/golden-datasets/{ds.id}/questions/',
                                  **self.writer_headers)
        assert resp.status_code == 400

    def test_question_delete_invalid_400(self):
        ds = self._make_dataset()
        resp = self.client.delete(
            f'/api/v1/analytics/golden-datasets/{ds.id}/questions/?question_id=abc',
            **self.writer_headers)
        assert resp.status_code == 400

    def test_question_delete_404(self):
        ds = self._make_dataset()
        resp = self.client.delete(
            f'/api/v1/analytics/golden-datasets/{ds.id}/questions/?question_id=99999',
            **self.writer_headers)
        assert resp.status_code == 404

    def test_question_delete_happy_200(self):
        ds = self._make_dataset()
        q = GoldenQuestion.objects.create(dataset=ds, question='待删问题')
        resp = self.client.delete(
            f'/api/v1/analytics/golden-datasets/{ds.id}/questions/?question_id={q.id}',
            **self.writer_headers)
        assert resp.status_code == 200
        assert not GoldenQuestion.objects.filter(id=q.id).exists()


# ============================================================================
# 低分回归沉淀 / 回归评估派发
# ============================================================================
class TestSiphonRegressionAPI(AnalyticsViewsBase):
    """SiphonRegressionView 测试"""

    def test_post_happy_200(self):
        with patch('apps.analytics.regression_eval.siphon_low_score_qa_to_regression_set',
                   return_value={'siphoned': 3}) as m:
            resp = self.client.post('/api/v1/analytics/regression/siphon/',
                                    data=json.dumps({'top_n': 5}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['siphoned'] == 3
        # top_n 透传给沉淀函数
        assert m.call_args[1]['top_n'] == 5

    def test_post_top_n_invalid_400(self):
        resp = self.client.post('/api/v1/analytics/regression/siphon/',
                                data=json.dumps({'top_n': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_post_exception_500(self):
        # 沉淀函数异常 → 500 且包含错误信息
        with patch('apps.analytics.regression_eval.siphon_low_score_qa_to_regression_set',
                   side_effect=RuntimeError('boom')):
            resp = self.client.post('/api/v1/analytics/regression/siphon/',
                                    data=json.dumps({}), content_type='application/json',
                                    **self.writer_headers)
        assert resp.status_code == 500
        assert 'boom' in resp.json()['detail']

    def test_post_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/regression/siphon/',
                                data=json.dumps({}), content_type='application/json',
                                **self.reader_headers)
        assert resp.status_code == 403


class TestRunRegressionEvalAPI(AnalyticsViewsBase):
    """RunRegressionEvalView 测试（异步派发 Celery）"""

    def test_post_happy_200(self):
        with patch('apps.analytics.tasks.run_regression_evaluation_task') as m:
            resp = self.client.post('/api/v1/analytics/regression/eval/',
                                    data=json.dumps({'dataset_id': 7, 'limit': 10}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['queued']
        m.delay.assert_called_once_with(dataset_id=7, limit=10)

    def test_post_dataset_id_invalid_400(self):
        resp = self.client.post('/api/v1/analytics/regression/eval/',
                                data=json.dumps({'dataset_id': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_post_limit_invalid_400(self):
        resp = self.client.post('/api/v1/analytics/regression/eval/',
                                data=json.dumps({'limit': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_post_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/regression/eval/',
                                data=json.dumps({}), content_type='application/json',
                                **self.reader_headers)
        assert resp.status_code == 403


# ============================================================================
# 离线评估执行（检索 / 回答）与检索报告列表
# ============================================================================
class TestRunRetrievalEvalAPI(AnalyticsViewsBase):
    """RunRetrievalEvalView 测试"""

    def test_missing_dataset_id_400(self):
        resp = self.client.post('/api/v1/analytics/eval/retrieval/',
                                data=json.dumps({}), content_type='application/json',
                                **self.writer_headers)
        assert resp.status_code == 400

    def test_invalid_dataset_id_400(self):
        resp = self.client.post('/api/v1/analytics/eval/retrieval/',
                                data=json.dumps({'dataset_id': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_happy_200(self):
        mock_report = MagicMock(id=1, recall_at_5=0.5, recall_at_10=0.6,
                                recall_at_20=0.7, mrr=0.4, ndcg_at_10=0.3,
                                questions_with_hits=8, questions_without_hits=2)
        with patch('apps.analytics.offline_eval.run_retrieval_evaluation',
                   return_value=mock_report) as m:
            resp = self.client.post('/api/v1/analytics/eval/retrieval/',
                                    data=json.dumps({'dataset_id': 1}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['report_id'] == 1
        assert data['recall_at_5'] == 0.5
        assert data['questions_without_hits'] == 2
        assert m.call_args[1]['dataset_id'] == 1

    def test_exception_500(self):
        with patch('apps.analytics.offline_eval.run_retrieval_evaluation',
                   side_effect=RuntimeError('eval failed')):
            resp = self.client.post('/api/v1/analytics/eval/retrieval/',
                                    data=json.dumps({'dataset_id': 1}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 500

    def test_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/eval/retrieval/',
                                data=json.dumps({'dataset_id': 1}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403


class TestRunAnswerEvalAPI(AnalyticsViewsBase):
    """RunAnswerEvalView 测试"""

    def test_missing_dataset_id_400(self):
        resp = self.client.post('/api/v1/analytics/eval/answer/',
                                data=json.dumps({}), content_type='application/json',
                                **self.writer_headers)
        assert resp.status_code == 400

    def test_invalid_dataset_id_400(self):
        resp = self.client.post('/api/v1/analytics/eval/answer/',
                                data=json.dumps({'dataset_id': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_invalid_max_questions_400(self):
        resp = self.client.post('/api/v1/analytics/eval/answer/',
                                data=json.dumps({'dataset_id': 1, 'max_questions': 'x'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_happy_200(self):
        # max_questions 钳位：传 9999 应被钳到 100
        results = [{'question': 'q1', 'score': 0.8}]
        with patch('apps.analytics.offline_eval.run_answer_quality_evaluation',
                   return_value=results) as m:
            resp = self.client.post('/api/v1/analytics/eval/answer/',
                                    data=json.dumps({'dataset_id': 1, 'max_questions': 9999}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok']
        assert data['evaluated_count'] == 1
        assert m.call_args[1]['max_questions'] == 100

    def test_exception_500(self):
        with patch('apps.analytics.offline_eval.run_answer_quality_evaluation',
                   side_effect=RuntimeError('eval failed')):
            resp = self.client.post('/api/v1/analytics/eval/answer/',
                                    data=json.dumps({'dataset_id': 1}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 500

    def test_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/eval/answer/',
                                data=json.dumps({'dataset_id': 1}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403


class TestRetrievalReportListAPI(AnalyticsViewsBase):
    """RetrievalReportListView 测试"""

    def test_empty_200(self):
        resp = self.client.get('/api/v1/analytics/eval/retrieval-reports/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 0

    def test_with_data(self):
        ds = GoldenDataset.objects.create(name='检索评估集', root_type='company_doc')
        RetrievalQualityReport.objects.create(
            dataset=ds, recall_at_5=0.5, recall_at_10=0.6, recall_at_20=0.7,
            mrr=0.4, ndcg_at_5=0.3, ndcg_at_10=0.35, total_questions=10,
            questions_with_hits=8, questions_without_hits=2, status='completed')
        resp = self.client.get('/api/v1/analytics/eval/retrieval-reports/',
                               **self.reader_headers)
        assert resp.status_code == 200
        row = resp.json()['rows'][0]
        assert row['dataset_id'] == ds.id
        assert row['recall_at_10'] == 0.6

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/eval/retrieval-reports/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]


# ============================================================================
# 文档质量（汇总 / 触发评估 / 报告列表）
# ============================================================================
class TestDocumentQualityAPI(AnalyticsViewsBase):
    """DocumentQualityReportView 测试"""

    def test_happy_200(self):
        # get_document_quality_summary 在视图内部导入，patch 模块级符号
        summary = {'total_docs': 2, 'avg_score': 88.0,
                   'score_distribution': {'excellent': 1, 'good': 1, 'fair': 0, 'poor': 0},
                   'common_issues': [{'type': 'too_short', 'count': 1}],
                   'recent_reports': []}
        with patch('apps.analytics.doc_quality.get_document_quality_summary',
                   return_value=summary) as m:
            resp = self.client.get(
                f'/api/v1/analytics/doc-quality/?start_date={self.yesterday}&end_date={self.today}',
                **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total_docs'] == 2
        assert data['dept_id'] == None
        assert data['team_id'] == None
        m.assert_called_once()

    def test_start_date_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/doc-quality/?start_date=invalid',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_end_date_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/doc-quality/?end_date=invalid',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/doc-quality/', **self.anon_headers)
        assert resp.status_code in [401, 403]


class TestRunDocQualityEvalAPI(AnalyticsViewsBase):
    """RunDocQualityEvalView 测试"""

    def test_document_id_happy_200(self):
        mock_report = MagicMock(id=1, quality_score=90.0)
        with patch('apps.analytics.doc_quality.evaluate_document_quality',
                   return_value=mock_report) as m:
            resp = self.client.post('/api/v1/analytics/doc-quality/evaluate/',
                                    data=json.dumps({'document_id': 1}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['report_id'] == 1
        assert resp.json()['score'] == 90.0
        m.assert_called_once_with(1)

    def test_document_id_invalid_400(self):
        resp = self.client.post('/api/v1/analytics/doc-quality/evaluate/',
                                data=json.dumps({'document_id': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_batch_happy_200(self):
        with patch('apps.analytics.doc_quality.batch_evaluate_document_quality',
                   return_value={'evaluated': 3, 'failed': 0}) as m:
            resp = self.client.post('/api/v1/analytics/doc-quality/evaluate/',
                                    data=json.dumps({'days': 3}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['summary']['evaluated'] == 3
        m.assert_called_once_with(days=3)

    def test_days_invalid_400(self):
        resp = self.client.post('/api/v1/analytics/doc-quality/evaluate/',
                                data=json.dumps({'days': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/doc-quality/evaluate/',
                                data=json.dumps({}), content_type='application/json',
                                **self.reader_headers)
        assert resp.status_code == 403


class TestDocumentQualityReportListAPI(AnalyticsViewsBase):
    """DocumentQualityReportListView 测试"""

    def _make_quality_report(self, **kw):
        doc = self._make_doc(file_name='质量文档.txt', dept_id=self.dept_a.id)
        defaults = dict(
            document=doc, quality_score=88.0, parse_status='success',
            text_extraction_rate=0.9, chunk_count=5, avg_chunk_chars=200,
            embedding_success_rate=1.0,
            quality_issues=[{'level': 'warning', 'type': 'too_short', 'detail': 'd'}],
            evaluated_at=timezone.now(),
        )
        defaults.update(kw)
        return DocumentQualityReport.objects.create(**defaults)

    def test_empty_200(self):
        resp = self.client.get('/api/v1/analytics/doc-quality/reports/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 0

    def test_with_data(self):
        self._make_quality_report()
        resp = self.client.get('/api/v1/analytics/doc-quality/reports/',
                               **self.reader_headers)
        assert resp.status_code == 200
        row = resp.json()['rows'][0]
        # 序列化器字段：document_name/quality_issues[:5]/evaluated_at
        assert row['document_name'] == '质量文档.txt'
        assert len(row['quality_issues']) == 1
        assert row['evaluated_at'] != ''

    def test_min_score_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/doc-quality/reports/?min_score=abc',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_min_score_filter(self):
        self._make_quality_report(quality_score=70.0)
        self._make_quality_report(quality_score=95.0)
        resp = self.client.get('/api/v1/analytics/doc-quality/reports/?min_score=90',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 1

    def test_org_filter(self):
        # dept_id 过滤：文档归属部门
        self._make_quality_report()
        resp = self.client.get(
            f'/api/v1/analytics/doc-quality/reports/?dept_id={self.dept_a.id}',
            **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 1
        assert resp.json()['dept_id'] == self.dept_a.id

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/doc-quality/reports/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]


# ============================================================================
# 多维度评分列表 / 手动评估派发
# ============================================================================
class TestMultiDimensionScoreAPI(AnalyticsViewsBase):
    """MultiDimensionScoreView 测试"""

    def test_empty_200(self):
        resp = self.client.get('/api/v1/analytics/multi-dim-scores/', **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 0
        assert data['dimension_summary'] == {}

    def test_with_data(self):
        qa = self._make_qa()
        self._make_score(qa, 'faithfulness', 0.8)
        self._make_score(qa, 'answer_relevancy', 0.6)
        resp = self.client.get('/api/v1/analytics/multi-dim-scores/', **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2
        assert data['dimension_summary']['faithfulness']['count'] == 1
        assert data['dimension_summary']['faithfulness']['avg_score'] == 0.8
        assert len(data['rows']) == 2

    def test_qa_record_id_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/multi-dim-scores/?qa_record_id=abc',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_qa_record_id_filter(self):
        qa1 = self._make_qa()
        qa2 = self._make_qa()
        self._make_score(qa1, 'faithfulness', 0.8)
        self._make_score(qa2, 'faithfulness', 0.9)
        resp = self.client.get(
            f'/api/v1/analytics/multi-dim-scores/?qa_record_id={qa1.id}',
            **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 1

    def test_dimension_filter(self):
        qa = self._make_qa()
        self._make_score(qa, 'faithfulness', 0.8)
        self._make_score(qa, 'toxicity', 0.2)
        resp = self.client.get('/api/v1/analytics/multi-dim-scores/?dimension=toxicity',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 1

    def test_start_date_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/multi-dim-scores/?start_date=invalid',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/multi-dim-scores/', **self.anon_headers)
        assert resp.status_code in [401, 403]


class TestRunMultiDimEvalAPI(AnalyticsViewsBase):
    """RunMultiDimEvalView 测试（异步派发 + 上下文预检）"""

    def test_missing_qa_id_400(self):
        resp = self.client.post('/api/v1/analytics/multi-dim-eval/',
                                data=json.dumps({}), content_type='application/json',
                                **self.writer_headers)
        assert resp.status_code == 400

    def test_invalid_qa_id_400(self):
        resp = self.client.post('/api/v1/analytics/multi-dim-eval/',
                                data=json.dumps({'qa_record_id': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_qa_not_found_404(self):
        resp = self.client.post('/api/v1/analytics/multi-dim-eval/',
                                data=json.dumps({'qa_record_id': 99999}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 404

    def test_no_context_400(self):
        # 无检索上下文：预检拦截，返回 400 不派发任务
        qa = self._make_qa()
        with patch('apps.analytics.production_eval._build_context_list',
                   return_value=[]):
            resp = self.client.post('/api/v1/analytics/multi-dim-eval/',
                                    data=json.dumps({'qa_record_id': qa.id}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_happy_200(self):
        qa = self._make_qa()
        with patch('apps.analytics.production_eval._build_context_list',
                   return_value=['检索上下文内容']):
            with patch('apps.analytics.production_eval.evaluate_sampled_qa') as m:
                resp = self.client.post('/api/v1/analytics/multi-dim-eval/',
                                        data=json.dumps({'qa_record_id': qa.id}),
                                        content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['queued']
        assert data['qa_id'] == qa.id
        assert data['eval_batch_id'].startswith('manual_')
        # 手动评估跳过日预算检查
        m.delay.assert_called_once()
        assert m.delay.call_args[1]['skip_budget_check'] == True

    def test_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/multi-dim-eval/',
                                data=json.dumps({'qa_record_id': 1}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403
