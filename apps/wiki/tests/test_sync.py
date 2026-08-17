"""
apps.wiki.sync 单元测试 —— Wiki 增量同步（节点级防抖派发）

覆盖范围：
- on_document_done_for_wiki：文档完成后的 Wiki 构建触发
  - 文档不存在 / 无 node_id：早返回
  - 配置关闭（WIKI_ENABLED=false）：标记 skipped，不派发
  - 正常流程：published Wiki 标记 expired + 文档置 pending + 防抖派发 build_node_wiki_task
  - 防抖：同节点已有待构建标记时不重复派发（仅首个文档完成者派发）

采用纯 mock（不依赖 DB）：
新实现用 Document.objects.filter(...).first() 查询文档，不再使用 .get()/DoesNotExist，
故 mock Document/KnowledgeNode/WikiPage 查询链与 _wiki_enabled/build_node_wiki_task，
聚焦早返回分支、状态写入与防抖派发契约。
"""
import pytest
from unittest.mock import patch, MagicMock, ANY

from apps.wiki.sync import on_document_done_for_wiki


def _fake_doc(node_id):
    """构造最小 Document 实例替身（含 node_id 属性）"""
    doc = MagicMock()
    doc.node_id = node_id
    return doc


# ============================================================================
# 文档不存在 / 无 node_id：早返回
# ============================================================================
class TestDocumentEarlyReturn:
    """文档不存在或无 node_id 时的早返回行为"""

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.KnowledgeNode')
    @patch('apps.knowledge.models.Document')
    def test_document_not_found_returns_none(self, mock_doc_model, mock_node_model, mock_wiki_page):
        """文档不存在（filter().first() 返回 None）时应早返回，不产生任何副作用"""
        mock_doc_model.objects.filter.return_value.only.return_value.first.return_value = None

        result = on_document_done_for_wiki(document_id=999)

        assert result is None
        mock_wiki_page.objects.filter.assert_not_called()
        mock_node_model.objects.filter.assert_not_called()

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.KnowledgeNode')
    @patch('apps.knowledge.models.Document')
    def test_document_without_node_id_returns_none(self, mock_doc_model, mock_node_model, mock_wiki_page):
        """文档存在但 node_id 为空时应早返回，不更新 Wiki 也不派发"""
        mock_doc_model.objects.filter.return_value.only.return_value.first.return_value = _fake_doc(None)

        result = on_document_done_for_wiki(document_id=1)

        assert result is None
        mock_wiki_page.objects.filter.assert_not_called()
        mock_node_model.objects.filter.assert_not_called()


# ============================================================================
# 配置关闭：标记 skipped，不派发
# ============================================================================
class TestWikiDisabled:
    """WIKI_ENABLED 关闭时的行为"""

    @pytest.mark.unit
    @patch('apps.wiki.tasks.build_node_wiki_task')
    @patch('apps.wiki.sync._wiki_enabled', return_value=False)
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.KnowledgeNode')
    @patch('apps.knowledge.models.Document')
    def test_disabled_marks_skipped_without_dispatch(
            self, mock_doc_model, mock_node_model, mock_wiki_page, mock_enabled, mock_task):
        """配置关闭时标记文档 wiki_status=skipped，不标记 Wiki 过期也不派发任务"""
        mock_doc_model.objects.filter.return_value.only.return_value.first.return_value = _fake_doc(42)

        on_document_done_for_wiki(document_id=1)

        # 文档标记为 skipped（未启用）
        mock_doc_model.objects.filter.assert_any_call(id=1)
        mock_doc_model.objects.filter.return_value.update.assert_called_once_with(
            wiki_status='skipped')
        # 不操作 WikiPage / 不派发任务
        mock_wiki_page.objects.filter.assert_not_called()
        mock_node_model.objects.filter.assert_not_called()
        mock_task.delay.assert_not_called()


