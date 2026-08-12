"""
apps.knowledge.views 补充覆盖率测试 —— 纯函数/辅助函数缺失分支

覆盖范围（行号以 apps/knowledge/views.py 当前文件为准）：
- _log_operation 记录异常吞没（70-71）
- _get_user_role kb_admin 分支（93）
- _build_visibility_chain 团队级/部门级/超管兜底审批链（205-231）
- _can_approve_node_visibility DEPT_LEADER 属地授权命中（284）
- _text_similarity 空样本 / _sync_vectors_active 空列表（498, 528）
- _extract_text_content code/config 编码回退 gbk/latin-1（579-583）
- _extract_spreadsheet_preview CSV 全编码解码失败（611）
- 预览转换辅助：_get_preview_tmp_dir / _libreoffice_bin / _libreoffice_available
  （707-709, 714, 719）
- _read_doc_bytes OSS 流式 + max_size 截断（733）
- _ensure_local_file 本地缺失 / OSS 缓存命中 / OSS 下载落盘（749, 751-760）
- _office_to_pdf_path 缓存命中 / 转换成功 / 产物缺失 / 转换异常（771-798）
- _get_preview_pdf_path 非可预览类型（807）
- _pdf_page_count 缓存命中 / 不可预览（824, 827）
- _render_pdf_page_png 缓存命中（845）
- _count_content_lines 空文本（901）
- _validate_upload_mime zip 魔数 / 字符串精确匹配 / 列表命中（2861, 2865）
- _save_file 空文件名回退 unnamed_file（2877）
- _has_doc_audit_page_access kb.manage 权限（3069）
- _audit_step_for 非待审状态返回空文案（3118）

采用 unittest.mock 模拟外部依赖（libmagic/存储/requests/缓存），不触发真实外部服务。
"""
import hashlib
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404

from apps.knowledge.models import DocOperationLog
from apps.knowledge.tests.test_views import (
    _get_or_create_role, _create_test_user, KnowledgeViewsExtraBase,
)
from apps.knowledge.views import (
    DocumentUploadView,
    _audit_step_for,
    _build_visibility_chain,
    _can_approve_node_visibility,
    _count_content_lines,
    _ensure_local_file,
    _extract_spreadsheet_preview,
    _extract_text_content,
    _get_preview_pdf_path,
    _get_preview_tmp_dir,
    _get_user_role,
    _has_doc_audit_page_access,
    _libreoffice_available,
    _libreoffice_bin,
    _log_operation,
    _office_to_pdf_path,
    _pdf_page_count,
    _read_doc_bytes,
    _render_pdf_page_png,
    _sync_vectors_active,
    _text_similarity,
)
from apps.users.models import (
    GrantStatus, Permission, RolePermissionRel, UserRoleRel,
)


# ============================================================================
# _log_operation 异常吞没
# ============================================================================
class TestLogOperationException:
    """DocOperationLog 写入失败时日志记录不应向外抛异常"""

    @pytest.mark.unit
    def test_create_exception_swallowed(self):
        req = MagicMock()
        req.user.is_authenticated = True
        req.META = {
            'HTTP_X_FORWARDED_FOR': '',
            'REMOTE_ADDR': '127.0.0.1',
            'HTTP_USER_AGENT': 'pytest',
        }
        with patch.object(DocOperationLog.objects, 'create',
                          side_effect=RuntimeError('db down')):
            _log_operation(req, 'test_action')  # 不应抛出


