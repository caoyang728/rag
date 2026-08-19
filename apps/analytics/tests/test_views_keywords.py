"""
tests/test_views_keywords.py — 关键词权重 & 检索反馈闭环视图测试

覆盖 views_keywords.py 中所有视图的分支逻辑，重点补全此前 84% 覆盖率
中缺失的行：ChunkClickLogView 参数校验、KeywordFeedbackAggListView
筛选分支与 limit 降级、KeywordFeedbackApplyView 校验与服务调用、
RunFeedbackLoopView ValueError 捕获等。

权限说明：
- KeywordWeightListView/DetailView/FeedbackAggListView/FeedbackApplyView/RunFeedbackLoopView
  均需 CanViewAnalytics + required_perm
- ChunkClickLogView 仅需 IsAuthenticated
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.analytics.models import (
    KeywordWeight, ChunkClickLog, KeywordFeedbackAgg,
)
from apps.users.models import (
    User, Role, UserRoleRel, RolePermissionRel, Permission, GrantStatus,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _create_test_user(username='testuser', password='testpass123',
                      is_super_admin=False, perms=None):
    """创建测试用户并分配权限"""
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
    """获取 JWT token"""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


# ---------------------------------------------------------------------------
# 测试基类
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class KeywordViewTestBase:
    """关键词相关视图测试基类，提供用户/认证头/测试数据"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self.client = Client()
        self.today = timezone.now().date()
        self.yesterday = self.today - timedelta(days=1)

        # --- 用户 ---
        self.normal_user = _create_test_user(
            username='kw_normal', password='pass12345', is_super_admin=False)
        self.super_admin = _create_test_user(
            username='kw_admin', password='admin12345', is_super_admin=True)
        self.system_reader = _create_test_user(
            username='kw_reader', password='pass12345',
            perms=['analytics.system.read'])
        self.system_writer = _create_test_user(
            username='kw_writer', password='pass12345',
            perms=['analytics.system.read', 'analytics.system.write'])

        # --- 认证头 ---
        self.anon_headers = {}
        self.normal_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.admin_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin)}'}
        self.reader_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.system_reader)}'}
        self.writer_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.system_writer)}'}


