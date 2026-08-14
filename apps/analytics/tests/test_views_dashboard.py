"""
Analytics 评估看板 / 覆盖率与反馈闭环 / 低分归因分析视图测试

覆盖范围（与 views.py 逐视图对齐）：
- 评估看板 overview（total_evaluated / dimension_groups / 安全告警）
- 评估看板 trend（按天聚合 series / 维度过滤）
- 评估看板 low-score-qa（低分 QA 列表）
- 评估看板 qa-detail（QA 详情 + 多维度评分）
- 覆盖率报告（四个重型函数均 mock）与反馈闭环
- 覆盖率报告生成 / 列表 / 详情（DELETE） / Excel 导出
- 低分归因列表（count / category/layer/status/root_type 组合过滤 / 组织筛选）/ 详情 / 手动触发 / 统计

说明：
- 评估看板数据直接构造 MultiDimensionScore，不依赖重型后端调用
- 覆盖率 / 反馈闭环 / 报表生成 / LLM 归因 / Celery 任务等重型后端调用均在视图源码导入处 mock
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
    Department, Team,
)
from apps.chat.models import QaRecord, QaFeedback
from apps.memory.models import Session
from apps.knowledge.models import KnowledgeNode, Document
from apps.analytics.models import (
    MultiDimensionScore, LowScoreAnalysis, RouteAnalysis, WikiPageQualityScore,
    CoverageReport,
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
# 评估看板（overview / trend / low-score-qa / qa-detail）
# ============================================================================
class TestEvalDashboardOverviewAPI(AnalyticsViewsBase):
    """EvalDashboardOverviewView 测试"""

    def test_empty_200(self):
        # 无评估数据：返回空结构（total_evaluated=0 + display_dimensions 白名单）
        resp = self.client.get('/api/v1/analytics/eval-dashboard/overview/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total_evaluated'] == 0
        assert data['dimension_groups'] == {}
        assert 'display_dimensions' in data
        assert data['days'] == 7

    def test_with_data(self):
        # 有评估数据：dimension_groups 按 4 大类分组
        qa = self._make_qa(is_success=True, answer_type='rag')
        self._make_score(qa, 'faithfulness', 0.8)
        self._make_score(qa, 'answer_relevancy', 0.9)
        self._make_score(qa, 'toxicity', 0.1)
        resp = self.client.get('/api/v1/analytics/eval-dashboard/overview/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total_evaluated'] == 1
        assert data['total_qa'] == 1
        assert data['coverage_rate'] == 1.0
        # quality 分组含两个维度
        quality_dims = {d['name'] for d in data['dimension_groups']['quality']['dimensions']}
        assert 'faithfulness' in quality_dims
        # 安全告警：toxicity < 0.5 计数
        assert data['safety_alert_count'] == 1

    def test_days_boundary(self):
        # days 参数钳位到 1-90
        resp = self.client.get('/api/v1/analytics/eval-dashboard/overview/?days=999',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['days'] == 90

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/overview/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]


class TestEvalDashboardTrendAPI(AnalyticsViewsBase):
    """EvalDashboardTrendView 测试"""

    def test_empty_200(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/trend/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['dates'] == []
        assert data['series'] == []
        assert data['dimension'] == 'all'

    def test_with_data(self):
        # MultiDimensionScore 对 (qa_record, dimension) 唯一，同维度多条须用不同 qa
        qa1 = self._make_qa(is_success=True, answer_type='rag')
        qa2 = self._make_qa(is_success=True, answer_type='rag')
        self._make_score(qa1, 'faithfulness', 0.7)
        self._make_score(qa2, 'faithfulness', 0.9, status='pending')  # 同维度多条
        resp = self.client.get('/api/v1/analytics/eval-dashboard/trend/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data['dates']) == 1
        # 同维度按天聚合成一个 series
        assert len(data['series']) == 1
        assert data['series'][0]['dimension'] == 'faithfulness'

    def test_dimension_filter(self):
        qa = self._make_qa(is_success=True, answer_type='rag')
        self._make_score(qa, 'faithfulness', 0.7)
        self._make_score(qa, 'toxicity', 0.2)
        resp = self.client.get('/api/v1/analytics/eval-dashboard/trend/?dimension=toxicity',
                               **self.reader_headers)
        assert resp.status_code == 200
        series = resp.json()['series']
        assert len(series) == 1
        assert series[0]['dimension'] == 'toxicity'

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/trend/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]


class TestEvalDashboardLowScoreAPI(AnalyticsViewsBase):
    """EvalDashboardLowScoreView 测试"""

    def test_empty_200(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/low-score-qa/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 0
        assert resp.json()['threshold'] == 0.5

    def test_with_low_score(self):
        # 均分 < 0.5 的 QA 进入低分列表（按均分升序）
        qa = self._make_qa(is_success=True, answer_type='rag',
                           question='低分问题', answer='低分回答')
        self._make_score(qa, 'faithfulness', 0.3)
        self._make_score(qa, 'answer_relevancy', 0.2)
        resp = self.client.get('/api/v1/analytics/eval-dashboard/low-score-qa/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 1
        row = data['rows'][0]
        assert row['qa_record_id'] == qa.id
        assert row['avg_score'] < 0.5
        assert row['min_dimension'] == 'answer_relevancy'

    def test_high_score_excluded(self):
        # 均分 >= threshold 的 QA 不进入列表
        qa = self._make_qa(is_success=True, answer_type='rag')
        self._make_score(qa, 'faithfulness', 0.9)
        self._make_score(qa, 'answer_relevancy', 0.8)
        resp = self.client.get('/api/v1/analytics/eval-dashboard/low-score-qa/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 0

    def test_limit_invalid_default(self):
        # limit 非整数时兜底默认 20，不报错
        resp = self.client.get('/api/v1/analytics/eval-dashboard/low-score-qa/?limit=abc',
                               **self.reader_headers)
        assert resp.status_code == 200


class TestEvalDashboardQaDetailAPI(AnalyticsViewsBase):
    """EvalDashboardQaDetailView 测试"""

    def test_missing_qa_id_400(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/qa-detail/',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_invalid_qa_id_400(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id=abc',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_qa_not_found_404(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id=99999',
                               **self.reader_headers)
        assert resp.status_code == 404

    def test_happy_200(self):
        qa = self._make_qa(is_success=True, answer_type='rag',
                           question='详情问题', answer='详情回答',
                           retrieval_hits=[1, 2, 3])
        self._make_score(qa, 'faithfulness', 0.8)
        self._make_score(qa, 'toxicity', 0.1)
        resp = self.client.get(
            f'/api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id={qa.id}',
            **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['qa']['question'] == '详情问题'
        assert data['qa']['retrieval_hits'] == [1, 2, 3]
        assert len(data['scores']) == 2
        assert round(data['avg_score'], 4) == round((0.8 + 0.1) / 2, 4)


# ============================================================================
# 覆盖率报告 / 反馈闭环 / 报告 CRUD 与 Excel 导出
# ============================================================================
class TestCoverageReportAPI(AnalyticsViewsBase):
    """CoverageReportView 测试（四个重型函数均 mock）"""

    def test_days_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/coverage/?days=abc',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_happy_200(self):
        coverage = {'total_hot': 10, 'covered': 8, 'rate': 0.8}
        gaps = [{'query': '什么是社保', 'count': 5}]
        duplicates = {'rate': 0.1, 'count': 2}
        domain = {'domain_coverage': []}
        with patch('apps.analytics.services.coverage_service.analyze_hot_query_coverage',
                   return_value=coverage), \
             patch('apps.analytics.services.coverage_service.detect_knowledge_gaps',
                   return_value=gaps), \
             patch('apps.analytics.services.coverage_service.detect_duplicate_chunks',
                   return_value=duplicates), \
             patch('apps.analytics.services.coverage_service.analyze_domain_coverage',
                   return_value=domain):
            resp = self.client.get('/api/v1/analytics/coverage/?days=14',
                                   **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['coverage']['rate'] == 0.8
        assert data['gap_count'] == 1
        assert len(data['gaps']) == 1
        assert data['duplicates']['count'] == 2

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/coverage/', **self.anon_headers)
        assert resp.status_code in [401, 403]


class TestFeedbackLoopAPI(AnalyticsViewsBase):
    """FeedbackLoopView 测试"""

    def test_days_invalid_400(self):
        resp = self.client.post('/api/v1/analytics/feedback-loop/',
                                data=json.dumps({'days': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_happy_200(self):
        with patch('apps.analytics.services.coverage_service.auto_link_feedback_to_chunks',
                   return_value={'linked': 3, 'resolved': 1}) as m:
            resp = self.client.post('/api/v1/analytics/feedback-loop/',
                                    data=json.dumps({'days': 7}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['linked'] == 3
        m.assert_called_once_with(days=7)

    def test_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/feedback-loop/',
                                data=json.dumps({}), content_type='application/json',
                                **self.reader_headers)
        assert resp.status_code == 403


class TestGenerateCoverageReportAPI(AnalyticsViewsBase):
    """GenerateCoverageReportView 测试"""

    def test_days_invalid_400(self):
        resp = self.client.post('/api/v1/analytics/coverage/generate/',
                                data=json.dumps({'days': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_happy_200(self):
        mock_report = MagicMock(id=3, report_date=self.today,
                                hot_query_coverage_rate=0.85, gap_count=2)
        with patch('apps.analytics.services.coverage_service.generate_coverage_report',
                   return_value=mock_report) as m:
            resp = self.client.post('/api/v1/analytics/coverage/generate/',
                                    data=json.dumps({'days': 7}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok']
        assert data['report_id'] == 3
        assert data['coverage_rate'] == 0.85
        assert data['gap_count'] == 2
        m.assert_called_once_with(days=7)

    def test_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/coverage/generate/',
                                data=json.dumps({}), content_type='application/json',
                                **self.reader_headers)
        assert resp.status_code == 403


class TestCoverageReportListAPI(AnalyticsViewsBase):
    """CoverageReportListView 测试"""

    def _make_report(self, **kw):
        defaults = dict(report_date=self.today, total_hot_queries=10,
                        covered_queries=8, uncovered_queries=2,
                        hot_query_coverage_rate=0.8, gap_count=1,
                        duplicate_chunk_rate=0.1, duplicate_chunk_count=2,
                        feedback_loop_count=1, feedback_resolved_count=0)
        defaults.update(kw)
        return CoverageReport.objects.create(**defaults)

    def test_empty_200(self):
        resp = self.client.get('/api/v1/analytics/coverage/reports/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 0

    def test_with_data(self):
        self._make_report()
        resp = self.client.get('/api/v1/analytics/coverage/reports/',
                               **self.reader_headers)
        assert resp.status_code == 200
        row = resp.json()['rows'][0]
        assert row['total_hot_queries'] == 10
        assert row['hot_query_coverage_rate'] == 0.8

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/coverage/reports/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]


class TestCoverageReportDetailAPI(AnalyticsViewsBase):
    """CoverageReportDetailView 测试（DELETE）"""

    def test_delete_happy_200(self):
        report = CoverageReport.objects.create(report_date=self.today)
        resp = self.client.delete(f'/api/v1/analytics/coverage/reports/{report.id}/',
                                  **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['ok'] == True

    def test_delete_404(self):
        resp = self.client.delete('/api/v1/analytics/coverage/reports/99999/',
                                  **self.writer_headers)
        assert resp.status_code == 404

    def test_read_only_403(self):
        report = CoverageReport.objects.create(report_date=self.today)
        resp = self.client.delete(f'/api/v1/analytics/coverage/reports/{report.id}/',
                                  **self.reader_headers)
        assert resp.status_code == 403


class TestCoverageReportExportAPI(AnalyticsViewsBase):
    """CoverageReportExportView 测试（openpyxl 多 Sheet 导出）"""

    def test_export_happy_200(self):
        report = CoverageReport.objects.create(
            report_date=self.today,
            gap_queries=[{'query': '什么是社保', 'count': 3, 'suggestion': '补充文档'}],
            domain_coverage={
                'domain_coverage': [
                    {'name': '测试部门A', 'doc_count': 2, 'chunk_count': 10,
                     '占比': 0.5, 'query_hit_rate': 0.8, 'teams': []},
                ],
            },
        )
        resp = self.client.get(f'/api/v1/analytics/coverage/reports/{report.id}/export/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert 'spreadsheetml' in resp['Content-Type']
        assert 'attachment' in resp['Content-Disposition']
        assert len(resp.content) > 1000

    def test_export_404(self):
        resp = self.client.get('/api/v1/analytics/coverage/reports/99999/export/',
                               **self.reader_headers)
        assert resp.status_code == 404

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/coverage/reports/1/export/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]


# ============================================================================
# 低分归因分析（列表 / 详情 / 手动触发 / 统计）
# ============================================================================
class TestLowScoreAnalysisAPI(AnalyticsViewsBase):
    """LowScoreAnalysis 系列视图测试"""

    def test_list_empty_200(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 0

    def test_list_with_data(self):
        qa = self._make_qa()
        self._make_low_analysis(qa)
        resp = self.client.get('/api/v1/analytics/low-score-analysis/',
                               **self.reader_headers)
        assert resp.status_code == 200
        row = resp.json()['rows'][0]
        # 序列化器字段：question/answer/root_type/category_label 等
        assert row['qa_record_id'] == qa.id
        assert row['question'] == qa.question[:80]
        assert row['category_label'] == '检索召回不足'
        assert row['layer_label'] == '检索层'
        assert row['method_label'] == '规则归因'
        assert row['status_label'] == '已完成'

    def test_list_days_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/?days=abc',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_list_limit_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/?limit=abc',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_list_filters(self):
        # category / layer / status / root_type 组合过滤
        qa = self._make_qa(root_type='test_root')
        self._make_low_analysis(qa, root_cause_category='content_gap',
                                affected_layer='content', status='completed')
        resp = self.client.get(
            '/api/v1/analytics/low-score-analysis/?category=content_gap&layer=content&status=completed&root_type=test_root',
            **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 1

    def test_list_org_filter(self):
        # dept_id 组织筛选：按 QaRecord.user.department 过滤
        qa = self._make_qa()
        self._make_low_analysis(qa)
        # normal_user 无部门归属 → dept 过滤后为空
        resp = self.client.get(f'/api/v1/analytics/low-score-analysis/?dept_id={self.dept_a.id}',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 0

    def test_detail_missing_qa_id_400(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/detail/',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_detail_invalid_400(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/detail/?qa_record_id=abc',
                               **self.reader_headers)
        assert resp.status_code == 400

    def test_detail_404(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/detail/?qa_record_id=99999',
                               **self.reader_headers)
        assert resp.status_code == 404

    def test_detail_happy_200(self):
        qa = self._make_qa(question='完整问题内容', answer='完整回答内容')
        self._make_low_analysis(qa)
        resp = self.client.get(
            f'/api/v1/analytics/low-score-analysis/detail/?qa_record_id={qa.id}',
            **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        # 详情接口补充完整对话
        assert data['full_question'] == '完整问题内容'
        assert data['full_answer'] == '完整回答内容'
        assert 'suggestions' in data

    def test_run_missing_qa_id_400(self):
        resp = self.client.post('/api/v1/analytics/low-score-analysis/run/',
                                data=json.dumps({}), content_type='application/json',
                                **self.writer_headers)
        assert resp.status_code == 400

    def test_run_invalid_qa_id_400(self):
        resp = self.client.post('/api/v1/analytics/low-score-analysis/run/',
                                data=json.dumps({'qa_record_id': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_run_no_scores_400(self):
        # 预检：无 MultiDimensionScore 的 QA 无法归因
        qa = self._make_qa()
        resp = self.client.post('/api/v1/analytics/low-score-analysis/run/',
                                data=json.dumps({'qa_record_id': qa.id}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_run_happy_200(self):
        qa = self._make_qa()
        self._make_score(qa, 'faithfulness', 0.3)
        with patch('apps.analytics.tasks.run_low_score_analysis') as m:
            resp = self.client.post('/api/v1/analytics/low-score-analysis/run/',
                                    data=json.dumps({'qa_record_id': qa.id}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['queued']
        assert data['qa_id'] == qa.id
        # 手动归因跳过日预算
        m.delay.assert_called_once_with(qa.id, threshold=None, skip_budget_check=True)

    def test_run_threshold_invalid_400(self):
        qa = self._make_qa()
        self._make_score(qa, 'faithfulness', 0.3)
        resp = self.client.post('/api/v1/analytics/low-score-analysis/run/',
                                data=json.dumps({'qa_record_id': qa.id, 'threshold': 'abc'}),
                                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400

    def test_run_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/low-score-analysis/run/',
                                data=json.dumps({'qa_record_id': 1}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403

    def test_stats_empty_200(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/stats/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 0
        assert data['by_category'] == []
        assert data['by_layer'] == []
        assert data['by_method'] == {'rule': 0, 'llm': 0, 'hybrid': 0}

    def test_stats_with_data(self):
        qa1 = self._make_qa()
        qa2 = self._make_qa()
        self._make_low_analysis(qa1, avg_score=0.3, analysis_method='rule')
        self._make_low_analysis(qa2, avg_score=0.4, analysis_method='llm')
        resp = self.client.get('/api/v1/analytics/low-score-analysis/stats/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2
        assert data['by_category'][0]['count'] == 2
        assert data['by_method']['rule'] == 1
        assert data['by_method']['llm'] == 1

    def test_stats_only_completed(self):
        # 仅统计 status=completed 的记录（LowScoreAnalysis 对 qa_record 唯一，两条须用不同 qa）
        qa1 = self._make_qa()
        qa2 = self._make_qa()
        self._make_low_analysis(qa1, status='completed')
        self._make_low_analysis(qa2, status='pending', avg_score=0.2)
        resp = self.client.get('/api/v1/analytics/low-score-analysis/stats/',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 1

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/low-score-analysis/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]


# ============================================================================
# 路由分析看板（四层命中率 + 各维均分对比）
# ============================================================================
class TestRouteAnalysisDashboardAPI(AnalyticsViewsBase):
    """RouteAnalysisDashboardView 测试"""

    def _make_route(self, qa, route_source, confidence=0.8, latency=150,
                    answer_quality=None):
        """创建一条 RouteAnalysis（qa_created_at 默认当前时间，落在窗口内）"""
        return RouteAnalysis.objects.create(
            qa_record_id=qa.id,
            question=qa.question,
            route_source=route_source,
            confidence=confidence,
            route_trace=[{'layer': route_source, 'confidence': confidence, 'latency_ms': 10}],
            latency_ms=latency,
            answer_quality=answer_quality,
            qa_created_at=timezone.now(),
        )

    def test_empty_200(self):
        """无数据 → 200 空结构（coverage/quality 为空,前端渲染空态）"""
        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 0
        assert data['coverage_by_route'] == []
        assert data['quality_by_route'] == {}
        assert data['daily_trend'] == []
        assert data['route_order'] == ['wiki', 'graphrag_local', 'graphrag_global', 'rag']

    def test_coverage_and_quality(self):
        """有数据 → 四层命中率 + 各层 12 维均分对比"""
        qa_wiki = self._make_qa(question='wiki 问题')
        qa_rag = self._make_qa(question='rag 问题')
        self._make_route(qa_wiki, 'wiki', confidence=0.9, answer_quality=0.8)
        self._make_route(qa_rag, 'rag', confidence=0.5, answer_quality=0.6)
        # wiki 层该 QA 已有评估分（影响 quality_by_route）
        self._make_score(qa_wiki, 'faithfulness', 0.9)
        self._make_score(qa_wiki, 'toxicity', 0.7)

        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2

        cov = {r['route']: r for r in data['coverage_by_route']}
        assert cov['wiki']['count'] == 1
        assert cov['wiki']['avg_confidence'] == 0.9
        assert cov['wiki']['avg_answer_quality'] == 0.8
        assert cov['rag']['count'] == 1
        # 命中率各占一半
        assert cov['wiki']['share'] == 0.5

        # 仅 wiki 层有评估分 → rag 层为空结构
        qb = data['quality_by_route']
        assert qb['wiki']['overall'] == 0.8  # (0.9+0.7)/2
        assert qb['wiki']['groups']['quality'] == 0.9
        assert qb['rag']['overall'] is None

        # 按天趋势含今天一行,wiki/rag 各 1
        assert data['daily_trend'] and data['daily_trend'][-1]['wiki'] == 1
        assert data['daily_trend'][-1]['rag'] == 1

    def test_days_window_filters_by_qa_created_at(self):
        """时间窗口按 qa_created_at(提问时间)过滤,聚合时间不影响窗口"""
        qa_old = self._make_qa(question='老问题')
        self._make_route(qa_old, 'rag')
        # 改写到 8 天前 → 超出 7 天窗口
        RouteAnalysis.objects.filter(qa_record_id=qa_old.id).update(
            qa_created_at=timezone.now() - timedelta(days=8))

        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/?days=7',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 0

    def test_org_filter(self):
        """组织筛选：按提问用户归属过滤命中"""
        qa_a = self._make_qa(user=self.normal_user)  # normal_user 无部门
        # 为 normal_user 挂部门后,部门筛选项应命中
        self.normal_user.department = self.dept_a
        self.normal_user.save()
        self._make_route(qa_a, 'wiki')

        resp = self.client.get(
            f'/api/v1/analytics/eval-dashboard/route-analysis/?dept_id={self.dept_a.id}',
            **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 1

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/',
                               **self.anon_headers)
        assert resp.status_code in [401, 403]

    def test_query_transform_stats(self):
        """改写命中率：从 QaRecord.route_trace 实时聚合 rewrite/decompose 统计"""
        # 2 条改写链路：1 次实际改写 changed、1 次未改写；其中 1 条触发分解
        self._make_qa(question='年假怎么申请', route_trace=[
            {'layer': 'query_rewrite', 'query': '年假怎么申请',
             'rewritten_query': '公司年假申请流程', 'changed': True, 'latency_ms': 120},
            {'layer': 'query_decompose', 'query': '年假怎么申请',
             'sub_queries': ['年假规则', '请假天数'], 'need_decompose': True,
             'decomposed': True, 'latency_ms': 80},
        ])
        self._make_qa(question='报销流程', route_trace=[
            {'layer': 'query_rewrite', 'query': '报销流程',
             'rewritten_query': '报销流程', 'changed': False, 'latency_ms': 60},
        ])
        # 普通 QA（无改写链路）不影响统计
        self._make_qa(question='普通问题')

        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/',
                               **self.reader_headers)
        assert resp.status_code == 200
        stats = resp.json()['query_transform_stats']
        assert stats['rewrite_total'] == 2
        assert stats['rewrite_changed'] == 1
        assert stats['rewrite_hit_rate'] == 0.5
        assert stats['decompose_total'] == 1

    def test_query_transform_stats_empty(self):
        """无改写链路 → 全 0（前端可渲染空态）"""
        self._make_qa(question='普通问题')
        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/',
                               **self.reader_headers)
        stats = resp.json()['query_transform_stats']
        assert stats == {
            'rewrite_total': 0, 'rewrite_changed': 0,
            'rewrite_hit_rate': 0.0, 'decompose_total': 0,
        }

    def test_personalization_stats(self):
        """个性化命中率：从 QaRecord.route_trace 聚合 applied/reordered/hit/cold_start"""
        # 生效 2 条：1 条重排且有画像命中、1 条未重排；另 1 条冷启动被跳过
        self._make_qa(question='年假怎么申请', route_trace=[
            {'layer': 'personalization', 'enabled': True, 'applied': True,
             'cold_start': False, 'weight': 0.1, 'adjusted_count': 2,
             'reordered': True, 'top_personalized': True,
             'personalized_hits': 3, 'latency_ms': 5},
        ])
        self._make_qa(question='报销流程', route_trace=[
            {'layer': 'personalization', 'enabled': True, 'applied': True,
             'cold_start': False, 'weight': 0.1, 'adjusted_count': 0,
             'reordered': False, 'top_personalized': False,
             'personalized_hits': 0, 'latency_ms': 3},
        ])
        self._make_qa(question='新员工问题', route_trace=[
            {'layer': 'personalization', 'enabled': True, 'applied': False,
             'cold_start': True, 'latency_ms': 0},
        ])
        # 普通 QA（无个性化链路）不影响统计
        self._make_qa(question='普通问题')

        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/',
                               **self.reader_headers)
        assert resp.status_code == 200
        stats = resp.json()['personalization_stats']
        assert stats['personalized_total'] == 2
        assert stats['cold_start_count'] == 1
        assert stats['adjusted_count'] == 1
        assert stats['adjust_rate'] == 0.5
        assert stats['hit_count'] == 1
        assert stats['personalized_hit_rate'] == 0.5

    def test_personalization_stats_empty(self):
        """无个性化链路（开关关闭）→ 全 0（前端可渲染空态）"""
        self._make_qa(question='普通问题')
        resp = self.client.get('/api/v1/analytics/eval-dashboard/route-analysis/',
                               **self.reader_headers)
        stats = resp.json()['personalization_stats']
        assert stats == {
            'personalized_total': 0, 'cold_start_count': 0,
            'adjusted_count': 0, 'adjust_rate': 0.0,
            'hit_count': 0, 'personalized_hit_rate': 0.0,
        }


class TestRouteAnalysisAggregateAPI(AnalyticsViewsBase):
    """RouteAnalysisAggregateView 手动触发聚合"""

    def test_post_queued_200(self):
        """正常派发 → 200 queued,透传 report_date"""
        with patch('apps.analytics.tasks.aggregate_route_analysis_daily') as m:
            resp = self.client.post(
                '/api/v1/analytics/route-analysis/aggregate/',
                data=json.dumps({'report_date': '2026-08-06'}),
                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['queued'] is True
        assert data['report_date'] == '2026-08-06'
        m.delay.assert_called_once_with('2026-08-06')

    def test_post_no_date_aggregates_yesterday(self):
        """不传日期 → 聚合昨天"""
        with patch('apps.analytics.tasks.aggregate_route_analysis_daily') as m:
            resp = self.client.post('/api/v1/analytics/route-analysis/aggregate/',
                                    data=json.dumps({}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['report_date'] == 'yesterday'
        m.delay.assert_called_once_with(None)

    def test_post_invalid_date_400(self):
        """非法日期格式 → 400 不派发"""
        with patch('apps.analytics.tasks.aggregate_route_analysis_daily') as m:
            resp = self.client.post(
                '/api/v1/analytics/route-analysis/aggregate/',
                data=json.dumps({'report_date': '2026/08/06'}),
                content_type='application/json', **self.writer_headers)
        assert resp.status_code == 400
        m.delay.assert_not_called()

    def test_post_read_only_403(self):
        """只读权限无写入权限 → 403"""
        resp = self.client.post('/api/v1/analytics/route-analysis/aggregate/',
                                data=json.dumps({}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403


# ============================================================================
# Wiki 页面质量（忠实度/完整性评估结果）
# ============================================================================
class TestWikiQualityAPI(AnalyticsViewsBase):
    """WikiQualityListView / WikiQualityEvaluateView 测试"""

    def _make_page(self, title='测试Wiki页'):
        """创建已发布 Wiki 页面"""
        from apps.wiki.models import WikiPage
        return WikiPage.objects.create(
            title=title, node=self.root_node, status='published', content='正文')

    def _make_score(self, page, dimension='faithfulness', score=0.9, **kw):
        """创建一条 WikiPageQualityScore"""
        defaults = dict(page=page, dimension=dimension, score=score,
                        status='completed', reason='评估理由')
        defaults.update(kw)
        return WikiPageQualityScore.objects.create(**defaults)

    def test_list_empty_200(self):
        resp = self.client.get('/api/v1/analytics/wiki-quality/', **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 0
        assert data['summary']['pages_evaluated'] == 0

    def test_list_with_data(self):
        """列表：summary 均分 + 页面粒度两维分数"""
        page = self._make_page(title='页面A')
        self._make_score(page, 'faithfulness', 0.9)
        self._make_score(page, 'completeness', 0.7)

        resp = self.client.get('/api/v1/analytics/wiki-quality/', **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 1
        assert data['summary']['pages_evaluated'] == 1
        assert data['summary']['avg_faithfulness'] == 0.9
        assert data['summary']['avg_completeness'] == 0.7
        row = data['rows'][0]
        assert row['title'] == '页面A'
        assert row['scores']['faithfulness']['score'] == 0.9
        assert row['scores']['completeness']['score'] == 0.7

    def test_list_failed_status(self):
        """失败记录计入 failed_pages,按 status 过滤生效"""
        page = self._make_page(title='失败页')
        self._make_score(page, 'faithfulness', status='failed', error_message='llm err')
        self._make_score(page, 'completeness', 0.8)

        resp = self.client.get('/api/v1/analytics/wiki-quality/?status=failed',
                               **self.reader_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['summary']['failed_pages'] == 1
        assert data['rows'][0]['scores']['faithfulness']['status'] == 'failed'

    def test_list_page_id_detail(self):
        """page_id 精确查询（详情弹窗用,不受分页影响）"""
        page = self._make_page(title='详情页')
        self._make_score(page, 'faithfulness', 0.85)
        resp = self.client.get(f'/api/v1/analytics/wiki-quality/?page_id={page.id}',
                               **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] == 1

    def test_evaluate_queued_200(self):
        """手动触发批量评估 → 200 queued"""
        with patch('apps.analytics.tasks.batch_evaluate_wiki_quality') as m:
            resp = self.client.post('/api/v1/analytics/wiki-quality/evaluate/',
                                    data=json.dumps({'days': 3, 'limit': 5}),
                                    content_type='application/json', **self.writer_headers)
        assert resp.status_code == 200
        assert resp.json()['queued'] is True
        m.delay.assert_called_once_with(days=3, limit=5)

    def test_evaluate_read_only_403(self):
        resp = self.client.post('/api/v1/analytics/wiki-quality/evaluate/',
                                data=json.dumps({}),
                                content_type='application/json', **self.reader_headers)
        assert resp.status_code == 403

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/wiki-quality/', **self.anon_headers)
        assert resp.status_code in [401, 403]