# ============================================================================
# _get_user_role / 审批链（需要 DB）
# ============================================================================
@pytest.mark.django_db
class TestGetUserRoleKbAdmin(KnowledgeViewsExtraBase):
    """_get_user_role kb_admin 快路径分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_kb_admin_role_by_permission(self):
        """授予 kb.manage_all 权限 → 判定为 kb_admin"""
        kb_user = _create_test_user('kbadmin')
        role = _get_or_create_role('kb_admin')
        perm, _ = Permission.objects.get_or_create(
            permission_key='kb.manage_all',
            defaults={'permission_name': '知识库管理', 'module': 'kb'})
        RolePermissionRel.objects.get_or_create(
            role=role, permission=perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=kb_user, role=role, defaults={'status': GrantStatus.ACTIVE})
        role_name, dept_id, team_ids = _get_user_role(kb_user)
        assert role_name == 'kb_admin'
        assert dept_id is None
        assert team_ids == []


@pytest.mark.django_db
class TestBuildVisibilityChain(KnowledgeViewsExtraBase):
    """_build_visibility_chain 按发起人角色动态构建审批链"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    def _make_dept_manager(self):
        """部门经理：user.manage 权限 + 主部门归属"""
        mgr = _create_test_user('mgr_chain')
        mgr.department = self.dept
        mgr.save(update_fields=['department'])
        role = _get_or_create_role('dept_manager')
        perm, _ = Permission.objects.get_or_create(
            permission_key='user.manage',
            defaults={'permission_name': '用户管理', 'module': 'user'})
        RolePermissionRel.objects.get_or_create(
            role=role, permission=perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=mgr, role=role, defaults={'status': GrantStatus.ACTIVE})
        return mgr

    @pytest.mark.integration
    def test_team_leader_chain_has_dept_leader(self):
        """团队组长发起（节点归属部门）→ 部门经理审批链"""
        chain = _build_visibility_chain(self.team_leader, self.category_node)
        assert len(chain) == 1
        assert chain[0]['step'] == 0
        assert chain[0]['approver_role'] == 'DEPT_LEADER'
        assert chain[0]['approver_scope_id'] == self.dept.id

    @pytest.mark.integration
    def test_dept_manager_chain_has_kb_admin(self):
        """部门经理发起 → 文档管理员/超管审批链"""
        mgr = self._make_dept_manager()
        chain = _build_visibility_chain(mgr, self.category_node)
        assert len(chain) == 1
        assert chain[0]['step'] == 0
        assert chain[0]['approver_role'] == 'KB_ADMIN'

    @pytest.mark.integration
    def test_admin_chain_double_kb_admin(self):
        """超管/文档管理员发起或归属缺失 → 双管理员复核链"""
        chain = _build_visibility_chain(self.super_admin, self.category_node)
        assert [s['approver_role'] for s in chain] == ['KB_ADMIN', 'KB_ADMIN']
        assert [s['step'] for s in chain] == [0, 1]

    @pytest.mark.integration
    def test_can_approve_dept_leader_scope_match(self):
        """DEPT_LEADER 步骤且 scope 命中用户属地部门 → 可审批"""
        mgr = self._make_dept_manager()
        assert _can_approve_node_visibility(
            mgr, self.category_node,
            {'approver_role': 'DEPT_LEADER',
             'approver_scope_id': self.dept.id}) is True


# ============================================================================
# 版本判定 / 向量同步纯函数
# ============================================================================
class TestVersionAndVectorHelpers:
    @pytest.mark.unit
    def test_text_similarity_empty_sample(self):
        """任一样本为空 → 相似度 0.0"""
        assert _text_similarity('', 'abc') == 0.0
        assert _text_similarity('abc', '') == 0.0

    @pytest.mark.unit
    def test_sync_vectors_active_empty_ids(self):
        """空 ID 列表直接返回，不触发向量查询"""
        with patch('apps.retrieval.models.DocumentVector.objects.filter') as m:
            _sync_vectors_active([], True)
            m.assert_not_called()


# ============================================================================
# 文本提取编码回退 / CSV 解码失败
# ============================================================================
class TestExtractTextCodeFallback:
    """code/config 文件 utf-8 解码失败 → gbk → latin-1"""

    @pytest.mark.unit
    def test_code_gbk_fallback(self):
        """code 文件 gbk 编码内容 → gbk 回退解码"""
        assert _extract_text_content('中文'.encode('gbk'), 'code', 'a.py') == '中文'

    @pytest.mark.unit
    def test_config_latin1_fallback(self):
        """config 文件 utf-8/gbk 均失败 → latin-1 兜底"""
        out = _extract_text_content(b'\xff\xfe\x81', 'config', 'a.conf')
        assert isinstance(out, str)
        assert len(out) == 3


class TestSpreadsheetCsvAllDecodeFail:
    @pytest.mark.unit
    def test_csv_all_encodings_fail(self):
        """CSV 全编码解码失败 → 解码失败提示"""

        class _FailDecode:
            def decode(self, encoding, *args, **kwargs):
                raise UnicodeDecodeError(encoding, b'', 0, 1, 'invalid')

        assert _extract_spreadsheet_preview(_FailDecode(), '.csv') == 'CSV 文件解码失败'