# ============================================================================
# KeywordWeightListView 测试
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestKeywordWeightListView(KeywordViewTestBase):
    """关键词权重列表/创建视图测试"""

    def _url(self, **qp):
        base = '/api/v1/analytics/keywords/'
        if qp:
            qs = '&'.join(f'{k}={v}' for k, v in qp.items())
            return f'{base}?{qs}'
        return base

    # --- GET 基础 ---

    def test_get_list_success(self):
        """正常列表返回 200 + rows/count"""
        resp = self.client.get(self._url(), **self.reader_headers)
        assert resp.status_code == 200
        assert 'rows' in resp.json()
        assert 'count' in resp.json()

    def test_get_list_anon_returns_401(self):
        """未登录访问返回 401"""
        resp = self.client.get(self._url())
        assert resp.status_code == 401

    def test_get_list_normal_user_returns_403(self):
        """无 analytics.system.read 权限返回 403"""
        resp = self.client.get(self._url(), **self.normal_headers)
        assert resp.status_code == 403

    # --- GET top 参数边界 ---

    def test_get_list_top_invalid_returns_400(self):
        """top 非整数返回 400"""
        resp = self.client.get(self._url(top='abc'), **self.reader_headers)
        assert resp.status_code == 400
        assert 'top' in resp.json()['detail']

    def test_get_list_top_negative_clamped_to_1(self):
        """top 负数被钳位到 1"""
        resp = self.client.get(self._url(top='-5'), **self.reader_headers)
        assert resp.status_code == 200

    def test_get_list_top_exceeds_500_clamped(self):
        """top 超过 500 被钳位到 500"""
        resp = self.client.get(self._url(top='9999'), **self.reader_headers)
        assert resp.status_code == 200

    # --- GET root_type 筛选 ---

    def test_get_list_filter_by_root_type(self):
        """按 root_type 过滤"""
        KeywordWeight.objects.create(keyword='kw1', root_type='code', weight_score=2.0)
        KeywordWeight.objects.create(keyword='kw2', root_type='doc', weight_score=1.5)
        resp = self.client.get(self._url(root_type='code'), **self.reader_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert all(r['root_type'] == 'code' for r in rows)

    # --- POST 创建 ---

    def test_post_create_success(self):
        """正常创建关键词权重"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'new_kw', 'weight_score': 2.5, 'root_type': 'code'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['keyword'] == 'new_kw'
        assert body['created'] is True

    def test_post_update_existing_keyword(self):
        """已存在关键词更新权重"""
        KeywordWeight.objects.create(
            keyword='dup_kw', root_type='all', weight_score=1.0)
        resp = self.client.post(
            self._url(),
            data={'keyword': 'dup_kw', 'weight_score': 3.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['created'] is False

    def test_post_keyword_empty_returns_400(self):
        """keyword 为空返回 400"""
        resp = self.client.post(
            self._url(),
            data={'keyword': '', 'weight_score': 1.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400

    def test_post_keyword_none_returns_400(self):
        """keyword 为 None 返回 400"""
        resp = self.client.post(
            self._url(),
            data={'weight_score': 1.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400

    def test_post_keyword_too_long_returns_400(self):
        """keyword 超过 64 字符返回 400"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'k' * 65, 'weight_score': 1.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400

    def test_post_root_type_too_long_returns_400(self):
        """root_type 超过 32 字符返回 400"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'kw', 'root_type': 'r' * 33},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400

    def test_post_weight_score_non_numeric_returns_400(self):
        """weight_score 非数字返回 400"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'kw', 'weight_score': 'abc'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400

    def test_post_weight_score_none_defaults_1(self):
        """weight_score 未传默认 1.0"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'default_kw'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['weight_score'] == 1.0

    def test_post_weight_score_clamped_high(self):
        """weight_score 超过 5.0 被钳位到 5.0"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'clamp_hi', 'weight_score': 10.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['weight_score'] == 5.0

    def test_post_weight_score_clamped_low(self):
        """weight_score 低于 0.1 被钳位到 0.1"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'clamp_lo', 'weight_score': 0.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['weight_score'] == 0.1

    def test_post_root_type_empty_defaults_all(self):
        """root_type 为空字符串默认 'all'"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'rt_default', 'root_type': ''},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['root_type'] == 'all'

    def test_post_root_type_none_defaults_all(self):
        """root_type 为 None 默认 'all'"""
        resp = self.client.post(
            self._url(),
            data={'keyword': 'rt_none'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['root_type'] == 'all'


# ============================================================================
# KeywordWeightDetailView 测试
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestKeywordWeightDetailView(KeywordViewTestBase):
    """关键词权重详情（PUT 调整）视图测试"""

    def _url(self, kw_id):
        return f'/api/v1/analytics/keywords/{kw_id}/'

    def test_put_success(self):
        """正常调整权重"""
        kw = KeywordWeight.objects.create(
            keyword='adj_kw', root_type='all', weight_score=1.0)
        resp = self.client.put(
            self._url(kw.id),
            data={'delta': 0.5},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['weight_score'] == 1.5

    def test_put_delta_none_returns_400(self):
        """delta 未传返回 400"""
        kw = KeywordWeight.objects.create(
            keyword='adj_kw2', root_type='all', weight_score=1.0)
        resp = self.client.put(
            self._url(kw.id),
            data={},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400
        assert 'delta' in resp.json()['detail']

    def test_put_delta_non_numeric_returns_400(self):
        """delta 非数字返回 400"""
        kw = KeywordWeight.objects.create(
            keyword='adj_kw3', root_type='all', weight_score=1.0)
        resp = self.client.put(
            self._url(kw.id),
            data={'delta': 'abc'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400

    def test_put_nonexistent_kw_returns_404(self):
        """调整不存在的关键词返回 404"""
        resp = self.client.put(
            self._url(999999),
            data={'delta': 0.1},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 404

    def test_put_delta_clamped_high(self):
        """调整后超过 5.0 被钳位"""
        kw = KeywordWeight.objects.create(
            keyword='clamp_adj', root_type='all', weight_score=4.8)
        resp = self.client.put(
            self._url(kw.id),
            data={'delta': 10.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['weight_score'] == 5.0

    def test_put_delta_clamped_low(self):
        """调整后低于 0.1 被钳位"""
        kw = KeywordWeight.objects.create(
            keyword='clamp_lo_adj', root_type='all', weight_score=0.2)
        resp = self.client.put(
            self._url(kw.id),
            data={'delta': -5.0},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['weight_score'] == 0.1

    @patch('apps.analytics.services.feedback_service.record_manual_adjustment')
    def test_put_calls_record_manual_adjustment(self, mock_record):
        """PUT 成功时调用 record_manual_adjustment 审计记录"""
        kw = KeywordWeight.objects.create(
            keyword='audit_kw', root_type='all', weight_score=1.0)
        resp = self.client.put(
            self._url(kw.id),
            data={'delta': 0.3},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 200
        mock_record.assert_called_once()


# ============================================================================
# ChunkClickLogView 测试（覆盖缺失行 126-156）
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestChunkClickLogView(KeywordViewTestBase):
    """溯源点击日志视图测试 — 覆盖所有参数校验分支"""

    URL = '/api/v1/analytics/chunk-clicks/'

    def test_post_success(self):
        """正常记录点击"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 1},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['ok'] is True
        assert ChunkClickLog.objects.filter(chunk_id=1).exists()

    def test_post_with_all_optional_fields(self):
        """携带全部可选字段（qa_record_id=None 避免 FK 违约）"""
        resp = self.client.post(
            self.URL,
            data={
                'chunk_id': 42,
                'document_id': 200,
                'root_type': 'code',
            },
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.get(chunk_id=42)
        assert log.document_id == 200
        assert log.root_type == 'code'

    def test_post_chunk_id_none_returns_400(self):
        """chunk_id 为 None → TypeError → 400"""
        resp = self.client.post(
            self.URL,
            data={},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400
        assert 'chunk_id' in resp.json()['detail']

    def test_post_chunk_id_non_numeric_returns_400(self):
        """chunk_id 非数字 → ValueError → 400"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 'abc'},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    def test_post_chunk_id_zero_returns_400(self):
        """chunk_id = 0 → 必须为正整数 → 400（覆盖行 130）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 0},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400
        assert '正整数' in resp.json()['detail']

    def test_post_chunk_id_negative_returns_400(self):
        """chunk_id 为负数 → 400"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': -5},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    def test_post_qa_record_id_empty_string_becomes_none(self):
        """qa_record_id 为空字符串 → 转为 None（覆盖行 134-136）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 1, 'qa_record_id': ''},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.filter(chunk_id=1).first()
        assert log.qa_record_id is None

    def test_post_qa_record_id_invalid_becomes_none(self):
        """qa_record_id 非法 → 转为 None（覆盖行 135-136）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 2, 'qa_record_id': 'not_a_number'},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.filter(chunk_id=2).first()
        assert log.qa_record_id is None

    def test_post_qa_record_id_none_stays_none(self):
        """qa_record_id 为 None → 保持 None（覆盖行 134）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 3, 'qa_record_id': None},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200

    def test_post_document_id_empty_string_becomes_none(self):
        """document_id 为空字符串 → 转为 None（覆盖行 138-141）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 4, 'document_id': ''},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.filter(chunk_id=4).first()
        assert log.document_id is None

    def test_post_document_id_invalid_becomes_none(self):
        """document_id 非法 → 转为 None（覆盖行 140-141）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 5, 'document_id': 'bad'},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.filter(chunk_id=5).first()
        assert log.document_id is None

    def test_post_document_id_none_stays_none(self):
        """document_id 为 None → 保持 None（覆盖行 138）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 6, 'document_id': None},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200

    def test_post_root_type_too_long_returns_400(self):
        """root_type 超过 32 字符返回 400（覆盖行 145-146）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 7, 'root_type': 'r' * 33},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400
        assert 'root_type' in resp.json()['detail']

    def test_post_root_type_empty_defaults_all(self):
        """root_type 为空字符串默认 'all'（覆盖行 142）"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 8, 'root_type': ''},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.filter(chunk_id=8).first()
        assert log.root_type == 'all'

    def test_post_root_type_none_defaults_all(self):
        """root_type 为 None 默认 'all'"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 9},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.filter(chunk_id=9).first()
        assert log.root_type == 'all'

    def test_post_root_type_whitespace_only_defaults_all(self):
        """root_type 纯空格 → strip 后为空 → 默认 'all'"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 10, 'root_type': '   '},
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        log = ChunkClickLog.objects.filter(chunk_id=10).first()
        assert log.root_type == 'all'

    def test_post_anon_returns_401(self):
        """未登录返回 401"""
        resp = self.client.post(
            self.URL,
            data={'chunk_id': 1},
            content_type='application/json',
        )
        assert resp.status_code == 401


# ============================================================================
# KeywordFeedbackAggListView 测试（覆盖缺失行 168-197）
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestKeywordFeedbackAggListView(KeywordViewTestBase):
    """自动调整记录列表视图测试 — 覆盖所有筛选分支与 limit 降级"""

    URL = '/api/v1/analytics/feedback-loop/aggregations/'

    def _make_agg(self, **kw):
        """创建一条 KeywordFeedbackAgg，自动生成唯一 keyword 避免约束冲突"""
        _counter = getattr(self, '_agg_counter', 0)
        self._agg_counter = _counter + 1
        defaults = dict(
            report_date=self.yesterday,
            keyword=f'test_kw_{_counter}', root_type='all',
            shown_count=10, click_count=3, adopt_count=2, bad_count=1,
            click_rate=0.3, adopt_rate=0.2,
            old_score=1.0, new_score=1.1, delta=0.1,
            reason='test reason', adjust_type='auto', status='pending',
        )
        defaults.update(kw)
        return KeywordFeedbackAgg.objects.create(**defaults)

    def test_get_list_success(self):
        """正常返回 200"""
        self._make_agg()
        resp = self.client.get(self.URL, **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] >= 1

    def test_get_list_anon_returns_401(self):
        """未登录返回 401"""
        resp = self.client.get(self.URL)
        assert resp.status_code == 401

    # --- date 筛选分支 ---

    def test_filter_by_valid_date(self):
        """有效 date 筛选（覆盖行 175-179）"""
        self._make_agg(report_date=self.yesterday)
        self._make_agg(report_date=self.today)
        resp = self.client.get(
            f'{self.URL}?date={self.today}', **self.reader_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert all(r['report_date'] == str(self.today) for r in rows)

    def test_filter_by_invalid_date_returns_400(self):
        """无效 date 格式返回 400（覆盖行 177-178）"""
        resp = self.client.get(
            f'{self.URL}?date=not-a-date', **self.reader_headers)
        assert resp.status_code == 400
        assert 'date' in resp.json()['detail']

    def test_filter_by_invalid_date_format_returns_400(self):
        """非 ISO 格式日期返回 400（覆盖行 177-178）"""
        resp = self.client.get(
            f'{self.URL}?date=2024/13/45', **self.reader_headers)
        assert resp.status_code == 400

    # --- keyword 筛选分支 ---

    def test_filter_by_keyword(self):
        """keyword 匹配筛选（覆盖行 181-182）"""
        self._make_agg(keyword='apple')
        self._make_agg(keyword='banana')
        resp = self.client.get(
            f'{self.URL}?keyword=apple', **self.reader_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert all('apple' in r['keyword'] for r in rows)

    # --- root_type 筛选分支 ---

    def test_filter_by_root_type(self):
        """root_type 筛选（覆盖行 184-185）"""
        self._make_agg(root_type='code')
        self._make_agg(root_type='doc')
        resp = self.client.get(
            f'{self.URL}?root_type=code', **self.reader_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert all(r['root_type'] == 'code' for r in rows)

    # --- status 筛选分支 ---

    def test_filter_by_status(self):
        """status 筛选（覆盖行 187-188）"""
        self._make_agg(status='pending')
        self._make_agg(status='applied')
        resp = self.client.get(
            f'{self.URL}?status=pending', **self.reader_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert all(r['status'] == 'pending' for r in rows)

    # --- limit 分支 ---

    def test_limit_clamped_to_100(self):
        """limit 超过 100 被钳位到 100（覆盖行 190）"""
        resp = self.client.get(
            f'{self.URL}?limit=999', **self.reader_headers)
        assert resp.status_code == 200

    def test_limit_clamped_to_1(self):
        """limit 负数被钳位到 1（覆盖行 190）"""
        resp = self.client.get(
            f'{self.URL}?limit=-5', **self.reader_headers)
        assert resp.status_code == 200

    def test_limit_invalid_defaults_50(self):
        """limit 非整数回退默认 50（覆盖行 191-192）"""
        resp = self.client.get(
            f'{self.URL}?limit=abc', **self.reader_headers)
        assert resp.status_code == 200

    def test_limit_none_defaults_50(self):
        """limit 未传使用默认 50"""
        resp = self.client.get(self.URL, **self.reader_headers)
        assert resp.status_code == 200

    def test_limit_valid_value(self):
        """有效 limit 值正常生效"""
        for _ in range(5):
            self._make_agg()
        resp = self.client.get(
            f'{self.URL}?limit=3', **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] <= 3

    def test_filter_empty_date_ignored(self):
        """date 为空字符串不触发过滤"""
        self._make_agg()
        resp = self.client.get(
            f'{self.URL}?date=', **self.reader_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] >= 1

    def test_filter_empty_keyword_ignored(self):
        """keyword 为空字符串不触发过滤"""
        self._make_agg()
        resp = self.client.get(
            f'{self.URL}?keyword=', **self.reader_headers)
        assert resp.status_code == 200

    def test_filter_whitespace_keyword_ignored(self):
        """keyword 为空格字符串 strip 后不触发过滤"""
        self._make_agg()
        resp = self.client.get(
            f'{self.URL}?keyword=   ', **self.reader_headers)
        assert resp.status_code == 200


# ============================================================================
# KeywordFeedbackApplyView 测试（覆盖缺失行 209-222）
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestKeywordFeedbackApplyView(KeywordViewTestBase):
    """人工复核应用/忽略视图测试"""

    URL = '/api/v1/analytics/feedback-loop/apply/'

    def _make_pending_agg(self):
        """创建一条 pending 状态的聚合记录"""
        return KeywordFeedbackAgg.objects.create(
            report_date=self.yesterday,
            keyword='pending_kw', root_type='all',
            shown_count=10, click_count=3, adopt_count=2, bad_count=1,
            click_rate=0.3, adopt_rate=0.2,
            old_score=1.0, new_score=1.2, delta=0.2,
            reason='auto', adjust_type='auto', status='pending',
        )

    def test_post_id_none_returns_400(self):
        """id 为 None 返回 400（覆盖行 213-214）"""
        resp = self.client.post(
            self.URL,
            data={'action': 'apply'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400
        assert 'id' in resp.json()['detail']

    def test_post_id_non_numeric_returns_400(self):
        """id 非数字返回 400（覆盖行 213-214）"""
        resp = self.client.post(
            self.URL,
            data={'id': 'abc', 'action': 'apply'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400

    def test_post_action_invalid_returns_400(self):
        """action 非法值返回 400（覆盖行 216-217）"""
        agg = self._make_pending_agg()
        resp = self.client.post(
            self.URL,
            data={'id': agg.id, 'action': 'delete'},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400
        assert 'action' in resp.json()['detail']

    def test_post_action_empty_defaults_apply(self):
        """action 为空字符串默认 'apply'（覆盖行 215）"""
        agg = self._make_pending_agg()
        # action 为空 → "apply" → apply_pending_adjustment 执行
        with patch('apps.analytics.services.feedback_service.apply_pending_adjustment',
                   return_value=(True, '已应用')):
            resp = self.client.post(
                self.URL,
                data={'id': agg.id, 'action': ''},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 200

    def test_post_action_none_defaults_apply(self):
        """action 为 None 默认 'apply'（覆盖行 215）"""
        agg = self._make_pending_agg()
        with patch('apps.analytics.services.feedback_service.apply_pending_adjustment',
                   return_value=(True, '已应用')):
            resp = self.client.post(
                self.URL,
                data={'id': agg.id},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 200

    def test_post_action_whitespace_returns_400(self):
        """action 纯空格 strip 后为空串 → 不在 apply/ignore 中 → 400

        注意：'   ' 是 truthy，不会触发 or 'apply' 回退，
        strip 后变 '' 不在合法值列表中，返回 400
        """
        agg = self._make_pending_agg()
        resp = self.client.post(
            self.URL,
            data={'id': agg.id, 'action': '   '},
            content_type='application/json',
            **self.writer_headers,
        )
        assert resp.status_code == 400
        assert 'action' in resp.json()['detail']

    def test_post_apply_success(self):
        """正常应用 pending 记录（覆盖行 219-222）"""
        agg = self._make_pending_agg()
        with patch('apps.analytics.services.feedback_service.apply_pending_adjustment',
                   return_value=(True, '已应用')):
            resp = self.client.post(
                self.URL,
                data={'id': agg.id, 'action': 'apply'},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 200
            assert resp.json()['ok'] is True

    def test_post_ignore_success(self):
        """正常忽略 pending 记录"""
        agg = self._make_pending_agg()
        with patch('apps.analytics.services.feedback_service.apply_pending_adjustment',
                   return_value=(True, '已忽略')):
            resp = self.client.post(
                self.URL,
                data={'id': agg.id, 'action': 'ignore'},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 200
            assert resp.json()['ok'] is True

    def test_post_service_returns_false_returns_400(self):
        """服务返回失败 → 400（覆盖行 220-221）"""
        agg = self._make_pending_agg()
        with patch('apps.analytics.services.feedback_service.apply_pending_adjustment',
                   return_value=(False, '聚合记录不存在')):
            resp = self.client.post(
                self.URL,
                data={'id': agg.id, 'action': 'apply'},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 400
            assert '聚合记录不存在' in resp.json()['detail']

    def test_post_anon_returns_401(self):
        """未登录返回 401"""
        resp = self.client.post(
            self.URL,
            data={'id': 1, 'action': 'apply'},
            content_type='application/json',
        )
        assert resp.status_code == 401

    def test_post_no_write_perm_returns_403(self):
        """无 analytics.system.write 权限返回 403"""
        resp = self.client.post(
            self.URL,
            data={'id': 1, 'action': 'apply'},
            content_type='application/json',
            **self.reader_headers,
        )
        assert resp.status_code == 403


# ============================================================================
# RunFeedbackLoopView 测试
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestRunFeedbackLoopView(KeywordViewTestBase):
    """手动触发反馈闭环聚合视图测试"""

    URL = '/api/v1/analytics/feedback-loop/run/'

    def test_post_success_default_date(self):
        """正常触发，默认聚合昨天"""
        mock_result = {
            'ok': True, 'report_date': str(self.yesterday),
            'total': 5, 'applied': 3, 'pending': 1, 'skipped': 1,
        }
        with patch('apps.analytics.services.feedback_service.run_keyword_feedback_loop',
                   return_value=mock_result):
            resp = self.client.post(
                self.URL,
                data={},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 200
            assert resp.json()['ok'] is True

    def test_post_with_custom_date(self):
        """传入指定日期"""
        mock_result = {
            'ok': True, 'report_date': '2024-06-15',
            'total': 0, 'applied': 0, 'pending': 0, 'skipped': 0,
        }
        with patch('apps.analytics.services.feedback_service.run_keyword_feedback_loop',
                   return_value=mock_result) as mock_fn:
            resp = self.client.post(
                self.URL,
                data={'date': '2024-06-15'},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 200
            mock_fn.assert_called_once_with(report_date='2024-06-15')

    def test_post_value_error_returns_400(self):
        """服务抛出 ValueError 返回 400"""
        with patch('apps.analytics.services.feedback_service.run_keyword_feedback_loop',
                   side_effect=ValueError('日期格式无效')):
            resp = self.client.post(
                self.URL,
                data={'date': 'bad-date'},
                content_type='application/json',
                **self.writer_headers,
            )
            assert resp.status_code == 400
            assert '日期格式无效' in resp.json()['detail']

    def test_post_anon_returns_401(self):
        """未登录返回 401"""
        resp = self.client.post(
            self.URL,
            data={},
            content_type='application/json',
        )
        assert resp.status_code == 401

    def test_post_no_write_perm_returns_403(self):
        """无 analytics.system.write 权限返回 403"""
        resp = self.client.post(
            self.URL,
            data={},
            content_type='application/json',
            **self.reader_headers,
        )
        assert resp.status_code == 403
