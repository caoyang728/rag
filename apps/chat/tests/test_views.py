"""
chat/views 接口测试
覆盖：SessionViewSet（CRUD、搜索、软删除）、
     ChatAskStreamView（SSE 流式问答接口参数校验与响应结构）、
     FeedbackView（反馈提交）、QaRecordListView（问答历史）
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import User, Role, UserRoleRel, GrantStatus
from apps.chat.models import QaRecord, QaFeedback, HotQaCache
from apps.memory.models import Session
from apps.knowledge.models import KnowledgeNode


def _create_test_user(username='testuser', password='testpass123',
                      is_super_admin=False):
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
    return user


def _get_auth_token(user):
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


@pytest.mark.django_db
class ChatAPITestBase:
    """会话 API 测试公共基类（子类自动继承 django_db）"""

    def _init_env(self):
        """初始化 client/用户/JWT 认证头/根节点（公共逻辑）"""
        self.client = Client()
        self.normal_user = _create_test_user(
            username='normal', password='pass12345', is_super_admin=False)
        self.super_admin = _create_test_user(
            username='admin', password='admin12345', is_super_admin=True)

        self.anon_headers = {}
        self.normal_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.admin_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin)}'}

        self.root_node = KnowledgeNode.objects.create(
            name='test_root', node_type='root', root_type='company_doc',
            created_by=self.super_admin)

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：初始化共享环境"""
        self._init_env()

    def _create_session(self, user=None, root_type='company_doc', title='Test Session'):
        if user is None:
            user = self.normal_user
        return Session.objects.create(user=user, root_type=root_type, title=title)


