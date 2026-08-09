"""
apps.knowledge.views 内部辅助函数测试 —— 纯函数提取预览 + 权限/可见性判定分支

覆盖范围：
- _extract_text_content：文本/PDF/DOCX/代码/电子表格/演示文稿/未知类型
  （含编码回退 utf-8→gbk→latin-1 与解析库异常分支）
- _extract_spreadsheet_preview：CSV 编码回退 / XLSX 读取 / XLS 旧格式
- _extract_presentation_preview：PPTX 读取 / PPT 旧格式
- _get_user_role：contributor 角色分支
- _resolve_node_visibility：祖先链可见性继承与 PUBLIC 兜底
- _resolve_node_org：祖先链组织归属还原
- _get_dept_node_paths / _get_team_node_paths：空输入与 path 前缀查询
- _can_approve_node_visibility：各 approver_role 分支
- _validate_visibility_level：非法值 / 管理员 / 三档层级
"""
import io
from unittest.mock import MagicMock, patch

import pytest

from apps.knowledge.models import KnowledgeNode, VisibilityLevel
from apps.knowledge.views import (
    _extract_text_content, _extract_spreadsheet_preview, _extract_presentation_preview,
    _get_user_role, _resolve_node_visibility, _resolve_node_org,
    _get_dept_node_paths, _get_team_node_paths, _can_approve_node_visibility,
    _validate_visibility_level,
)
from apps.knowledge.tests.test_views import (
    _get_or_create_role, _create_test_user, KnowledgeViewsTestBase,
)
from apps.users.models import UserRoleRel, GrantStatus


# ============================================================================
# _extract_text_content 纯函数
# ============================================================================
class TestExtractTextContent:
    """_extract_text_content 各文件类型与异常分支"""

    @pytest.mark.unit
    def test_txt_utf8(self):
        """utf-8 文本直接解码"""
        assert _extract_text_content('你好'.encode('utf-8'), 'txt', 'a.txt') == '你好'

    @pytest.mark.unit
    def test_txt_gbk_fallback(self):
        """utf-8 解码失败回退 gbk"""
        assert _extract_text_content('中文'.encode('gbk'), 'txt', 'a.txt') == '中文'

    @pytest.mark.unit
    def test_txt_latin1_fallback(self):
        """utf-8/gbk 均失败回退 latin-1（忽略错误）"""
        data = b'\xff\xfe\x81'
        out = _extract_text_content(data, 'txt', 'a.txt')
        assert isinstance(out, str)

    @pytest.mark.unit
    @patch('pypdf.PdfReader')
    def test_pdf_extracts_text(self, mock_reader_cls):
        """PDF 有文本 → 逐页拼接"""
        page = MagicMock()
        page.extract_text.return_value = '第一页'
        mock_reader_cls.return_value.pages = [page]
        out = _extract_text_content(b'%PDF', 'pdf', 'a.pdf')
        assert '第一页' in out

    @pytest.mark.unit
    @patch('pypdf.PdfReader')
    def test_pdf_empty_text(self, mock_reader_cls):
        """PDF 无文本 → 提示无文本内容"""
        page = MagicMock()
        page.extract_text.return_value = '   '
        mock_reader_cls.return_value.pages = [page]
        assert '无文本内容' in _extract_text_content(b'%PDF', 'pdf', 'a.pdf')

    @pytest.mark.unit
    @patch('pypdf.PdfReader', side_effect=ImportError)
    def test_pdf_import_error(self, mock_reader_cls):
        """pypdf 缺失 → 安装提示"""
        assert '需要安装 pypdf' in _extract_text_content(b'%PDF', 'pdf', 'a.pdf')

    @pytest.mark.unit
    @patch('pypdf.PdfReader', side_effect=RuntimeError('corrupt'))
    def test_pdf_parse_error(self, mock_reader_cls):
        """PDF 解析异常 → 解析失败信息"""
        assert 'PDF 解析失败' in _extract_text_content(b'%PDF', 'pdf', 'a.pdf')

    @pytest.mark.unit
    @patch('docx.Document')
    def test_docx_extracts_paragraphs(self, mock_doc_cls):
        """DOCX 段落拼接"""
        para = MagicMock()
        para.text = '段落一'
        mock_doc_cls.return_value.paragraphs = [para]
        out = _extract_text_content(b'PK', 'docx', 'a.docx')
        assert '段落一' in out

    @pytest.mark.unit
    @patch('docx.Document', side_effect=ImportError)
    def test_docx_import_error(self, mock_doc_cls):
        """python-docx 缺失 → 安装提示"""
        assert '需要安装 python-docx' in _extract_text_content(b'PK', 'docx', 'a.docx')

    @pytest.mark.unit
    @patch('docx.Document', side_effect=RuntimeError('bad'))
    def test_docx_parse_error(self, mock_doc_cls):
        """DOCX 解析异常 → 解析失败信息"""
        assert 'Word 解析失败' in _extract_text_content(b'PK', 'docx', 'a.docx')

    @pytest.mark.unit
    def test_code_file(self):
        """代码/配置文件直接解码"""
        out = _extract_text_content('def f(): pass'.encode('utf-8'), 'code', 'a.py')
        assert 'def f()' in out

    @pytest.mark.unit
    def test_unknown_utf8(self):
        """未知类型 utf-8 可解码 → 原文"""
        out = _extract_text_content('普通文本'.encode('utf-8'), 'other', 'a.xyz')
        assert '普通文本' in out

    @pytest.mark.unit
    def test_unknown_binary(self):
        """未知类型非 utf-8 字节 → 无法预览提示"""
        out = _extract_text_content(b'\xff\xfe\x01', 'other', 'a.xyz')
        assert '无法预览' in out

    @pytest.mark.unit
    @patch('apps.knowledge.views._extract_spreadsheet_preview', return_value='sheet')
    def test_spreadsheet_delegates(self, mock_prev):
        """电子表格委托 _extract_spreadsheet_preview"""
        assert _extract_text_content(b'x', 'spreadsheet', 'a.xlsx') == 'sheet'
        mock_prev.assert_called_once()

    @pytest.mark.unit
    @patch('apps.knowledge.views._extract_presentation_preview', return_value='slides')
    def test_presentation_delegates(self, mock_prev):
        """演示文稿委托 _extract_presentation_preview"""
        assert _extract_text_content(b'x', 'presentation', 'a.pptx') == 'slides'
        mock_prev.assert_called_once()


