"""
memory views 接口测试 —— MemoryDebugView / RefineMemoryView / UserMemoryView

覆盖范围：
- JWT 认证：匿名访问三个端点均返回 401/403
- 记忆上下文（GET /api/v1/memory/context/）：session_id 必填 400、会话不存在/非本人会话 404、正常返回 200、内部异常 500
- 强制提炼（POST /api/v1/memory/refine/）：参数校验、会话归属校验、异步任务分发（mock 掉 Celery 任务）、分发异常不阻断
- 用户画像（GET/PATCH /api/v1/memory/user-memory/）：GET 默认值、PATCH 各字段更新与非法类型 400、output_preference 合并进 preferences

外部依赖（MemoryManager、refine_session_memory 任务）全部 mock，
避免测试依赖真实 Redis 与 LLM 调用。
"""
import json
from unittest.mock import patch

import pytest

from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import User
from apps.memory.models import Session, UserMemory


def _create_test_user(username='memoryuser', password='testpass123'):
    return User.objects.create_user(
        username=username, password=password, email=f'{username}@test.com')


def _get_auth_token(user):
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


@pytest.mark.django_db
class MemoryAPITestBase:
    """memory 接口测试公共基类：用户、会话、请求头（子类自动继承 django_db）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/用户/JWT 头/会话"""
        self.client = Client()
        self.user = _create_test_user(username='memoryuser')
        self.other_user = _create_test_user(username='otheruser')
        self.anon_headers = {}
        self.auth_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.user)}'}
        self.session = Session.objects.create(user=self.user, title='测试会话')
        self.other_session = Session.objects.create(user=self.other_user, title='他人会话')