# ============================================================================
# 正常流程：标记过期 + 置 pending + 防抖派发
# ============================================================================
class TestSyncDispatch:
    """文档完成后正常触发 Wiki 构建的流程"""

    @pytest.mark.unit
    @patch('apps.wiki.tasks.build_node_wiki_task')
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.KnowledgeNode')
    @patch('apps.knowledge.models.Document')
    def test_marks_published_wiki_expired(self, mock_doc_model, mock_node_model, mock_wiki_page, mock_task):
        """应将节点下 status='published' 的 Wiki 页面更新为 'expired'"""
        mock_doc_model.objects.filter.return_value.only.return_value.first.return_value = _fake_doc(42)
        mock_node_model.objects.filter.return_value.update.return_value = 1

        on_document_done_for_wiki(document_id=1)

        mock_wiki_page.objects.filter.assert_called_once_with(
            node_id=42, status='published')
        # 系统自动过期：记录过期时间，不记操作人 / 原因（人工过期走 views.expire）
        mock_wiki_page.objects.filter.return_value.update.assert_called_once_with(
            status='expired', expire_reason='', expired_by=None, expired_at=ANY)

    @pytest.mark.unit
    @patch('apps.wiki.tasks.build_node_wiki_task')
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.KnowledgeNode')
    @patch('apps.knowledge.models.Document')
    def test_sets_doc_pending_and_dispatches(self, mock_doc_model, mock_node_model, mock_wiki_page, mock_task):
        """文档应置 wiki_status=pending，并通过节点 check-and-set 派发构建任务"""
        mock_doc_model.objects.filter.return_value.only.return_value.first.return_value = _fake_doc(42)
        # 节点原子更新返回 1 → 本调用派发
        mock_node_model.objects.filter.return_value.update.return_value = 1

        on_document_done_for_wiki(document_id=1)

        # 文档状态写入 pending
        mock_doc_model.objects.filter.return_value.update.assert_called_once_with(
            wiki_status='pending')
        # 节点防抖标记：id + 未删除 + 当前无待构建标记
        mock_node_model.objects.filter.assert_called_once_with(
            id=42, is_deleted=False, wiki_pending=False)
        mock_node_model.objects.filter.return_value.update.assert_called_once_with(
            wiki_pending=True)
        # 派发任务（参数为节点 ID）
        mock_task.delay.assert_called_once_with(42)

    @pytest.mark.unit
    @patch('apps.wiki.tasks.build_node_wiki_task')
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.KnowledgeNode')
    @patch('apps.knowledge.models.Document')
    def test_no_dispatch_when_node_already_pending(self, mock_doc_model, mock_node_model, mock_wiki_page, mock_task):
        """同节点已有待构建标记（update 返回 0）时，文档仍置 pending 但不重复派发"""
        mock_doc_model.objects.filter.return_value.only.return_value.first.return_value = _fake_doc(42)
        # 节点已由前一个文档置为 graph_pending=True → check-and-set 失败返回 0
        mock_node_model.objects.filter.return_value.update.return_value = 0

        on_document_done_for_wiki(document_id=1)

        # 文档状态仍写入 pending（合并到已派发的节点任务中）
        mock_doc_model.objects.filter.return_value.update.assert_called_once_with(
            wiki_status='pending')
        # 不重复派发
        mock_task.delay.assert_not_called()

    @pytest.mark.unit
    @patch('apps.wiki.tasks.build_node_wiki_task')
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.KnowledgeNode')
    @patch('apps.knowledge.models.Document')
    def test_dispatch_uses_node_id_from_doc(self, mock_doc_model, mock_node_model, mock_wiki_page, mock_task):
        """派发参数应使用 doc.node_id，而非 document_id 入参"""
        mock_doc_model.objects.filter.return_value.only.return_value.first.return_value = _fake_doc(77)
        mock_node_model.objects.filter.return_value.update.return_value = 1

        on_document_done_for_wiki(document_id=10)

        mock_task.delay.assert_called_once_with(77)