# ============================================================================
# _extract_spreadsheet_preview 纯函数
# ============================================================================
class TestExtractSpreadsheetPreview:
    """_extract_spreadsheet_preview CSV/XLSX/XLS 分支"""

    @pytest.mark.unit
    def test_csv_utf8(self):
        """CSV utf-8 直接解码"""
        assert _extract_spreadsheet_preview('a,b\n1,2'.encode('utf-8'), '.csv') == 'a,b\n1,2'

    @pytest.mark.unit
    def test_csv_gbk_fallback(self):
        """CSV utf-8 失败回退 gbk"""
        assert _extract_spreadsheet_preview('中文'.encode('gbk'), '.csv') == '中文'

    @pytest.mark.unit
    def test_csv_latin1_fallback(self):
        """CSV 所有字节均可被 latin-1 解码 → 回退 latin-1 原文"""
        out = _extract_spreadsheet_preview(b'\xff\xfe\xff\xfe', '.csv')
        assert out == '\xff\xfe\xff\xfe'

    @pytest.mark.unit
    @patch('openpyxl.load_workbook')
    def test_xlsx_iterates_rows(self, mock_load):
        """XLSX 遍历工作表与数据行"""
        ws = MagicMock()
        ws.title = 'Sheet1'
        ws.iter_rows.return_value = iter([('a', 1), (None, '')])
        mock_load.return_value.worksheets = [ws]
        mock_load.return_value.close = MagicMock()
        out = _extract_spreadsheet_preview(b'PK', '.xlsx')
        assert 'Sheet1' in out
        assert 'a | 1' in out

    @pytest.mark.unit
    @patch('openpyxl.load_workbook', side_effect=ImportError)
    def test_xlsx_import_error(self, mock_load):
        """openpyxl 缺失 → 安装提示"""
        assert '需要安装 openpyxl' in _extract_spreadsheet_preview(b'PK', '.xlsx')

    @pytest.mark.unit
    @patch('openpyxl.load_workbook', side_effect=RuntimeError('corrupt'))
    def test_xlsx_parse_error(self, mock_load):
        """XLSX 解析异常 → 解析失败信息"""
        assert '电子表格解析失败' in _extract_spreadsheet_preview(b'PK', '.xlsx')

    @pytest.mark.unit
    def test_xls_unsupported(self):
        """XLS 旧版格式 → 下载提示"""
        assert '.xls 旧版格式不支持预览' in _extract_spreadsheet_preview(b'x', '.xls')

    @pytest.mark.unit
    def test_unknown_ext(self):
        """未知扩展名 → 不支持提示"""
        assert _extract_spreadsheet_preview(b'x', '.zzz') == '不支持的电子表格格式'


