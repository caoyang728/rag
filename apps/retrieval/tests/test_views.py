"""
apps.retrieval.views 测试 —— DebugSearchView 混合检索调试接口

覆盖范围：
- 未认证 → 401
- query 为空 → 400
- 正常检索 → 200，hybrid_search 调用参数（root_types 默认值、top_k、do_rerank）
- hybrid_search 异常 → 500

 mock hybrid_search：
DebugSearchView 仅做参数解析与异常兜底，检索本身在 hybrid_search 内部，
mock 后可专注验证接口契约（参数拼装、错误码、权限）。
"""
import pytest
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from unittest.mock import patch

from apps.users.models import User


def _create_test_user(username):
    """创建测试用户"""
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')


def _get_auth_token(user):
    """生成用户 JWT access token"""
    return str(RefreshToken.for_user(user).access_token)


@pytest.fixture
def auth_headers():
    """已登录用户的 JWT header（每个测试独立建用户，DB 每测试清空）"""
    user = _create_test_user('retrieval_user')
    return {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(user)}'}


# ============================================================================
# DebugSearchView
# ============================================================================
@pytest.mark.django_db
class TestDebugSearchView:
    """DebugSearchView POST 接口测试

    Django 测试客户端请求会经过 IpFilterMiddleware（查询 IpWhitelist 表），
    auth_headers fixture 也需建用户，故类级启用 DB。
    """

    @pytest.mark.integration
    def test_anonymous_401(self):
        """未认证访问应返回 401"""
        resp = Client().post('/api/v1/retrieval/search/', {'query': 'x'})
        assert resp.status_code == 401

    @pytest.mark.integration
    def test_empty_query_400(self, auth_headers):
        """query 为空或纯空白时返回 400"""
        client = Client()
        resp = client.post('/api/v1/retrieval/search/', {'query': ''}, **auth_headers)
        assert resp.status_code == 400
        resp2 = client.post('/api/v1/retrieval/search/', {'query': '   '}, **auth_headers)
        assert resp2.status_code == 400

    @pytest.mark.integration
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_search_success(self, mock_hybrid, auth_headers):
        """正常检索：返回 hybrid_search 结果，默认 root_types=['company_doc']、top_k=5、do_rerank=True"""
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        resp = Client().post(
            '/api/v1/retrieval/search/',
            {'query': '测试问题'},
            **auth_headers,
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.json()['chunks'] == []
        mock_hybrid.assert_called_once()
        kwargs = mock_hybrid.call_args.kwargs
        # 无根节点时兜底默认 company_doc
        assert kwargs['root_types'] == ['company_doc']
        assert kwargs['do_rerank'] is True
        assert kwargs['top_k'] == 5

    @pytest.mark.integration
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_search_passes_root_types_and_top_k(self, mock_hybrid, auth_headers):
        """显式传 root_types / top_k / do_rerank=False 时应透传"""
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        resp = Client().post(
            '/api/v1/retrieval/search/',
            {'query': 'q', 'root_types': ['hr_docs'], 'top_k': 10, 'do_rerank': False},
            **auth_headers,
            content_type='application/json',
        )
        assert resp.status_code == 200
        kwargs = mock_hybrid.call_args.kwargs
        assert kwargs['root_types'] == ['hr_docs']
        assert kwargs['top_k'] == 10
        assert kwargs['do_rerank'] is False

    @pytest.mark.integration
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_search_exception_500(self, mock_hybrid, auth_headers):
        """hybrid_search 抛异常时返回 500 且携带错误信息"""
        mock_hybrid.side_effect = RuntimeError('vector db down')
        resp = Client().post(
            '/api/v1/retrieval/search/', {'query': 'q'},
            **auth_headers, content_type='application/json')
        assert resp.status_code == 500
        assert 'vector db down' in resp.json()['detail']