# ============================================================================
# 预览转换辅助函数
# ============================================================================
class TestPreviewHelpers:
    """预览链路纯辅助函数分支（全部 mock 外部依赖）"""

    @pytest.mark.unit
    def test_get_preview_tmp_dir_creates(self):
        """预览临时目录统一落位 scripts/tmp/preview 并自动创建"""
        d = _get_preview_tmp_dir()
        assert d.endswith(os.path.join('scripts', 'tmp', 'preview'))
        assert os.path.isdir(d)

    @pytest.mark.unit
    def test_libreoffice_bin_falls_back_to_soffice(self):
        """libreoffice 未安装时回退查找 soffice"""
        with patch('apps.knowledge.views.shutil.which',
                   side_effect=[None, '/usr/bin/soffice']):
            assert _libreoffice_bin() == '/usr/bin/soffice'

    @pytest.mark.unit
    def test_libreoffice_available_true(self):
        """检测到 soffice 可执行文件 → 可用"""
        with patch('apps.knowledge.views._libreoffice_bin',
                   return_value='/usr/bin/soffice'):
            assert _libreoffice_available() is True

    def _oss_doc(self, **over):
        doc = MagicMock()
        doc.file_path = over.pop('file_path', 'oss://bucket/obj')
        doc.id = over.pop('doc_id', 1)
        doc.file_size = over.pop('file_size', 10)
        doc.file_name = over.pop('file_name', 'a.docx')
        return doc

    @pytest.mark.unit
    def test_read_doc_bytes_oss_max_size(self):
        """OSS 文件流式读取并受 max_size 截断"""
        doc = self._oss_doc()
        storage = MagicMock()
        storage.get_url.return_value = 'http://example.com/obj'
        resp = MagicMock()
        resp.iter_content.return_value = iter([b'abcd', b'ef'])
        fake_requests = MagicMock()
        fake_requests.get.return_value = resp
        with patch('apps.knowledge.views.get_document_storage',
                   return_value=storage), \
             patch('requests.get', fake_requests.get):
            content = _read_doc_bytes(doc, max_size=6)
        assert content == b'abcdef'

    @pytest.mark.unit
    def test_ensure_local_file_missing_404(self):
        """本地物理文件缺失 → Http404"""
        doc = MagicMock()
        doc.file_path = '/tmp/definitely_missing_cov_file.txt'
        with pytest.raises(Http404):
            _ensure_local_file(doc)

    @pytest.mark.unit
    def test_ensure_local_file_oss_cached(self):
        """OSS 文件已有本地缓存 → 直接复用缓存路径"""
        fd, path = tempfile.mkstemp(suffix='.bin')
        os.close(fd)
        try:
            doc = self._oss_doc()
            fake_cache = MagicMock()
            fake_cache.get.return_value = path
            with patch('apps.knowledge.views.cache', fake_cache):
                assert _ensure_local_file(doc) == path
            fake_cache.get.assert_called_once()
        finally:
            os.unlink(path)

    @pytest.mark.unit
    def test_ensure_local_file_oss_downloads(self):
        """OSS 文件无缓存 → 下载落盘并写入缓存"""
        tmpdir = tempfile.mkdtemp()
        doc = self._oss_doc()
        fake_cache = MagicMock()
        fake_cache.get.return_value = None
        with patch('apps.knowledge.views.cache', fake_cache), \
             patch('apps.knowledge.views._get_preview_tmp_dir',
                   return_value=tmpdir), \
             patch('apps.knowledge.views._read_doc_bytes', return_value=b'DATA'):
            out = _ensure_local_file(doc)
        assert os.path.exists(out)
        with open(out, 'rb') as f:
            assert f.read() == b'DATA'
        fake_cache.set.assert_called_once()

    @pytest.mark.unit
    def test_office_to_pdf_path_cached(self):
        """转换产物命中缓存 → 直接返回 PDF 路径"""
        fd, pdf_path = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        try:
            doc = self._oss_doc(file_path='/tmp/src.docx', file_name='a.docx')
            fake_cache = MagicMock()
            fake_cache.get.return_value = pdf_path
            with patch('apps.knowledge.views.cache', fake_cache), \
                 patch('apps.knowledge.views._libreoffice_available',
                       return_value=True):
                assert _office_to_pdf_path(doc) == pdf_path
        finally:
            os.unlink(pdf_path)

    def _convert_env(self):
        """构造转换环境：临时目录 + Office 文档 doc"""
        tmpdir = tempfile.mkdtemp()
        doc = self._oss_doc(file_path='/tmp/src.docx', file_name='a.docx')
        return tmpdir, doc

    @pytest.mark.unit
    def test_office_to_pdf_path_convert_success(self):
        """LibreOffice 转换成功 → 返回产物路径并写缓存"""
        tmpdir, doc = self._convert_env()
        out_dir = os.path.join(tmpdir, 'out')
        os.makedirs(out_dir)
        src_name = (f'doc_{doc.id}_'
                    f'{hashlib.md5(doc.file_path.encode("utf-8")).hexdigest()[:8]}'
                    f'{os.path.splitext(doc.file_name)[1]}')
        pdf_path = os.path.join(out_dir, os.path.splitext(src_name)[0] + '.pdf')
        with open(pdf_path, 'wb') as f:
            f.write(b'%PDF')
        fake_cache = MagicMock()
        fake_cache.get.return_value = None
        with patch('apps.knowledge.views.cache', fake_cache), \
             patch('apps.knowledge.views._libreoffice_available',
                   return_value=True), \
             patch('apps.knowledge.views._libreoffice_bin',
                   return_value='/usr/bin/soffice'), \
             patch('apps.knowledge.views._read_doc_bytes', return_value=b'data'), \
             patch('apps.knowledge.views._get_preview_tmp_dir',
                   return_value=tmpdir), \
             patch('apps.knowledge.views.subprocess.run') as mock_run:
            out = _office_to_pdf_path(doc)
        assert out == pdf_path
        mock_run.assert_called_once()
        fake_cache.set.assert_called_once()

    @pytest.mark.unit
    def test_office_to_pdf_path_output_missing(self):
        """转换命令成功但产物缺失 → None"""
        tmpdir, doc = self._convert_env()
        fake_cache = MagicMock()
        fake_cache.get.return_value = None
        with patch('apps.knowledge.views.cache', fake_cache), \
             patch('apps.knowledge.views._libreoffice_available',
                   return_value=True), \
             patch('apps.knowledge.views._libreoffice_bin',
                   return_value='/usr/bin/soffice'), \
             patch('apps.knowledge.views._read_doc_bytes', return_value=b'data'), \
             patch('apps.knowledge.views._get_preview_tmp_dir',
                   return_value=tmpdir), \
             patch('apps.knowledge.views.subprocess.run'):
            assert _office_to_pdf_path(doc) is None

    @pytest.mark.unit
    def test_office_to_pdf_path_exception(self):
        """转换过程抛异常 → 记录日志并返回 None"""
        tmpdir, doc = self._convert_env()
        fake_cache = MagicMock()
        fake_cache.get.return_value = None
        with patch('apps.knowledge.views.cache', fake_cache), \
             patch('apps.knowledge.views._libreoffice_available',
                   return_value=True), \
             patch('apps.knowledge.views._read_doc_bytes', return_value=b'data'), \
             patch('apps.knowledge.views._get_preview_tmp_dir',
                   return_value=tmpdir), \
             patch('apps.knowledge.views.subprocess.run',
                   side_effect=RuntimeError('lo crash')):
            assert _office_to_pdf_path(doc) is None

    @pytest.mark.unit
    def test_get_preview_pdf_path_other_returns_none(self):
        """非 PDF/Office 类型 → 无可渲染 PDF 路径"""
        doc = MagicMock()
        doc.file_type = 'txt'
        assert _get_preview_pdf_path(doc) is None

    @pytest.mark.unit
    def test_pdf_page_count_cached(self):
        """页数命中缓存 → 直接返回"""
        doc = self._oss_doc()
        fake_cache = MagicMock()
        fake_cache.get.return_value = 7
        with patch('apps.knowledge.views.cache', fake_cache):
            assert _pdf_page_count(doc) == 7

    @pytest.mark.unit
    def test_pdf_page_count_unavailable_zero(self):
        """不可预览（无 PDF 路径）→ 0 页"""
        doc = self._oss_doc()
        fake_cache = MagicMock()
        fake_cache.get.return_value = None
        with patch('apps.knowledge.views.cache', fake_cache), \
             patch('apps.knowledge.views._open_pdf_document',
                   return_value=None):
            assert _pdf_page_count(doc) == 0

    @pytest.mark.unit
    def test_render_pdf_page_png_cached(self):
        """页图命中缓存 → 直接返回 PNG 字节"""
        doc = self._oss_doc()
        fake_cache = MagicMock()
        fake_cache.get.return_value = b'PNGDATA'
        with patch('apps.knowledge.views.cache', fake_cache):
            assert _render_pdf_page_png(doc, 1, 1200) == b'PNGDATA'

    @pytest.mark.unit
    def test_count_content_lines_empty(self):
        """空文本 → 0 行"""
        assert _count_content_lines('') == 0


