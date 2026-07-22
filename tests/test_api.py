"""
API Test Cases for RAG-Agent Backend
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')
django.setup()

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from apps.knowledge.models import KnowledgeNode, Document
from apps.chat.models import QaRecord, QaFeedback
from apps.memory.models import Session
from apps.analytics.models import KeywordWeight
from apps.security.models import IpWhitelist, IpBlacklist, LoginAttempt, SensitiveWord
from apps.audit.models import AuditLog
from datetime import timedelta

User = get_user_model()


@override_settings(DEBUG=True)
class AuthAPITests(TestCase):
    """测试认证相关接口"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123',
            real_name='测试用户',
            email='test@example.com',
            status='active'
        )

    def test_login_success(self):
        resp = self.client.post('/api/v1/auth/login/', {
            'username': 'testuser',
            'password': 'testpass123'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('access', resp.json())
        self.assertIn('refresh', resp.json())
        self.assertIn('user', resp.json())

    def test_login_wrong_password(self):
        resp = self.client.post('/api/v1/auth/login/', {
            'username': 'testuser',
            'password': 'wrongpass'
        })
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()['detail'], '用户名或密码错误')

    def test_login_empty_fields(self):
        resp = self.client.post('/api/v1/auth/login/', {
            'username': '',
            'password': ''
        })
        self.assertEqual(resp.status_code, 400)

    def test_profile(self):
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        resp = self.client.get('/api/v1/auth/profile/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['username'], 'testuser')

    def test_change_password(self):
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        resp = self.client.post('/api/v1/auth/change-password/', {
            'old_password': 'testpass123',
            'new_password': 'newpass123'
        })
        self.assertEqual(resp.status_code, 200)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('newpass123'))


@override_settings(DEBUG=True)
class KnowledgeAPITests(TestCase):
    """测试知识库相关接口"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123',
            real_name='测试用户',
            status='active'
        )
        self.token = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

        self.root_node = KnowledgeNode.objects.create(
            name='公司文档',
            type='company_doc',
            is_root=True,
            created_by=self.user
        )
        self.child_node = KnowledgeNode.objects.create(
            name='技术文档',
            type='category',
            parent=self.root_node,
            created_by=self.user
        )

    def test_node_tree(self):
        resp = self.client.get('/api/v1/knowledge/nodes/tree/', {'root_type': 'company_doc'})
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()), 1)

    def test_create_node(self):
        resp = self.client.post('/api/v1/knowledge/nodes/', {
            'name': '新节点',
            'type': 'category',
            'parent_id': self.root_node.id
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['name'], '新节点')

    def test_document_list(self):
        resp = self.client.get('/api/v1/knowledge/documents/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('results', resp.json())

    def test_document_detail(self):
        doc = Document.objects.create(
            filename='test.txt',
            sha256='abc123',
            size=100,
            status='pending',
            node=self.root_node,
            created_by=self.user
        )
        resp = self.client.get(f'/api/v1/knowledge/documents/{doc.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['filename'], 'test.txt')


@override_settings(DEBUG=True)
class ChatAPITests(TestCase):
    """测试聊天相关接口"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123',
            real_name='测试用户',
            status='active'
        )
        self.token = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_create_session(self):
        resp = self.client.post('/api/v1/chat/sessions/', {'topic': '测试会话'})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['title'], '测试会话')

    def test_session_list(self):
        Session.objects.create(user=self.user, title='会话1')
        Session.objects.create(user=self.user, title='会话2')

        resp = self.client.get('/api/v1/chat/sessions/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()), 2)

    def test_send_message(self):
        session = Session.objects.create(user=self.user, title='测试会话')
        resp = self.client.post(f'/api/v1/chat/sessions/{session.id}/messages/', {
            'content': '你好',
            'stream': False
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('response', resp.json())

    def test_feedback(self):
        session = Session.objects.create(user=self.user, title='测试会话')
        qa = QaRecord.objects.create(
            session=session,
            user=self.user,
            question='测试问题',
            answer='测试答案'
        )
        resp = self.client.post(f'/api/v1/chat/feedback/{qa.id}/', {
            'rating': 1,
            'reason': '回答准确'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(QaFeedback.objects.filter(qa_record=qa, rating=1).exists())


@override_settings(DEBUG=True)
class AnalyticsAPITests(TestCase):
    """测试分析相关接口"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='adminuser',
            password='testpass123',
            real_name='管理员',
            status='active',
            is_staff=True
        )
        self.token = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_overview(self):
        resp = self.client.get('/api/v1/analytics/overview/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('total_qa', data)
        self.assertIn('accuracy', data)
        self.assertIn('avg_latency_ms', data)

    def test_trend(self):
        resp = self.client.get('/api/v1/analytics/trend/', {'days': 7})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('trend', data)
        self.assertEqual(data['days'], 7)

    def test_keywords_list(self):
        KeywordWeight.objects.create(word='测试', weight=1.0, category='test')
        resp = self.client.get('/api/v1/analytics/keywords/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()['rows']), 1)

    def test_keywords_update(self):
        kw = KeywordWeight.objects.create(word='测试', weight=1.0, category='test')
        resp = self.client.put(f'/api/v1/analytics/keywords/{kw.id}/', {
            'weight': 2.0
        })
        self.assertEqual(resp.status_code, 200)
        kw.refresh_from_db()
        self.assertEqual(kw.weight, 2.0)

    def test_bad_feedbacks(self):
        session = Session.objects.create(user=self.user, title='测试会话')
        qa = QaRecord.objects.create(
            session=session,
            user=self.user,
            question='测试问题',
            answer='测试答案'
        )
        QaFeedback.objects.create(qa_record=qa, rating=-1, comment='回答错误')

        resp = self.client.get('/api/v1/analytics/bad-feedbacks/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()['rows']), 1)


@override_settings(DEBUG=True)
class SecurityAPITests(TestCase):
    """测试安全相关接口"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='adminuser',
            password='testpass123',
            real_name='管理员',
            status='active',
            is_staff=True
        )
        self.token = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_whitelist_list(self):
        IpWhitelist.objects.create(ip_or_cidr='127.0.0.1', description='本地', created_by=self.user)
        resp = self.client.get('/api/v1/security/ip-whitelist/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()['rows']), 1)

    def test_whitelist_create(self):
        resp = self.client.post('/api/v1/security/ip-whitelist/', {
            'ip_or_cidr': '192.168.1.0/24',
            'description': '内网'
        })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(IpWhitelist.objects.filter(ip_or_cidr='192.168.1.0/24').exists())

    def test_whitelist_delete(self):
        obj = IpWhitelist.objects.create(ip_or_cidr='10.0.0.1', description='测试', created_by=self.user)
        resp = self.client.delete(f'/api/v1/security/ip-whitelist/{obj.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(IpWhitelist.objects.filter(id=obj.id).exists())

    def test_blacklist_list(self):
        IpBlacklist.objects.create(ip='1.2.3.4', reason='manual', detail='测试封禁')
        resp = self.client.get('/api/v1/security/ip-blacklist/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()['rows']), 1)

    def test_blacklist_create(self):
        resp = self.client.post('/api/v1/security/ip-blacklist/', {
            'ip': '5.6.7.8',
            'reason': 'manual',
            'detail': '测试'
        })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(IpBlacklist.objects.filter(ip='5.6.7.8').exists())

    def test_blacklist_unblock(self):
        obj = IpBlacklist.objects.create(ip='9.10.11.12', reason='manual', detail='测试')
        resp = self.client.put(f'/api/v1/security/ip-blacklist/{obj.id}/')
        self.assertEqual(resp.status_code, 200)
        obj.refresh_from_db()
        self.assertFalse(obj.is_active)

    def test_login_attempts(self):
        LoginAttempt.objects.create(username='test', ip='127.0.0.1', result='success')
        resp = self.client.get('/api/v1/security/login-attempts/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()['total'], 1)

    def test_sensitive_words(self):
        SensitiveWord.objects.create(word='敏感词1', category='other', action='mask')
        resp = self.client.get('/api/v1/security/sensitive-words/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()['rows']), 1)


@override_settings(DEBUG=True)
class AuditAPITests(TestCase):
    """测试审计日志接口"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='adminuser',
            password='testpass123',
            real_name='管理员',
            status='active',
            is_staff=True
        )
        self.token = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_audit_logs(self):
        AuditLog.objects.create(
            actor=self.user,
            action='login',
            target_type='user',
            target_id=self.user.id,
            ip_address='127.0.0.1',
            result='success'
        )
        resp = self.client.get('/api/v1/audit/logs/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()['rows']), 1)


if __name__ == '__main__':
    import unittest
    unittest.main()