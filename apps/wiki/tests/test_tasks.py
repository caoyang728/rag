"""
apps.wiki.tasks 测试 —— LLM Wiki Celery 任务

覆盖范围：
- build_node_wiki_task：按节点防抖构建（清标记 / 配置关闭标 skipped / 批量回写 done / 失败 raise）
- generate_wiki_for_node：节点 Wiki 生成任务透传 + 状态回写
- generate_community_wiki_task：社区 Wiki 生成任务透传
- refresh_expired_wiki_pages：过期页面刷新（无过期/有节点成功/无节点跳过/异常不计入）

采用 mock：
任务仅做参数拼接与生成器调用转发，LLM 与生成逻辑分别在 llm/generator
模块（各有专项测试），patch get_llm_advanced 与 generator 即可验证任务契约；
build_node_wiki_task / refresh_expired_wiki_pages 涉及节点文档状态回写，走 DB 集成验证。
"""
import uuid

from unittest.mock import MagicMock, patch

import pytest

from apps.wiki.tasks import (
    build_node_wiki_task,
    generate_wiki_for_node,
    generate_community_wiki_task,
    refresh_expired_wiki_pages,
)
from apps.wiki.models import WikiPage
from apps.users.models import User, Department, Team
from apps.knowledge.models import KnowledgeNode, Document, VisibilityLevel


def _make_user(username='wiki-user'):
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')


def _make_node():
    """创建知识节点（level 4 业务分类）"""
    node = KnowledgeNode.objects.create(
        root_type='company_doc', node_type='folder', node_level=4, name='Wiki节点')
    node.path = f'/{node.id}/'
    node.depth = 1
    node.save(update_fields=['path', 'depth'])
    return node


def _make_doc(node, owner, title, **extra):
    """创建已完成文档（直接 ORM 写入，绕过上传管线）

    extra 可覆盖默认字段（如 wiki_status='pending' 构造待构建文档）。
    满足 doc_owner_scope_required 约束：team_id / dept_id 至少一个非空。
    """
    dept = Department.objects.create(name=f'Wiki部-{title}', code=f'w-{uuid.uuid4().hex[:8]}')
    team = Team.objects.create(
        name=f'Wiki组-{title}', code=f'wt-{uuid.uuid4().hex[:8]}', department=dept)
    fields = {
        'node': node,
        'title': title,
        'file_name': f'{title}.txt',
        'file_type': 'txt',
        'file_size': 100,
        'file_hash': uuid.uuid4().hex,
        'file_path': '/tmp/fake.txt',
        'mime_type': 'text/plain',
        'owner': owner,
        'dept_id': dept.id,
        'team_id': team.id,
        'visibility_level': VisibilityLevel.TEAM_ONLY,
        'root_type': node.root_type,
        'status': 'done',
    }
    fields.update(extra)
    return Document.objects.create(**fields)


def _fake_page(page_id=1):
    """构造生成器返回的伪页面对象"""
    page = MagicMock()
    page.id = page_id
    page.title = '生成的Wiki'
    return page


@pytest.mark.django_db
@pytest.mark.integration
class TestBuildNodeWikiTask:
    """build_node_wiki_task 按节点防抖构建测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入节点/用户/文档"""
        self.node = _make_node()
        self.owner = _make_user('wiki-owner')
        self.doc_1 = _make_doc(self.node, self.owner, '文档1', wiki_status='pending')
        self.doc_2 = _make_doc(self.node, self.owner, '文档2', wiki_status='pending')

    def test_builds_and_writes_done(self):
        """构建成功应回写节点下已完成文档 wiki_status=done，并清除节点待构建标记"""
        with patch('apps.wiki.sync._wiki_enabled', return_value=True), \
                patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()), \
                patch('apps.wiki.generator.generate_wiki_page', return_value=_fake_page(5)):
            result = build_node_wiki_task(self.node.id)

        assert result == {'ok': True, 'processed': 2}
        self.doc_1.refresh_from_db()
        self.doc_2.refresh_from_db()
        assert self.doc_1.wiki_status == 'done'
        assert self.doc_2.wiki_status == 'done'
        # 节点待构建标记已清除（任务执行后不残留，后续文档可重新触发）
        self.node.refresh_from_db()
        assert self.node.wiki_pending is False

    def test_failure_marks_failed_and_raises(self):
        """构建失败应回写 failed 并抛异常（供任务看板记录）"""
        with patch('apps.wiki.sync._wiki_enabled', return_value=True), \
                patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()), \
                patch('apps.wiki.generator.generate_wiki_page',
                      side_effect=RuntimeError('llm down')):
            with pytest.raises(RuntimeError):
                build_node_wiki_task(self.node.id)

        self.doc_1.refresh_from_db()
        assert self.doc_1.wiki_status == 'failed'
        self.doc_2.refresh_from_db()
        assert self.doc_2.wiki_status == 'failed'

    def test_disabled_marks_skipped_without_build(self):
        """配置关闭时待构建文档标记 skipped，不调用 LLM"""
        with patch('apps.wiki.sync._wiki_enabled', return_value=False), \
                patch('apps.llm.factory.get_llm_advanced') as mock_llm:
            result = build_node_wiki_task(self.node.id)

        assert result == {'ok': True, 'processed': 0, 'skipped': True}
        mock_llm.assert_not_called()
        self.doc_1.refresh_from_db()
        assert self.doc_1.wiki_status == 'skipped'
        self.doc_2.refresh_from_db()
        assert self.doc_2.wiki_status == 'skipped'

    def test_no_docs_returns_zero(self):
        """节点下无已完成文档时直接返回 processed=0，不调用 LLM"""
        Document.objects.filter(id__in=[self.doc_1.id, self.doc_2.id]).update(status='failed')
        with patch('apps.wiki.sync._wiki_enabled', return_value=True), \
                patch('apps.llm.factory.get_llm_advanced') as mock_llm:
            result = build_node_wiki_task(self.node.id)

        assert result == {'ok': True, 'processed': 0}
        mock_llm.assert_not_called()


class TestGenerateWikiForNode:
    """节点 Wiki 生成任务测试"""

    @pytest.mark.unit
    def test_generate_for_node_returns_page_id(self):
        """任务应调用高级模型与 generate_wiki_page 并返回 page.id"""
        with patch('apps.llm.factory.get_llm_advanced', return_value=MagicMock()) as mock_llm, \
                patch('apps.wiki.generator.generate_wiki_page', return_value=_fake_page(7)) as mock_gen, \
                patch('apps.wiki.tasks._set_node_docs_wiki_status') as mock_set_status:
            result = generate_wiki_for_node(42)

        assert result == 7
        mock_llm.assert_called_once()
        mock_gen.assert_called_once_with(42, mock_llm.return_value)
        # 页面生成成功后回写该节点文档 wiki_status=done
        mock_set_status.assert_called_once_with(42, 'done')


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
