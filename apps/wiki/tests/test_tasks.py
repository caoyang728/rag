"""
apps.wiki.tasks 测试 —— LLM Wiki Celery 任务

覆盖范围：
- generate_wiki_for_node：节点 Wiki 生成任务透传
- generate_community_wiki_task：社区 Wiki 生成任务透传
- refresh_expired_wiki_pages：过期页面刷新（无过期/有节点成功/无节点跳过/异常不计入）

采用 mock：
任务仅做参数拼接与生成器调用转发，LLM 与生成逻辑分别在 llm/generator
模块（各有专项测试），patch get_llm_advanced 与 generator 即可验证任务契约。
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.wiki.tasks import (
    generate_wiki_for_node,
    generate_community_wiki_task,
    refresh_expired_wiki_pages,
)
from apps.wiki.models import WikiPage
from apps.users.models import User


def _make_user(username='wiki-user'):
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')


def _make_node():
    """创建知识节点（level 4 业务分类）"""
    from apps.knowledge.models import KnowledgeNode
    node = KnowledgeNode.objects.create(
        root_type='company_doc', node_type='folder', node_level=4, name='Wiki节点')
    node.path = f'/{node.id}/'
    node.depth = 1
    node.save(update_fields=['path', 'depth'])
    return node


def _fake_page(page_id=1):
    """构造生成器返回的伪页面对象"""
    page = MagicMock()
    page.id = page_id
    page.title = '生成的Wiki'
    return page


class TestGenerateWikiForNode:
    """节点 Wiki 生成任务测试"""

    @pytest.mark.unit
    def test_generate_for_node_returns_page_id(self):
        """任务应调用高级模型与 generate_wiki_page 并返回 page.id"""
        with patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()) as mock_llm, \
                patch('apps.wiki.generator.generate_wiki_page', return_value=_fake_page(7)) as mock_gen:
            result = generate_wiki_for_node(42)

        assert result == 7
        mock_llm.assert_called_once()
        mock_gen.assert_called_once_with(42, mock_llm.return_value)


class TestGenerateCommunityWikiTask:
    """社区 Wiki 生成任务测试"""

    @pytest.mark.unit
    def test_generate_community_returns_page_id(self):
        """任务应调用 generate_community_wiki 并透传 community_id 与 level"""
        with patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()) as mock_llm, \
                patch('apps.wiki.generator.generate_community_wiki', return_value=_fake_page(9)) as mock_gen:
            result = generate_community_wiki_task(3, level=2)

        assert result == 9
        mock_gen.assert_called_once_with(3, 2, mock_llm.return_value)


@pytest.mark.django_db
@pytest.mark.integration
class TestRefreshExpiredWikiPages:
    """过期 Wiki 页面刷新任务测试"""

    def test_no_expired_returns_zero(self):
        """无过期页面时直接返回 0，不调用 LLM"""
        with patch('apps.llm.factory.get_llm_advanced') as mock_llm:
            assert refresh_expired_wiki_pages() == 0
        mock_llm.assert_not_called()

    def test_refresh_expired_with_node(self):
        """有过期页面且存在节点时逐个刷新并计数"""
        node = _make_node()
        WikiPage.objects.create(title='过期页', node=node, status='expired')
        with patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()), \
                patch('apps.wiki.generator.generate_wiki_page',
                      return_value=_fake_page(5)) as mock_gen:
            count = refresh_expired_wiki_pages()

        assert count == 1
        mock_gen.assert_called_once_with(node.id, mock_gen.call_args[0][1])

    def test_refresh_skips_page_without_node(self):
        """无 node 的过期页面应跳过，不计入刷新数"""
        node = _make_node()
        WikiPage.objects.create(title='无节点页', node=None, status='expired')
        WikiPage.objects.create(title='有节点页', node=node, status='expired')
        with patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()), \
                patch('apps.wiki.generator.generate_wiki_page',
                      return_value=_fake_page(5)) as mock_gen:
            count = refresh_expired_wiki_pages()

        assert count == 1
        assert mock_gen.call_count == 1

    def test_refresh_exception_not_counted(self):
        """单页刷新抛异常应记录日志并跳过，不影响其他页面"""
        node = _make_node()
        WikiPage.objects.create(title='会失败的页', node=node, status='expired')
        WikiPage.objects.create(title='正常的页', node=node, status='expired')
        with patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()), \
                patch('apps.wiki.generator.generate_wiki_page',
                      side_effect=[RuntimeError('llm down'), _fake_page(6)]):
            count = refresh_expired_wiki_pages()

        assert count == 1