# ============================================================================
# _extract_presentation_preview 纯函数
# ============================================================================
class TestExtractPresentationPreview:
    """_extract_presentation_preview PPTX/PPT 分支"""

    @pytest.mark.unit
    @patch('pptx.Presentation')
    def test_pptx_extracts_shapes(self, mock_prs_cls):
        """PPTX 提取有文本的形状"""
        shape = MagicMock()
        shape.has_text_frame = True
        para = MagicMock()
        para.text = '标题文本'
        shape.text_frame.paragraphs = [para]
        slide = MagicMock()
        slide.shapes = [shape]
        mock_prs_cls.return_value.slides = [slide]
        out = _extract_presentation_preview(b'PK', '.pptx')
        assert '幻灯片 1' in out
        assert '标题文本' in out

    @pytest.mark.unit
    @patch('pptx.Presentation')
    def test_pptx_no_text(self, mock_prs_cls):
        """PPTX 无文本 → 无文本内容提示"""
        mock_prs_cls.return_value.slides = []
        assert _extract_presentation_preview(b'PK', '.pptx') == '演示文稿无文本内容'

    @pytest.mark.unit
    @patch('pptx.Presentation', side_effect=ImportError)
    def test_pptx_import_error(self, mock_prs_cls):
        """python-pptx 缺失 → 安装提示"""
        assert '需要安装 python-pptx' in _extract_presentation_preview(b'PK', '.pptx')

    @pytest.mark.unit
    @patch('pptx.Presentation', side_effect=RuntimeError('corrupt'))
    def test_pptx_parse_error(self, mock_prs_cls):
        """PPTX 解析异常 → 解析失败信息"""
        assert '演示文稿解析失败' in _extract_presentation_preview(b'PK', '.pptx')

    @pytest.mark.unit
    def test_ppt_unsupported(self):
        """PPT 旧版格式 → 下载提示"""
        assert '.ppt 旧版格式不支持预览' in _extract_presentation_preview(b'x', '.ppt')

    @pytest.mark.unit
    def test_unknown_ext(self):
        """未知扩展名 → 不支持提示"""
        assert _extract_presentation_preview(b'x', '.zzz') == '不支持的演示文稿格式'


# ============================================================================
# _get_user_role / 可见性 / 组织归属 / 审批权限（需要 DB）
# ============================================================================
@pytest.mark.django_db
class TestUserRoleContributor(KnowledgeViewsTestBase):
    """_get_user_role contributor 角色分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_contributor_role_returns_contributor(self):
        """显式授予 contributor 角色 → 返回 ('contributor', dept_id, team_ids)"""
        contributor_role = _get_or_create_role('contributor')
        UserRoleRel.objects.get_or_create(
            user=self.normal_user, role=contributor_role,
            defaults={'status': GrantStatus.ACTIVE})
        role, dept_id, team_ids = _get_user_role(self.normal_user)
        assert role == 'contributor'
        assert dept_id is None

    @pytest.mark.integration
    def test_viewer_fallback_none(self):
        """无 contributor 角色 → 返回 None（调用方视为无上传权限）"""
        role, _dept_id, _team_ids = _get_user_role(self.normal_user)
        assert role is None


@pytest.mark.django_db
class TestResolveNodeVisibility(KnowledgeViewsTestBase):
    """_resolve_node_visibility 祖先链继承"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_node_with_own_visibility(self):
        """节点自身设置了可见范围 → 直接返回"""
        self.category_node.visibility_level = VisibilityLevel.DEPT_ONLY
        self.category_node.save(update_fields=['visibility_level'])
        assert _resolve_node_visibility(self.category_node) == VisibilityLevel.DEPT_ONLY

    @pytest.mark.integration
    def test_inherit_from_ancestor(self):
        """节点未设置 → 沿祖先链向上取最近非空"""
        self.team_node.visibility_level = VisibilityLevel.TEAM_ONLY
        self.team_node.save(update_fields=['visibility_level'])
        assert _resolve_node_visibility(self.category_node) == VisibilityLevel.TEAM_ONLY

    @pytest.mark.integration
    def test_fallback_public(self):
        """祖先链全部未设置 → root 兜底 PUBLIC"""
        assert _resolve_node_visibility(self.category_node) == VisibilityLevel.PUBLIC


