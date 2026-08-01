"""
Analytics API 端点集成测试

覆盖范围：
- 12 个 API 视图的状态码验证 (200/400/401/403)
- 参数校验（非法日期、负数 page、超大 days）
- 权限控制（匿名/普通用户/管理员）
- 数据结构验证（返回字段完整性）
- 忠实度评估任务集成测试
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import (
    User, Role, UserRoleRel, RolePermissionRel, Permission, GrantStatus,
)
from apps.chat.models import QaRecord, QaFeedback
from apps.memory.models import Session
from apps.analytics.models import (
    SystemMetricsReport, OrgUsageReport, QueueDepthLog, AnswerQualityReport,
    KeywordWeight,
)
from apps.knowledge.models import KnowledgeNode


def _create_test_user(username='testuser', password='testpass123',
                      is_super_admin=False, perms=None):
    """创建测试用户并分配权限

    - Role.code → Role.role_key；Permission.code → Permission.permission_key
    - UserRole → UserRoleRel；RolePermission → RolePermissionRel
    """
    user = User.objects.create_user(
        username=username, password=password,
        email=f'{username}@test.com',
    )
    if is_super_admin:
        # is_super_admin 基于 role_key='super_admin' 的属性判定
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
    """获取 JWT token"""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


class AnalyticsAPITestBase(TestCase):
    """测试基类：初始化用户和测试数据"""

    def setUp(self):
        self.client = Client()
        self.today = timezone.now().date()
        self.yesterday = self.today - timedelta(days=1)

        # --- 用户 ---
        self.normal_user = _create_test_user(
            username='normal', password='pass12345', is_super_admin=False)
        self.super_admin = _create_test_user(
            username='admin', password='admin12345', is_super_admin=True)
        self.system_reader = _create_test_user(
            username='sys_reader', password='pass12345',
            perms=['analytics.system.read'])
        self.system_writer = _create_test_user(
            username='sys_writer', password='pass12345',
            perms=['analytics.system.read', 'analytics.system.write'])
        self.org_reader = _create_test_user(
            username='org_reader', password='pass12345',
            perms=['analytics.org.read'])

        # --- 认证头 ---
        self.anon_headers = {}
        self.normal_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.admin_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin)}'}
        self.reader_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.system_reader)}'}
        self.writer_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.system_writer)}'}
        self.org_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.org_reader)}'}

        # --- 准备 QA 数据 ---
        self._create_test_qa_data()

        # --- 准备报表数据 ---
        self._create_test_reports()

    def _create_test_qa_data(self):
        """创建测试用 QaRecord 数据"""
        KnowledgeNode.objects.create(
            name='test_root', node_type='root', root_type='test_root',
            created_by=self.super_admin)

        session = Session.objects.create(
            user=self.normal_user, root_type='test_root', title='Test Session')

        self.qa_records = []
        for i in range(15):
            record = QaRecord.objects.create(
                session=session, user=self.normal_user,
                question=f'测试问题 {i}', answer=f'测试回答 {i}',
                answer_type='rag', root_type='test_root',
                is_hit_cache=(i % 3 == 0),
                is_success=(i % 5 != 0),
                error_type='timeout' if i % 5 == 0 else '',
                tokens_prompt=100 + i * 10,
                tokens_completion=50 + i * 5,
                cost_estimate=Decimal(f'{0.01 * (i + 1):.4f}'),
                latency_total_ms=500 + i * 50,
                latency_llm_ms=300 + i * 30,
                latency_retrieval_ms=100 + i * 10,
                latency_ttfb_ms=200 + i * 20,
                tokens_per_second=round(10.0 + i * 0.5, 2),
                created_at=timezone.now() - timedelta(days=i),
            )
            self.qa_records.append(record)

            QaFeedback.objects.create(
                qa_record=record,
                user=self.normal_user,
                rating=1 if i % 3 != 1 else -1,
                comment=f'反馈 {i}',
            )

        # 关键词权重
        for kw_data in [
            {'keyword': '测试关键词', 'weight_score': 1.5, 'hit_count': 10,
             'good_feedback': 8, 'bad_feedback': 2, 'root_type': 'test_root'},
            {'keyword': '另一个词', 'weight_score': 0.8, 'hit_count': 5,
             'good_feedback': 3, 'bad_feedback': 2, 'root_type': 'test_root'},
        ]:
            KeywordWeight.objects.create(**kw_data)

    def _create_test_reports(self):
        """创建预计算报表"""
        SystemMetricsReport.objects.create(
            report_date=self.yesterday,
            total_qa=100, cache_hit_count=30, normal_qa_count=70,
            p50_latency_total=400, p95_latency_total=800, p99_latency_total=1200,
            p50_latency_llm=300, p95_latency_llm=600,
            p50_latency_retrieval=100, p95_latency_retrieval=200,
            p50_ttfb=200, p95_ttfb=400,
            cache_hit_p50_latency=50, cache_hit_p95_latency=100,
            cache_hit_rate=0.3, llm_success_rate=0.95,
            llm_timeout_rate=0.02, embedding_error_rate=0.01,
            avg_tokens_per_second=12.5,
            total_tokens_prompt=50000, total_tokens_completion=25000,
            total_cost=Decimal('50.000000'),
            latency_histogram={'0-100': 10, '100-200': 30, '200-300': 60},
            error_distribution={'timeout': 2, 'network': 1},
        )

        OrgUsageReport.objects.create(
            report_date=self.yesterday,
            department_id=1, department_name='测试部门',
            team_id=-1, team_name='',
            qa_count=100, user_count=10,
            total_tokens=75000, total_cost=Decimal('50.000000'),
            avg_latency_ms=400, p95_latency_ms=800,
            good_feedback_rate=0.85,
            cache_hit_count=30, cache_hit_rate=0.3,
        )

        QueueDepthLog.objects.create(
            queue_name='default', depth=5, worker_count=2,
            minute_bucket=timezone.now().replace(second=0, microsecond=0))

        # 为一个非缓存 + 成功的 QA 记录创建质量报告
        clean_record = QaRecord.objects.filter(is_success=True, is_hit_cache=False).first()
        if clean_record:
            AnswerQualityReport.objects.create(
                qa_record=clean_record,
                status='completed', faithfulness_score=0.85,
                faithfulness_reason='回答忠实于原文',
                eval_model='deepseek-chat', eval_tokens_used=100,
                eval_cost=Decimal('0.050000'), eval_latency_ms=2000,
            )


class TestKeywordWeightAPI(AnalyticsAPITestBase):
    """关键词权重 API 测试"""

    def test_list_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/keywords/', **self.anon_headers)
        self.assertIn(resp.status_code, [401, 403])

    def test_list_normal_user_403(self):
        resp = self.client.get('/api/v1/analytics/keywords/', **self.normal_headers)
        self.assertEqual(resp.status_code, 403)

    def test_list_with_read_perm_200(self):
        resp = self.client.get('/api/v1/analytics/keywords/', **self.reader_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('rows', data)
        self.assertIn('count', data)

    def test_list_with_filter(self):
        resp = self.client.get('/api/v1/analytics/keywords/?root_type=test_root',
                              **self.reader_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.json()['count'], 0)

    def test_update_with_write_perm_200(self):
        kw = KeywordWeight.objects.first()
        resp = self.client.put(f'/api/v1/analytics/keywords/{kw.id}/',
                               data=json.dumps({'delta': 0.1}),
                               content_type='application/json',
                               **self.writer_headers)
        self.assertEqual(resp.status_code, 200)

    def test_update_with_read_perm_403(self):
        kw = KeywordWeight.objects.first()
        resp = self.client.put(f'/api/v1/analytics/keywords/{kw.id}/',
                               data=json.dumps({'delta': 0.1}),
                               content_type='application/json',
                               **self.reader_headers)
        self.assertEqual(resp.status_code, 403)

    def test_update_nonexistent_404(self):
        resp = self.client.put('/api/v1/analytics/keywords/99999/',
                               data=json.dumps({'delta': 0.1}),
                               content_type='application/json',
                               **self.writer_headers)
        self.assertEqual(resp.status_code, 404)


class TestBadFeedbackAPI(AnalyticsAPITestBase):
    """差评反馈 API 测试"""

    def test_list_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/bad-feedbacks/', **self.anon_headers)
        self.assertIn(resp.status_code, [401, 403])

    def test_list_with_read_perm_200(self):
        resp = self.client.get('/api/v1/analytics/bad-feedbacks/', **self.reader_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('rows', resp.json())

    def test_list_with_filter(self):
        resp = self.client.get('/api/v1/analytics/bad-feedbacks/?root_type=test_root',
                              **self.reader_headers)
        self.assertEqual(resp.status_code, 200)

    def test_update_status_200(self):
        fb = QaFeedback.objects.filter(rating__lt=0).first()
        if not fb:
            self.skipTest('No negative feedback available')
        resp = self.client.put(f'/api/v1/analytics/bad-feedbacks/{fb.id}/',
                               data=json.dumps({'status': 'resolved'}),
                               content_type='application/json',
                               **self.writer_headers)
        self.assertEqual(resp.status_code, 200)

    def test_update_invalid_status_400(self):
        fb = QaFeedback.objects.first()
        resp = self.client.put(f'/api/v1/analytics/bad-feedbacks/{fb.id}/',
                               data=json.dumps({'status': 'invalid_status'}),
                               content_type='application/json',
                               **self.writer_headers)
        self.assertEqual(resp.status_code, 400)


class TestOverviewAPI(AnalyticsAPITestBase):
    """概览统计 API 测试"""

    def test_overview_anonymous_401(self):
        resp = self.client.get('/api/v1/analytics/overview/', **self.anon_headers)
        self.assertIn(resp.status_code, [401, 403])

    def test_overview_authenticated_200(self):
        resp = self.client.get('/api/v1/analytics/overview/', **self.normal_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for key in ['total_qa', 'accuracy', 'avg_latency_ms', 'active_users']:
            self.assertIn(key, data)

    def test_overview_with_filter(self):
        resp = self.client.get('/api/v1/analytics/overview/?root_type=test_root',
                              **self.normal_headers)
        self.assertEqual(resp.status_code, 200)


class TestTrendAPI(AnalyticsAPITestBase):
    """趋势图 API 测试"""

    def test_trend_default_200(self):
        resp = self.client.get('/api/v1/analytics/trend/', **self.normal_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('trend', data)

    def test_trend_custom_days_200(self):
        resp = self.client.get('/api/v1/analytics/trend/?days=3', **self.normal_headers)
        self.assertEqual(resp.status_code, 200)

    def test_trend_invalid_days_400(self):
        resp = self.client.get('/api/v1/analytics/trend/?days=abc', **self.normal_headers)
        self.assertEqual(resp.status_code, 400)

    def test_trend_days_out_of_range_400(self):
        resp = self.client.get('/api/v1/analytics/trend/?days=999', **self.normal_headers)
        self.assertEqual(resp.status_code, 400)

    def test_trend_custom_date_range_200(self):
        start = self.yesterday - timedelta(days=3)
        resp = self.client.get(
            f'/api/v1/analytics/trend/?start_date={start}&end_date={self.yesterday}',
            **self.normal_headers)
        self.assertEqual(resp.status_code, 200)

    def test_trend_invalid_date_400(self):
        resp = self.client.get(
            '/api/v1/analytics/trend/?start_date=invalid&end_date=2024-01-01',
            **self.normal_headers)
        self.assertEqual(resp.status_code, 400)

    def test_trend_start_after_end_400(self):
        resp = self.client.get(
            '/api/v1/analytics/trend/?start_date=2024-01-15&end_date=2024-01-01',
            **self.normal_headers)
        self.assertEqual(resp.status_code, 400)

    def test_trend_max_range_400(self):
        resp = self.client.get(
            '/api/v1/analytics/trend/?start_date=2023-01-01&end_date=2024-12-31',
            **self.normal_headers)
        self.assertEqual(resp.status_code, 400)


class TestQaRecordAPI(AnalyticsAPITestBase):
    """QA 记录 API 测试"""

    def test_list_authenticated_200(self):
        resp = self.client.get('/api/v1/analytics/qa-records/', **self.normal_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('total', data)
        self.assertIn('rows', data)

    def test_list_pagination(self):
        resp = self.client.get('/api/v1/analytics/qa-records/?page=1&page_size=5',
                              **self.normal_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['rows']), 5)

    def test_list_invalid_page_400(self):
        resp = self.client.get('/api/v1/analytics/qa-records/?page=abc',
                              **self.normal_headers)
        self.assertEqual(resp.status_code, 400)

    def test_list_invalid_date_400(self):
        resp = self.client.get('/api/v1/analytics/qa-records/?start_date=invalid',
                              **self.normal_headers)
        self.assertEqual(resp.status_code, 400)


class TestSystemMetricsAPI(AnalyticsAPITestBase):
    """系统指标 API 测试"""

    def test_get_with_read_perm_200(self):
        resp = self.client.get(
            f'/api/v1/analytics/system-metrics/?date={self.yesterday}',
            **self.reader_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['available'])
        for key in ['total_qa', 'cache_hit_rate', 'llm_success_rate',
                     'latency_histogram', 'error_distribution']:
            self.assertIn(key, data)

    def test_get_no_report_unavailable(self):
        resp = self.client.get(
            '/api/v1/analytics/system-metrics/?date=2020-01-01',
            **self.reader_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['available'])

    def test_get_invalid_date_400(self):
        resp = self.client.get(
            '/api/v1/analytics/system-metrics/?date=invalid',
            **self.reader_headers)
        self.assertEqual(resp.status_code, 400)


class TestOrgUsageAPI(AnalyticsAPITestBase):
    """组织使用报表 API 测试"""

    def test_get_with_org_perm_200(self):
        resp = self.client.get(
            f'/api/v1/analytics/org-usage/?date={self.yesterday}',
            **self.org_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('rows', data)

    def test_get_with_system_perm_403(self):
        resp = self.client.get(
            f'/api/v1/analytics/org-usage/?date={self.yesterday}',
            **self.reader_headers)
        self.assertEqual(resp.status_code, 403)

    def test_get_invalid_date_400(self):
        resp = self.client.get(
            '/api/v1/analytics/org-usage/?date=invalid',
            **self.org_headers)
        self.assertEqual(resp.status_code, 400)

    def test_get_invalid_dept_id_400(self):
        resp = self.client.get(
            f'/api/v1/analytics/org-usage/?date={self.yesterday}&department_id=abc',
            **self.org_headers)
        self.assertEqual(resp.status_code, 400)


class TestQualityReportAPI(AnalyticsAPITestBase):
    """质量报告 API 测试"""

    def test_list_with_read_perm_200(self):
        resp = self.client.get('/api/v1/analytics/quality-reports/', **self.reader_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('rows', data)
        self.assertIn('summary', data)

    def test_list_invalid_date_400(self):
        resp = self.client.get(
            '/api/v1/analytics/quality-reports/?start_date=invalid',
            **self.reader_headers)
        self.assertEqual(resp.status_code, 400)


class TestQueueDepthAPI(AnalyticsAPITestBase):
    """队列深度 API 测试"""

    def test_get_super_admin_200(self):
        resp = self.client.get('/api/v1/analytics/queue-depth/', **self.admin_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('queues', data)


class TestRealtimeSnapshotAPI(AnalyticsAPITestBase):
    """实时快照 API 测试"""

    def test_get_super_admin_200(self):
        resp = self.client.get('/api/v1/analytics/realtime/', **self.admin_headers)
        self.assertIn(resp.status_code, [200, 503])

    def test_get_normal_user_403(self):
        resp = self.client.get('/api/v1/analytics/realtime/', **self.normal_headers)
        self.assertEqual(resp.status_code, 403)


class TestDailyReportAPI(AnalyticsAPITestBase):
    """日报 API 测试"""

    def test_get_authenticated_200(self):
        resp = self.client.get('/api/v1/analytics/daily/', **self.normal_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('today', data)
        self.assertIn('yesterday', data)


class TestFaithfulnessEvaluation(AnalyticsAPITestBase):
    """忠实度评估任务集成测试"""

    def test_parse_faithfulness_result_valid(self):
        """测试 LLM 输出解析"""
        from apps.analytics.utils import parse_faithfulness_result

        valid_output = '{"score": 0.85, "reason": "回答忠实于原文"}'
        score, reason = parse_faithfulness_result(valid_output)
        self.assertEqual(score, 0.85)
        self.assertEqual(reason, '回答忠实于原文')

    def test_parse_faithfulness_result_invalid(self):
        """测试无效 LLM 输出的容错"""
        from apps.analytics.utils import parse_faithfulness_result

        invalid_output = 'I dont understand'
        score, reason = parse_faithfulness_result(invalid_output)
        self.assertEqual(score, 0.0)
        self.assertIn('解析失败', reason)

    def test_parse_faithfulness_result_score_clamping(self):
        """测试分数钳位"""
        from apps.analytics.utils import parse_faithfulness_result

        too_high = '{"score": 1.5, "reason": ""}'
        score, _ = parse_faithfulness_result(too_high)
        self.assertEqual(score, 1.0)

        too_low = '{"score": -0.5, "reason": ""}'
        score, _ = parse_faithfulness_result(too_low)
        self.assertEqual(score, 0.0)

    def test_parse_faithfulness_result_markdown(self):
        """测试 markdown 包装去除"""
        from apps.analytics.utils import parse_faithfulness_result

        md_output = '```json\n{"score": 0.7, "reason": "好"}\n```'
        score, reason = parse_faithfulness_result(md_output)
        self.assertEqual(score, 0.7)
        self.assertEqual(reason, '好')


class TestEdgeCases(AnalyticsAPITestBase):
    """边界条件测试"""

    def test_permission_denied_non_admin(self):
        """普通用户访问管理员 API 返回 403"""
        admin_endpoints = [
            '/api/v1/analytics/keywords/',
            '/api/v1/analytics/bad-feedbacks/',
            '/api/v1/analytics/system-metrics/',
            '/api/v1/analytics/quality-reports/',
        ]
        for endpoint in admin_endpoints:
            resp = self.client.get(endpoint, **self.normal_headers)
            self.assertEqual(resp.status_code, 403,
                             msg=f'{endpoint} should return 403 for normal user')

    def test_cross_permission_boundary(self):
        """验证权限隔离：org_reader 不能访问 system 接口"""
        system_endpoints = [
            '/api/v1/analytics/keywords/',
            '/api/v1/analytics/bad-feedbacks/',
            '/api/v1/analytics/system-metrics/',
            '/api/v1/analytics/quality-reports/',
        ]
        for endpoint in system_endpoints:
            resp = self.client.get(endpoint, **self.org_headers)
            self.assertEqual(resp.status_code, 403,
                             msg=f'{endpoint} should return 403 for org reader')

    def test_empty_queryset_handling(self):
        """测试空查询集的处理"""
        resp = self.client.get(
            '/api/v1/analytics/qa-records/?start_date=2020-01-01&end_date=2020-01-02',
            **self.normal_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['total'], 0)

    def test_keyword_weight_boundaries(self):
        """测试关键词权重边界值"""
        kw = KeywordWeight.objects.first()

        # 上边界
        resp = self.client.put(f'/api/v1/analytics/keywords/{kw.id}/',
                               data=json.dumps({'delta': 5.0}),
                               content_type='application/json',
                               **self.writer_headers)
        self.assertEqual(resp.status_code, 200)
        kw.refresh_from_db()
        self.assertLessEqual(kw.weight_score, 2.0)

        # 下边界
        resp = self.client.put(f'/api/v1/analytics/keywords/{kw.id}/',
                               data=json.dumps({'delta': -5.0}),
                               content_type='application/json',
                               **self.writer_headers)
        self.assertEqual(resp.status_code, 200)
        kw.refresh_from_db()
        self.assertGreaterEqual(kw.weight_score, 0.1)
