"""
apps.knowledge.views 文档在线预览测试（preview / preview_page）

覆盖：
- preview：按文件类型区分渲染形态
  （PDF 页图 / Office 降级文本 / 代码与文本行模式：小文件整文件直出、大文件按行分块）
- preview_page：PDF 页图渲染 PNG / 越界 422 / 无权限 403
- 权限：can_read 校验（无权限 403）
"""
import os
import tempfile
from unittest.mock import patch

import pytest

from apps.knowledge.models import (
    ResourceBlockList, ResourceType, ShareStatus,
)
from apps.knowledge.tests.test_views import (
    _auth_headers, KnowledgeViewsExtraBase,
)


class TestDocumentPreview(KnowledgeViewsExtraBase):
    """DocumentViewSet.preview 模式区分与分页测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上跟踪临时文件，测试结束后统一清理"""
        self._init_env()
        self._tmp_paths = []
        yield
        for p in getattr(self, '_tmp_paths', []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _make_real_file(self, content, suffix='.txt'):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, 'wb') as f:
            f.write(content if isinstance(content, bytes) else content.encode('utf-8'))
        self._tmp_paths.append(path)
        return path

    def _make_pdf_file(self):
        """用 pymupdf 生成一张两页的真实 PDF，返回本地路径"""
        import fitz
        fd, path = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        doc = fitz.open()
        for i in range(2):
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 72), f'PDF preview page {i + 1}')
        doc.save(path)
        doc.close()
        self._tmp_paths.append(path)
        return path

    def _set_doc_file(self, doc, path, file_type, file_name=None, file_size=None):
        doc.file_path = path
        doc.file_type = file_type
        if file_name:
            doc.file_name = file_name
        if file_size is not None:
            doc.file_size = file_size
        doc.save(update_fields=['file_path', 'file_type', 'file_name', 'file_size'])

    @pytest.mark.integration
    def test_preview_txt_owner_200(self):
        """小 txt 文档 → mode=text，整文件直出（whole=true）"""
        path = self._make_real_file('第一行\n第二行\n第三行')
        self._set_doc_file(self.doc_own_private, path, 'txt')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['mode'] == 'text'
        assert data['whole'] is True
        assert data['start_line'] == 1
        assert data['total_pages'] == 1
        assert '第一行' in data['content']
        assert data['can_copy'] is False

    @pytest.mark.integration
    def test_preview_code_whole_when_small(self):
        """小代码文件（≤1000 行）→ mode=code，整文件直出且带语言"""
        code = '\n'.join(f'print({i})' for i in range(120))
        path = self._make_real_file(code, suffix='.py')
        self._set_doc_file(self.doc_own_private, path, 'code', file_name='demo.py')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['mode'] == 'code'
        assert data['language'] == 'python'
        assert data['whole'] is True
        assert data['total_lines'] == 120
        assert data['start_line'] == 1
        assert data['content'].count('\n') == 119
        assert 'print(0)' in data['content']
        assert 'print(119)' in data['content']

    @pytest.mark.integration
    def test_preview_code_chunked_when_large(self):
        """大代码文件（>1000 行）→ whole=false，按 offset/limit 分块且 has_more"""
        code = '\n'.join(f'print({i})' for i in range(1200))
        path = self._make_real_file(code, suffix='.py')
        self._set_doc_file(self.doc_own_private, path, 'code', file_name='big.py')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/?offset=501&limit=500',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['mode'] == 'code'
        assert data['whole'] is False
        assert data['total_lines'] == 1200
        assert data['page_size_lines'] == 500
        assert data['start_line'] == 501
        assert data['has_more'] is True
        assert data['content'].startswith('print(500)')
        assert data['content'].endswith('print(999)')
        assert 'print(499)' not in data['content']
        assert 'print(1000)' not in data['content']

    @pytest.mark.integration
    def test_preview_code_chunk_boundaries_continuous(self):
        """连续分块无缝：第 1 块与第 2 块行号衔接、无重复无遗漏"""
        code = '\n'.join(f'line{i}' for i in range(1200))
        path = self._make_real_file(code, suffix='.py')
        self._set_doc_file(self.doc_own_private, path, 'code', file_name='big.py')
        resp1 = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/?offset=1&limit=500',
            **_auth_headers(self.normal_user))
        resp2 = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/?offset=501&limit=500',
            **_auth_headers(self.normal_user))
        d1, d2 = resp1.json(), resp2.json()
        assert d1['start_line'] == 1
        assert d2['start_line'] == 501
        # 接缝为行边界，前端可直接拼接（行号连续）
        assert d1['content'].endswith('line499')
        assert d2['content'].startswith('line500')
        # 末尾分块 has_more=False
        resp3 = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/?offset=1001&limit=500',
            **_auth_headers(self.normal_user))
        d3 = resp3.json()
        assert d3['start_line'] == 1001
        assert d3['has_more'] is False

    @pytest.mark.integration
    def test_preview_text_chunked_when_large_no_ellipsis(self):
        """大文本文件 → 分块为行边界切片，不携带省略号装饰（可无缝拼接）"""
        content = ''.join(f'段落{i} 的内容内容内容\n' for i in range(2000))
        path = self._make_real_file(content, suffix='.txt')
        self._set_doc_file(self.doc_own_private, path, 'txt')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/?offset=1&limit=500',
            **_auth_headers(self.normal_user))
        data = resp.json()
        assert data['mode'] == 'text'
        assert data['whole'] is False
        assert '...' not in data['content']
        assert data['content'].startswith('段落0')
        assert data['total_lines'] == 2000

    @pytest.mark.integration
    def test_preview_pdf_image_mode(self):
        """PDF 文档 → mode=image，返回页数与页图 URL"""
        path = self._make_pdf_file()
        self._set_doc_file(self.doc_own_private, path, 'pdf', file_name='demo.pdf')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['mode'] == 'image'
        assert data['total_pages'] == 2
        assert data['page_url'].endswith(f'/documents/{self.doc_own_private.id}/preview_page/?w=1200&page=')

    @pytest.mark.integration
    def test_preview_office_without_libreoffice_degrade_to_text(self):
        """未安装 LibreOffice 时 Office 文档降级为文本模式并带降级提示"""
        path = self._make_real_file('sheet row', suffix='.xlsx')
        self._set_doc_file(self.doc_own_private, path, 'spreadsheet', file_name='a.xlsx')
        with patch('apps.knowledge.views._libreoffice_available', return_value=False):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['mode'] == 'text'
        assert 'LibreOffice' in data['fallback_notice']

    @pytest.mark.integration
    def test_preview_office_with_libreoffice_image_mode(self):
        """装有 LibreOffice 且转换成功 → Office 文档以页图模式预览"""
        pdf_path = self._make_pdf_file()
        path = self._make_real_file('sheet row', suffix='.xlsx')
        self._set_doc_file(self.doc_own_private, path, 'spreadsheet', file_name='a.xlsx')
        with patch('apps.knowledge.views._office_to_pdf_path', return_value=pdf_path):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['mode'] == 'image'
        assert data['total_pages'] == 2

    @pytest.mark.integration
    def test_preview_no_permission_403(self):
        """被拉黑（PUBLIC 文档但 Deny Override）→ 403"""
        self.doc_other_public.has_block_user = True
        self.doc_other_public.save(update_fields=['has_block_user'])
        ResourceBlockList.objects.create(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_other_public.id,
            blocked_user=self.normal_user, reason='涉密剔除', blocked_by=self.super_admin,
            status=ShareStatus.ACTIVE)
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/preview/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_preview_file_missing_404(self):
        """物理文件不存在 → 404"""
        self._set_doc_file(self.doc_own_private, '/tmp/missing_preview.txt', 'txt')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404