class TestMemoryContextView(MemoryAPITestBase):
    """GET /api/v1/memory/context/ 记忆上下文接口测试"""

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/memory/context/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    def test_missing_session_id_400(self):
        """缺少 session_id 应返回 400"""
        resp = self.client.get('/api/v1/memory/context/', **self.auth_headers)
        assert resp.status_code == 400

    def test_nonexistent_session_404(self):
        resp = self.client.get(
            '/api/v1/memory/context/?session_id=99999', **self.auth_headers)
        assert resp.status_code == 404

    def test_other_user_session_404(self):
        """非本人会话应返回 404，防止越权读取他人记忆"""
        resp = self.client.get(
            f'/api/v1/memory/context/?session_id={self.other_session.id}',
            **self.auth_headers)
        assert resp.status_code == 404

    @patch('apps.memory.manager.MemoryManager')
    def test_context_success_200(self, mock_mgr_cls):
        """正常路径：load_context 结果透传为响应体"""
        mock_mgr_cls.return_value.load_context.return_value = {
            'memory_block': '记忆块内容',
            'parts': {'global': '', 'user': '', 'session': '', 'short_term': []},
        }
        resp = self.client.get(
            f'/api/v1/memory/context/?session_id={self.session.id}&question=报销流程',
            **self.auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['memory_block'] == '记忆块内容'
        # 应携带会话与问题参数调用 load_context
        mock_mgr_cls.return_value.load_context.assert_called_once_with(
            self.user, self.session, '报销流程')

    @patch('apps.memory.manager.MemoryManager')
    def test_context_internal_error_500(self, mock_mgr_cls):
        """MemoryManager 内部异常应返回 500 且包含错误信息"""
        mock_mgr_cls.return_value.load_context.side_effect = RuntimeError('boom')
        resp = self.client.get(
            f'/api/v1/memory/context/?session_id={self.session.id}',
            **self.auth_headers)
        assert resp.status_code == 500
        assert '内部错误' in resp.json()['detail']


class TestRefineMemoryView(MemoryAPITestBase):
    """POST /api/v1/memory/refine/ 强制提炼接口测试"""

    def test_anonymous_401(self):
        resp = self.client.post(
            '/api/v1/memory/refine/',
            data=json.dumps({'session_id': 1}),
            content_type='application/json',
            **self.anon_headers)
        assert resp.status_code in [401, 403]

    def test_missing_session_id_400(self):
        resp = self.client.post(
            '/api/v1/memory/refine/',
            data=json.dumps({}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 400

    def test_nonexistent_session_404(self):
        resp = self.client.post(
            '/api/v1/memory/refine/',
            data=json.dumps({'session_id': 99999}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 404

    def test_other_user_session_404(self):
        """非本人会话应返回 404，防止越权触发他人会话的提炼"""
        resp = self.client.post(
            '/api/v1/memory/refine/',
            data=json.dumps({'session_id': self.other_session.id}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 404

    @patch('apps.memory.tasks.refine_session_memory')
    def test_refine_success_200(self, mock_refine):
        """正常路径：分发提炼异步任务并返回 ok"""
        resp = self.client.post(
            '/api/v1/memory/refine/',
            data=json.dumps({'session_id': self.session.id}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 200
        assert resp.json()['ok']
        mock_refine.delay.assert_called_once_with(self.session.id)

    @patch('apps.memory.tasks.refine_session_memory')
    def test_refine_dispatch_error_still_200(self, mock_refine):
        """任务分发异常被吞掉，接口仍返回 200（提炼失败不应阻断主流程）"""
        mock_refine.delay.side_effect = RuntimeError('broker down')
        resp = self.client.post(
            '/api/v1/memory/refine/',
            data=json.dumps({'session_id': self.session.id}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 200
        assert resp.json()['ok']


class TestUserMemoryView(MemoryAPITestBase):
    """GET/PATCH /api/v1/memory/user-memory/ 用户画像接口测试"""

    def test_anonymous_401(self):
        resp = self.client.get('/api/v1/memory/user-memory/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    def test_get_defaults_200(self):
        """首次访问自动创建空画像并返回默认值"""
        resp = self.client.get('/api/v1/memory/user-memory/', **self.auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['domain_tags'] == []
        assert data['frequent_topics'] == []
        assert data['preferences'] == {}
        assert data['profile_text'] == ''
        # GET 自动创建了 UserMemory 记录（get_or_create）
        assert UserMemory.objects.filter(user=self.user).exists()

    def test_get_existing_profile(self):
        """已有画像时返回已存内容（用户创建信号已自动初始化 UserMemory，这里直接更新）"""
        um = UserMemory.objects.get(user=self.user)
        um.domain_tags = ['技术']
        um.frequent_topics = ['RAG']
        um.profile_text = '偏好简洁回答'
        um.save()
        resp = self.client.get('/api/v1/memory/user-memory/', **self.auth_headers)
        data = resp.json()
        assert data['domain_tags'] == ['技术']
        assert data['frequent_topics'] == ['RAG']
        assert data['profile_text'] == '偏好简洁回答'

    def test_patch_domain_tags(self):
        """更新 domain_tags：空白项被过滤、单项截断到 32 字符"""
        resp = self.client.patch(
            '/api/v1/memory/user-memory/',
            data=json.dumps({'domain_tags': [' 技术 ', '   ', '', 'x' * 40]}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 200
        um = UserMemory.objects.get(user=self.user)
        assert um.domain_tags == ['技术', 'x' * 32]

    def test_patch_frequent_topics(self):
        """更新 frequent_topics：自动去空白"""
        resp = self.client.patch(
            '/api/v1/memory/user-memory/',
            data=json.dumps({'frequent_topics': ['RAG', ' 检索 ']}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 200
        um = UserMemory.objects.get(user=self.user)
        assert um.frequent_topics == ['RAG', '检索']

    def test_patch_preferences(self):
        """更新 preferences 字典"""
        resp = self.client.patch(
            '/api/v1/memory/user-memory/',
            data=json.dumps({'preferences': {'tone': '专业', 'length': '简洁'}}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 200
        um = UserMemory.objects.get(user=self.user)
        assert um.preferences['tone'] == '专业'
        assert um.preferences['length'] == '简洁'

    def test_patch_output_preference_merged(self):
        """output_preference 应合并进 preferences 且不覆盖已有键"""
        um = UserMemory.objects.get(user=self.user)
        um.preferences = {'tone': '专业'}
        um.save()
        resp = self.client.patch(
            '/api/v1/memory/user-memory/',
            data=json.dumps({'output_preference': '简洁'}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 200
        um = UserMemory.objects.get(user=self.user)
        assert um.preferences['output_preference'] == '简洁'
        assert um.preferences['tone'] == '专业'

    def test_patch_domain_tags_not_list_400(self):
        resp = self.client.patch(
            '/api/v1/memory/user-memory/',
            data=json.dumps({'domain_tags': '技术'}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 400

    def test_patch_frequent_topics_not_list_400(self):
        resp = self.client.patch(
            '/api/v1/memory/user-memory/',
            data=json.dumps({'frequent_topics': 123}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 400

    def test_patch_preferences_not_dict_400(self):
        resp = self.client.patch(
            '/api/v1/memory/user-memory/',
            data=json.dumps({'preferences': ['a']}),
            content_type='application/json',
            **self.auth_headers)
        assert resp.status_code == 400