@pytest.mark.django_db
class TestResolveNodeOrg(KnowledgeViewsTestBase):
    """_resolve_node_org 组织归属还原"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_dept_and_team_org(self):
        """团队子树节点 → 还原 (dept_id, team_id)"""
        dept_id, team_id = _resolve_node_org(self.category_node)
        assert dept_id == self.dept.id
        assert team_id == self.team.id

    @pytest.mark.integration
    def test_dept_only(self):
        """部门节点直属文件夹 → (dept_id, None)"""
        dept_id, team_id = _resolve_node_org(self.dept_node)
        assert dept_id == self.dept.id
        assert team_id is None

    @pytest.mark.integration
    def test_no_org_ancestor(self):
        """无 ORG 祖先（root 下手动文件夹）→ (None, None)"""
        folder = self._create_node('公共夹', 'folder', node_level=2, parent=self.root_node)
        dept_id, team_id = _resolve_node_org(folder)
        assert dept_id is None and team_id is None


@pytest.mark.django_db
class TestDeptTeamNodePaths(KnowledgeViewsTestBase):
    """_get_dept_node_paths / _get_team_node_paths"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_empty_dept_paths(self):
        """无属地授权 → 空列表"""
        assert _get_dept_node_paths(self.normal_user) == []

    @pytest.mark.integration
    def test_empty_team_paths(self):
        """空团队 ID 列表 → 空列表"""
        assert _get_team_node_paths([]) == []

    @pytest.mark.integration
    def test_team_paths_found(self):
        """团队 ID → 对应 Level 3 节点 path"""
        paths = _get_team_node_paths([self.team.id])
        assert self.team_node.path in paths


@pytest.mark.django_db
class TestCanApproveNodeVisibility(KnowledgeViewsTestBase):
    """_can_approve_node_visibility 各审批角色分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_legacy_chain_owner_can_approve(self):
        """旧工单（无 approver_role）→ 节点所有者可审批"""
        self.category_node.owner_user = self.normal_user
        self.category_node.save(update_fields=['owner_user'])
        assert _can_approve_node_visibility(
            self.normal_user, self.category_node, {}) is True

    @pytest.mark.integration
    def test_legacy_chain_admin_can_approve(self):
        """旧工单 → 超管也可审批"""
        assert _can_approve_node_visibility(
            self.super_admin, self.category_node, {}) is True

    @pytest.mark.integration
    def test_dept_leader_admin_shortcut(self):
        """DEPT_LEADER 步骤 → 管理员直接可审"""
        assert _can_approve_node_visibility(
            self.super_admin, self.category_node,
            {'approver_role': 'DEPT_LEADER'}) is True

    @pytest.mark.integration
    def test_dept_leader_without_scope_denied(self):
        """DEPT_LEADER 步骤但无 scope/权限 → 拒绝"""
        assert _can_approve_node_visibility(
            self.normal_user, self.category_node,
            {'approver_role': 'DEPT_LEADER'}) is False

    @pytest.mark.integration
    def test_kb_admin_role(self):
        """KB_ADMIN 步骤 → 仅管理员"""
        assert _can_approve_node_visibility(
            self.super_admin, self.category_node,
            {'approver_role': 'KB_ADMIN'}) is True
        assert _can_approve_node_visibility(
            self.normal_user, self.category_node,
            {'approver_role': 'KB_ADMIN'}) is False

    @pytest.mark.integration
    def test_super_admin_role(self):
        """SUPER_ADMIN 步骤 → 仅超管"""
        assert _can_approve_node_visibility(
            self.super_admin, self.category_node,
            {'approver_role': 'SUPER_ADMIN'}) is True
        assert _can_approve_node_visibility(
            self.normal_user, self.category_node,
            {'approver_role': 'SUPER_ADMIN'}) is False

    @pytest.mark.integration
    def test_unknown_role_fallback_admin(self):
        """未知角色 → 回退管理员判定"""
        assert _can_approve_node_visibility(
            self.super_admin, self.category_node,
            {'approver_role': 'UNKNOWN'}) is True
        assert _can_approve_node_visibility(
            self.normal_user, self.category_node,
            {'approver_role': 'UNKNOWN'}) is False


@pytest.mark.django_db
class TestValidateVisibilityLevel(KnowledgeViewsTestBase):
    """_validate_visibility_level 层级校验"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_invalid_level(self):
        """非法层级值 → (False, 错误信息)"""
        is_valid, msg = _validate_visibility_level(self.normal_user, 'INVALID')
        assert is_valid is False
        assert '无效的可见性层级' in msg

    @pytest.mark.integration
    def test_admin_any_level(self):
        """管理员可设置任意层级"""
        assert _validate_visibility_level(
            self.super_admin, VisibilityLevel.PUBLIC)[0] is True

    @pytest.mark.integration
    def test_team_dept_levels_ok_for_normal(self):
        """普通用户可设置 TEAM_ONLY / DEPT_ONLY"""
        assert _validate_visibility_level(
            self.normal_user, VisibilityLevel.TEAM_ONLY)[0] is True
        assert _validate_visibility_level(
            self.normal_user, VisibilityLevel.DEPT_ONLY)[0] is True

    @pytest.mark.integration
    def test_public_ok_for_normal(self):
        """PUBLIC 创建时可设置（扩大需走工单）"""
        assert _validate_visibility_level(
            self.normal_user, VisibilityLevel.PUBLIC)[0] is True