class TestSessionAPI(ChatAPITestBase):
    """会话 CRUD 接口测试"""

    def test_list_authenticated_200(self):
        self._create_session()
        resp = self.client.get('/api/v1/chat/sessions/', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        # DRF 默认使用分页，返回 {'count': ..., 'results': [...]}
        assert 'results' in data or isinstance(data, list)
        results = data['results'] if 'results' in data else data
        assert len(results) >= 1

    def test_list_anonymous_401(self):
        resp = self.client.get('/api/v1/chat/sessions/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    def test_create_session_201(self):
        resp = self.client.post(
            '/api/v1/chat/sessions/',
            data=json.dumps({'title': 'New Session', 'root_type': 'company_doc'}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['title'] == 'New Session'
        assert 'id' in data
        assert data['root_type'] == 'company_doc'

    def test_create_session_default_root_type(self):
        resp = self.client.post(
            '/api/v1/chat/sessions/',
            data=json.dumps({'title': 'Minimal Session'}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 201

    def test_retrieve_session_200(self):
        session = self._create_session()
        resp = self.client.get(f'/api/v1/chat/sessions/{session.id}/', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['id'] == session.id

    def test_update_session_200(self):
        session = self._create_session()
        resp = self.client.put(
            f'/api/v1/chat/sessions/{session.id}/',
            data=json.dumps({'title': 'Updated', 'root_type': 'company_doc'}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 200
        assert resp.json()['title'] == 'Updated'

    def test_delete_session_soft(self):
        """软删除：is_deleted=True，返回 204"""
        session = self._create_session()
        resp = self.client.delete(f'/api/v1/chat/sessions/{session.id}/', **self.normal_headers)
        assert resp.status_code == 204
        session.refresh_from_db()
        assert session.is_deleted is True

    def test_search_session(self):
        self._create_session(title='Searchable Title')
        resp = self.client.get(
            '/api/v1/chat/sessions/?search=Searchable',
            **self.normal_headers
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_search_session_by_first_question(self):
        """search 应能匹配会话首条提问（annotate _first_question 后过滤）"""
        session = self._create_session(title='无关标题')
        QaRecord.objects.create(
            session=session, user=self.normal_user, question='报销流程怎么走', answer='')
        resp = self.client.get(
            '/api/v1/chat/sessions/?search=报销',
            **self.normal_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        results = data['results'] if 'results' in data else data
        assert len(results) == 1
        assert results[0]['id'] == session.id

    def test_qa_action(self):
        """获取会话下的问答记录"""
        session = self._create_session()
        resp = self.client.get(
            f'/api/v1/chat/sessions/{session.id}/qa/',
            **self.normal_headers
        )
        assert resp.status_code == 200


class TestChatAskStreamAPI(ChatAPITestBase):
    """SSE 流式问答接口测试"""

    @patch('apps.agent.executor.ask_stream')
    def test_ask_stream_success(self, mock_executor):
        """正常流式问答应返回 SSE 响应"""
        from apps.agent.streamer import stream_response
        mock_executor.return_value = iter([
            {'type': 'start', 'session_id': 1, 'citations': [], 'is_hit_cache': False},
            {'type': 'first_token', 'ttfb_ms': 100},
            {'type': 'delta', 'delta': 'Hello'},
            {'type': 'delta', 'delta': 'World'},
            {'type': 'done', 'message_id': 1, 'session_id': 1, 'citations': [], 'stats': {}},
        ])

        resp = self.client.post(
            '/api/v1/chat/ask_stream/',
            data=json.dumps({'question': 'What is RAG?'}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'text/event-stream'

    def test_ask_stream_empty_question_400(self):
        """空问题应返回 400"""
        resp = self.client.post(
            '/api/v1/chat/ask_stream/',
            data=json.dumps({'question': ''}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 400

    def test_ask_stream_anonymous_401(self):
        resp = self.client.post(
            '/api/v1/chat/ask_stream/',
            data=json.dumps({'question': 'Test'}),
            content_type='application/json',
            **self.anon_headers
        )
        assert resp.status_code in [401, 403]

    @patch('apps.agent.executor.ask_stream')
    def test_ask_stream_with_existing_session(self, mock_executor):
        """指定 session_id 时应复用会话"""
        session = self._create_session()
        mock_executor.return_value = iter([
            {'type': 'start', 'session_id': session.id, 'citations': [], 'is_hit_cache': False},
            {'type': 'done', 'message_id': 1, 'session_id': session.id, 'citations': [], 'stats': {}},
        ])

        resp = self.client.post(
            '/api/v1/chat/ask_stream/',
            data=json.dumps({'question': 'Test', 'session_id': session.id}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 200

    def test_ask_stream_nonexistent_session_404(self):
        """不存在的 session_id 应返回 404"""
        resp = self.client.post(
            '/api/v1/chat/ask_stream/',
            data=json.dumps({'question': 'Test', 'session_id': 99999}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 404

    @patch('apps.agent.executor.ask_stream')
    def test_ask_stream_default_root_type(self, mock_executor):
        """未指定 root_types 时应自动获取默认根类型"""
        mock_executor.return_value = iter([
            {'type': 'start', 'session_id': 1, 'citations': [], 'is_hit_cache': False},
            {'type': 'done', 'message_id': 1, 'session_id': 1, 'citations': [], 'stats': {}},
        ])

        resp = self.client.post(
            '/api/v1/chat/ask_stream/',
            data=json.dumps({'question': 'Test'}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 200

    @patch('apps.agent.executor.ask_stream')
    def test_ask_stream_with_mode(self, mock_executor):
        """指定 mode 参数应正确传递"""
        mock_executor.return_value = iter([
            {'type': 'start', 'session_id': 1, 'citations': [], 'is_hit_cache': False},
            {'type': 'done', 'message_id': 1, 'session_id': 1, 'citations': [], 'stats': {}},
        ])

        for mode in ['auto', 'rag', 'agent', 'wiki', 'graphrag']:
            mock_executor.reset_mock()
            self.client.post(
                '/api/v1/chat/ask_stream/',
                data=json.dumps({'question': 'Test', 'mode': mode}),
                content_type='application/json',
                **self.normal_headers
            )
            call_kwargs = mock_executor.call_args[1]
            assert call_kwargs['mode'] == mode

    @patch('apps.agent.executor.ask_stream')
    def test_ask_stream_session_turn_incremented(self, mock_executor):
        """流式请求应预先将 session.turn_count +1"""
        session = self._create_session()
        session.turn_count = 5
        session.save()

        mock_executor.return_value = iter([
            {'type': 'start', 'session_id': session.id, 'citations': [], 'is_hit_cache': False},
            {'type': 'done', 'message_id': 1, 'session_id': session.id, 'citations': [], 'stats': {}},
        ])

        self.client.post(
            '/api/v1/chat/ask_stream/',
            data=json.dumps({'question': 'Test', 'session_id': session.id}),
            content_type='application/json',
            **self.normal_headers
        )
        session.refresh_from_db()
        assert session.turn_count == 6


class TestFeedbackAPI(ChatAPITestBase):
    """反馈提交接口测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：复用基类共享环境并补充会话与 QA 记录"""
        self._init_env()
        self.session = self._create_session()
        self.qa_record = QaRecord.objects.create(
            session=self.session, user=self.normal_user,
            question='Test question', answer='Test answer',
            answer_type='rag', root_type='company_doc',
        )

    def test_submit_feedback_201(self):
        resp = self.client.post(
            '/api/v1/chat/feedback/',
            data=json.dumps({
                'qa_record_id': self.qa_record.id,
                'rating': 1,
                'comment': 'Great answer'
            }),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 201

    def test_submit_feedback_update_existing_200(self):
        """已有反馈时更新而非创建"""
        QaFeedback.objects.create(
            qa_record=self.qa_record, user=self.normal_user, rating=0)
        resp = self.client.post(
            '/api/v1/chat/feedback/',
            data=json.dumps({
                'qa_record_id': self.qa_record.id,
                'rating': -1,
            }),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 200

    def test_submit_feedback_missing_id_400(self):
        resp = self.client.post(
            '/api/v1/chat/feedback/',
            data=json.dumps({'rating': 1}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 400

    def test_submit_feedback_nonexistent_qa_404(self):
        resp = self.client.post(
            '/api/v1/chat/feedback/',
            data=json.dumps({
                'qa_record_id': 99999,
                'rating': 1,
            }),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 404

    def test_submit_feedback_with_tags(self):
        resp = self.client.post(
            '/api/v1/chat/feedback/',
            data=json.dumps({
                'qa_record_id': self.qa_record.id,
                'rating': -1,
                'tags': ['不准确', '无引用'],
                'comment': 'This is wrong'
            }),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert '不准确' in data['tags']

    def test_submit_feedback_with_message_id_alias(self):
        """message_id 作为 qa_record_id 的别名（SSE 返回字段兼容）"""
        resp = self.client.post(
            '/api/v1/chat/feedback/',
            data=json.dumps({'message_id': self.qa_record.id, 'rating': 1}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 201
        assert resp.json()['qa_record_id'] == self.qa_record.id


class TestQaRecordListAPI(ChatAPITestBase):
    """问答历史接口测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：复用基类共享环境并补充会话与 5 条 QA 记录"""
        self._init_env()
        self.session = self._create_session()
        for i in range(5):
            QaRecord.objects.create(
                session=self.session, user=self.normal_user,
                question=f'Q{i}', answer=f'A{i}',
                answer_type='rag', root_type='company_doc',
            )

    def test_list_records_200(self):
        resp = self.client.get('/api/v1/chat/records/', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert 'records' in data
        assert len(data['records']) >= 5

    def test_list_filter_by_session(self):
        resp = self.client.get(
            f'/api/v1/chat/records/?session_id={self.session.id}',
            **self.normal_headers
        )
        assert resp.status_code == 200
        assert len(resp.json()['records']) == 5

    def test_list_anonymous_401(self):
        resp = self.client.get('/api/v1/chat/records/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    def test_list_excludes_other_users(self):
        """其他用户的记录不可见（通过会话归属过滤）"""
        other_user = _create_test_user(username='other', password='pass12345')
        # 为其他用户创建会话和记录
        other_session = Session.objects.create(user=other_user, root_type='company_doc', title='Other Session')
        QaRecord.objects.create(
            session=other_session, user=other_user,
            question='Other Q', answer='Other A',
            answer_type='rag', root_type='company_doc',
        )
        resp = self.client.get('/api/v1/chat/records/', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        # 只返回当前用户的记录
        for r in data['records']:
            assert r['session_id'] != other_session.id


class TestChatConfigAPI(ChatAPITestBase):
    """GET /api/v1/chat/config/ 聊天页来源配置接口测试
    （返回 SystemConfig CHAT_SOURCE_ENABLED 开启的来源，前端据此渲染来源开关）"""

    def test_config_anonymous_401(self):
        resp = self.client.get('/api/v1/chat/config/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @patch('apps.system.config_loader.get_config_value')
    def test_config_when_enabled_doc_db_then_returns_only_enabled(self, mock_cfg):
        """系统配置只开启 doc/db 时，接口只返回开启的来源"""
        mock_cfg.return_value = 'doc,db'
        resp = self.client.get('/api/v1/chat/config/', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['sources_enabled'] == ['doc', 'db']
        assert data['all_sources'] == ['doc', 'db', 'web', 'llm']

    @patch('apps.system.config_loader.get_config_value')
    def test_config_when_empty_then_fallback_all(self, mock_cfg):
        """配置为空（未初始化）时回退全部来源，保证聊天页来源开关可用"""
        mock_cfg.return_value = ''
        resp = self.client.get('/api/v1/chat/config/', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['sources_enabled'] == ['doc', 'db', 'web', 'llm']

    @patch('apps.system.config_loader.get_config_value')
    def test_config_when_invalid_keys_then_filtered(self, mock_cfg):
        """配置含非法 key 时剔除，仅返回合法来源"""
        mock_cfg.return_value = 'doc,foo,llm'
        resp = self.client.get('/api/v1/chat/config/', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['sources_enabled'] == ['doc', 'llm']