# ============================================================================
# 上传 MIME 校验 / 文件名清理
# ============================================================================
class TestValidateUploadMimeMore:
    """_validate_upload_mime zip 魔数 / 字符串 / 列表分支"""

    @pytest.mark.unit
    def test_zip_octet_stream_magic_ok(self):
        """zip 容器误报 octet-stream 时校验 PK 魔数后放行"""
        assert DocumentUploadView._validate_upload_mime(
            '.docx', 'application/octet-stream', b'PK\x03\x04xxxx', 'any') is True

    @pytest.mark.unit
    def test_zip_octet_stream_magic_mismatch(self):
        """zip 容器魔数不符 → 拒绝"""
        assert DocumentUploadView._validate_upload_mime(
            '.docx', 'application/octet-stream', b'NOTZIP', 'any') is False

    @pytest.mark.unit
    def test_zip_exact_string_match(self):
        """zip 类扩展名非 octet-stream → 与预期 MIME 字符串精确匹配"""
        mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        assert DocumentUploadView._validate_upload_mime(
            '.docx', mime, b'x', mime) is True

    @pytest.mark.unit
    def test_zip_string_mismatch(self):
        """zip 类扩展名 MIME 不匹配 → 拒绝"""
        assert DocumentUploadView._validate_upload_mime(
            '.docx', 'application/pdf', b'x',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document') is False

    @pytest.mark.unit
    def test_other_ext_list_match(self):
        """非文本/非 zip 扩展名命中预期 MIME 列表 → 放行"""
        assert DocumentUploadView._validate_upload_mime(
            '.ppt', 'application/vnd.ms-powerpoint', b'x',
            ['application/vnd.ms-powerpoint', 'application/octet-stream']) is True


class TestSaveFileUnnamed:
    """_save_file 文件名清理后为空 → unnamed_file 兜底"""

    @pytest.mark.unit
    def test_empty_safe_name_fallback(self):
        storage = MagicMock()
        storage.save.return_value = '/tmp/saved_cov.bin'
        f = SimpleUploadedFile('!!!', b'x')
        node = MagicMock()
        with patch('apps.knowledge.views.django_text.get_valid_filename',
                   return_value=''), \
             patch('apps.knowledge.storage.get_document_storage',
                   return_value=storage), \
             patch('apps.knowledge.storage.generate_node_storage_path',
                   return_value='kb/1'):
            out = DocumentUploadView()._save_file(f, node)
        assert out == '/tmp/saved_cov.bin'
        saved_name = storage.save.call_args[0][0]
        assert 'unnamed_file' in saved_name


# ============================================================================
# 文档审核辅助
# ============================================================================
@pytest.mark.django_db
class TestDocAuditPageAccessKbManage(KnowledgeViewsExtraBase):
    """_has_doc_audit_page_access kb.manage 权限分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_kb_manage_permission_allows(self):
        """仅有 kb.manage（非超管/kb_admin）→ 放行"""
        user = _create_test_user('kb_mgr')
        role = _get_or_create_role('kb_editor')
        perm, _ = Permission.objects.get_or_create(
            permission_key='kb.manage',
            defaults={'permission_name': '知识库操作', 'module': 'kb'})
        RolePermissionRel.objects.get_or_create(
            role=role, permission=perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=user, role=role, defaults={'status': GrantStatus.ACTIVE})
        assert _has_doc_audit_page_access(user) is True


class TestAuditStepForOtherStatus:
    """_audit_step_for 非待审状态 → 空文案"""

    @pytest.mark.unit
    def test_non_pending_status_returns_empty(self):
        user = MagicMock()
        user.is_super_admin = False
        user.is_kb_admin = False
        doc = MagicMock()
        doc.audit_status = 'passed'
        doc.team_id = 1
        doc.dept_id = 1
        assert _audit_step_for(user, doc, {1}, {1}) == ''