class TestDocumentPreviewPage(KnowledgeViewsExtraBase):
    """DocumentViewSet.preview_page 页图渲染测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上跟踪临时文件，测试结束后统一清理"""
        self._init_env()
        self._tmp_paths = []
        yield
        for p in getattr(self, '_tmp_paths', []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _make_pdf_file(self):
        """用 pymupdf 生成一张两页的真实 PDF，返回本地路径"""
        import fitz
        fd, path = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        doc = fitz.open()
        for i in range(2):
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 72), f'PDF preview page {i + 1}')
        doc.save(path)
        doc.close()
        self._tmp_paths.append(path)
        return path

    def _set_pdf_doc(self, doc, path):
        doc.file_path = path
        doc.file_type = 'pdf'
        doc.save(update_fields=['file_path', 'file_type'])

    @pytest.mark.integration
    def test_preview_page_renders_png(self):
        """渲染 PDF 第 2 页 → 200 + image/png"""
        path = self._make_pdf_file()
        self._set_pdf_doc(self.doc_own_private, path)
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview_page/?page=2',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith('image/png')
        assert len(resp.content) > 100

    @pytest.mark.integration
    def test_preview_page_out_of_range_422(self):
        """请求不存在的页码 → 422"""
        path = self._make_pdf_file()
        self._set_pdf_doc(self.doc_own_private, path)
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview_page/?page=99',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 422

    @pytest.mark.integration
    def test_preview_page_no_permission_403(self):
        """被拉黑（PUBLIC 文档但 Deny Override）→ 403"""
        self.doc_other_public.has_block_user = True
        self.doc_other_public.save(update_fields=['has_block_user'])
        ResourceBlockList.objects.create(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_other_public.id,
            blocked_user=self.normal_user, reason='涉密剔除', blocked_by=self.super_admin,
            status=ShareStatus.ACTIVE)
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/preview_page/?page=1',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_preview_page_unrenderable_422(self):
        """Office 未装 LibreOffice 无法渲染页图 → 422（供前端降级）"""
        path = self._make_pdf_file()
        self._set_pdf_doc(self.doc_own_private, path)
        with patch('apps.knowledge.views._get_preview_pdf_path', return_value=None):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview_page/?page=1',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 422
