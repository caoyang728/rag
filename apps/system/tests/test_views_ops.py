"""
apps.system.views 运维视图测试 —— HealthView / StatsView / GlobalSearchView

覆盖范围：
- HealthView：健康检查全链路（DB/Redis/LLM 正常与异常分支，mock 外部依赖）
- StatsView 看板统计
- GlobalSearchView 跨域搜索（含 64 字符上限与权限过滤）

Mock 策略：HealthView 的 Redis ping 与 LLM get_llm 均为外部依赖，
测试中 mock 以隔离环境。
"""
import json
from unittest.mock import patch, MagicMock

import pytest

from apps.system.tests.test_views import SystemAPITestBase


class TestHealthView(SystemAPITestBase):
    """健康检查：DB/Redis/LLM 三组件状态"""

    @pytest.mark.integration
    def test_health_all_ok(self):
        """三组件全部正常时返回 ok=True，各组件 ok 标记为 True"""
        fake_llm = MagicMock()
        fake_llm.provider = 'deepseek'
        with patch('redis.Redis.ping', return_value=True), \
             patch('apps.llm.factory.get_llm', return_value=fake_llm):
            resp = self.client.get('/api/v1/system/health/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['service'] == 'rag-agent-backend'
        assert data['ok'] is True
        assert data['components']['db']['ok'] is True
        assert data['components']['redis']['ok'] is True
        assert data['components']['llm']['ok'] is True
        assert data['components']['llm']['provider'] == 'deepseek'

    @pytest.mark.integration
    def test_health_redis_llm_down(self):
        """Redis ping 与 LLM 初始化失败时组件 ok=False 并记录错误（整体 ok 仅 DB 失败时置 False）"""
        with patch('redis.Redis.ping', side_effect=ConnectionError('no redis')), \
             patch('apps.llm.factory.get_llm', side_effect=RuntimeError('no llm')):
            resp = self.client.get('/api/v1/system/health/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['components']['db']['ok'] is True
        assert data['components']['redis']['ok'] is False
        assert 'error' in data['components']['redis']
        assert data['components']['llm']['ok'] is False
        assert 'error' in data['components']['llm']
class TestStatsAndSearchView(SystemAPITestBase):
    """StatsView 看板与 GlobalSearchView 跨域搜索"""

    @pytest.mark.integration
    def test_stats_200(self):
        """看板返回用户/节点/文档/QA 数量统计"""
        resp = self.client.get('/api/v1/system/stats/', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        for k in ('users', 'nodes', 'documents', 'documents_ready',
                  'qa_records', 'my_qa_records'):
            assert k in data

    @pytest.mark.integration
    def test_stats_anonymous_401(self):
        resp = self.client.get('/api/v1/system/stats/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_search_empty_q(self):
        """空关键词返回空分组"""
        resp = self.client.get(
            '/api/v1/system/search/?q=', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['groups'] == {}

    @pytest.mark.integration
    def test_search_too_long_q_400(self):
        """超过 64 字符的关键词返回 400"""
        resp = self.client.get(
            '/api/v1/system/search/?q=' + 'x' * 65, **self.normal_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_search_results(self):
        """命中文档/会话/节点时按分组返回"""
        from apps.knowledge.models import Document, KnowledgeNode
        from apps.memory.models import Session
        node = KnowledgeNode.objects.create(
            root_type='company_doc', node_type='folder', name='检索测试节点',
            path='/1/2/', depth=2)
        Session.objects.create(user=self.normal_user, title='检索测试会话')
        Document.objects.create(
            node=node, team_id=1, owner=self.normal_user,
            title='检索测试文档', file_name='test.pdf', file_type='pdf',
            file_hash='a' * 64, root_type='company_doc')
        resp = self.client.get(
            '/api/v1/system/search/?q=检索测试', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['query'] == '检索测试'
        assert len(data['groups']['documents']) >= 1
        assert len(data['groups']['sessions']) >= 1
        assert len(data['groups']['nodes']) >= 1
        assert data['total'] >= 3

    @pytest.mark.integration
    def test_search_anonymous_401(self):
        resp = self.client.get(
            '/api/v1/system/search/?q=x', **self.anon_headers)
        assert resp.status_code in [401, 403]
