"""
apps.wiki.sync 单元测试 —— Wiki 增量同步

覆盖范围：
- on_document_done_for_wiki：文档完成后标记关联节点 Wiki 为过期
- 分支：文档不存在直接返回、文档无 node_id 直接返回、正常标记 published -> expired
- 同步契约：仅更新 status='published' 的 WikiPage，过滤 node_id 匹配

用纯 mock（不依赖 DB）：
on_document_done_for_wiki 内部 import Document/WikiPage 并执行 ORM 查询与 update，
本测试聚焦分支逻辑（早返回条件、update 过滤参数），故 mock 模型查询链，
避免真实 DB 写入与外键约束耦合。
"""
import pytest
from unittest.mock import patch, MagicMock

from django.core.exceptions import ObjectDoesNotExist

from apps.wiki.sync import on_document_done_for_wiki


# ----------------------------------------------------------------------------
# Document mock 需补齐 DoesNotExist 类属性：
# 真实模型 Document.DoesNotExist 是异常类，被 mock 替换后变成 MagicMock，
# 导致 `except Document.DoesNotExist:` 报 "catching classes that do not inherit
# from BaseException"。这里将其指回真实异常类，保证 except 语法可用。
# ----------------------------------------------------------------------------
def _make_document_model_mock():
    """构造 Document 模型 mock，补齐 DoesNotExist 异常类"""
    mock = MagicMock()
    mock.DoesNotExist = ObjectDoesNotExist
    return mock


# ============================================================================
# 文档不存在：早返回
# ============================================================================
class TestDocumentNotFound:
    """文档不存在时的早返回行为"""

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_document_not_found_returns_none(self, mock_doc_model, mock_wiki_page):
        """文档不存在时返回 None，不触发 WikiPage 更新"""
        # 真实模型的 DoesNotExist 是异常类，mock 后需补回，否则 except 语句报错
        mock_doc_model.DoesNotExist = ObjectDoesNotExist
        mock_doc_model.objects.get.side_effect = ObjectDoesNotExist()

        result = on_document_done_for_wiki(document_id=999)

        assert result is None
        # WikiPage 不应被查询
        mock_wiki_page.objects.filter.assert_not_called()

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_document_not_found_no_wiki_update(self, mock_doc_model, mock_wiki_page):
        """文档不存在时 update 不应被调用，避免误标记"""
        mock_doc_model.DoesNotExist = ObjectDoesNotExist
        mock_doc_model.objects.get.side_effect = ObjectDoesNotExist()

        on_document_done_for_wiki(document_id=999)

        mock_wiki_page.objects.filter.return_value.update.assert_not_called()


# ============================================================================
# 文档无 node_id：早返回
# ============================================================================
class TestDocumentNoNodeId:
    """文档无 node_id 时的早返回行为"""

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_no_node_id_returns_none(self, mock_doc_model, mock_wiki_page):
        """doc.node_id 为 None 时返回 None，不更新 Wiki"""
        doc = MagicMock()
        doc.node_id = None
        mock_doc_model.objects.get.return_value = doc

        result = on_document_done_for_wiki(document_id=1)

        assert result is None
        mock_wiki_page.objects.filter.assert_not_called()

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_falsy_node_id_returns_none(self, mock_doc_model, mock_wiki_page):
        """doc.node_id 为 0（falsy）时也早返回

        `if not doc.node_id` 判定 falsy 即返回，0 / None / '' 均命中。
        """
        doc = MagicMock()
        doc.node_id = 0
        mock_doc_model.objects.get.return_value = doc

        result = on_document_done_for_wiki(document_id=1)

        assert result is None
        mock_wiki_page.objects.filter.assert_not_called()


# ============================================================================
# 正常同步：标记 Wiki 为过期
# ============================================================================
class TestSyncMarkExpired:
    """文档完成后标记 Wiki 过期的正常流程测试"""

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_marks_published_wiki_expired(self, mock_doc_model, mock_wiki_page):
        """node_id 存在时，应将 status='published' 的 Wiki 更新为 'expired'"""
        doc = MagicMock()
        doc.node_id = 42
        mock_doc_model.objects.get.return_value = doc
        mock_wiki_page.objects.filter.return_value.update.return_value = 3

        on_document_done_for_wiki(document_id=1)

        # 过滤应同时按 node_id 与 status='published'
        mock_wiki_page.objects.filter.assert_called_once_with(
            node_id=42, status='published')
        # 更新为 expired
        mock_wiki_page.objects.filter.return_value.update.assert_called_once_with(
            status='expired')

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_no_published_wiki_no_error(self, mock_doc_model, mock_wiki_page):
        """节点下无 published Wiki 时 update 返回 0，不应报错"""
        doc = MagicMock()
        doc.node_id = 42
        mock_doc_model.objects.get.return_value = doc
        mock_wiki_page.objects.filter.return_value.update.return_value = 0

        # 不应抛异常
        result = on_document_done_for_wiki(document_id=1)
        assert result is None  # 函数无显式返回值
        # update 仍应被调用
        mock_wiki_page.objects.filter.return_value.update.assert_called_once_with(
            status='expired')

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_update_uses_doc_node_id(self, mock_doc_model, mock_wiki_page):
        """过滤 node_id 应取自 doc.node_id，而非 document_id 入参"""
        doc = MagicMock()
        doc.node_id = 77  # 与 document_id 不同
        mock_doc_model.objects.get.return_value = doc
        mock_wiki_page.objects.filter.return_value.update.return_value = 1

        on_document_done_for_wiki(document_id=10)

        _, kwargs = mock_wiki_page.objects.filter.call_args
        # 应使用 doc.node_id=77，而非入参 document_id=10
        assert kwargs['node_id'] == 77


# ============================================================================
# 文档查询契约
# ============================================================================
class TestDocumentQueryContract:
    """Document 查询契约测试"""

    @pytest.mark.unit
    @patch('apps.wiki.models.WikiPage')
    @patch('apps.knowledge.models.Document')
    def test_document_queried_by_id(self, mock_doc_model, mock_wiki_page):
        """Document.objects.get 应以入参 document_id 为查询条件"""
        doc = MagicMock()
        doc.node_id = 5
        mock_doc_model.objects.get.return_value = doc

        on_document_done_for_wiki(document_id=123)

        mock_doc_model.objects.get.assert_called_once_with(id=123)
