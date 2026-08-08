"""
apps.knowledge.views 文档相关补充测试 —— 恢复 / 硬删 / 重解析 / 下载 / 原文 / 上传分支

与 test_views.py 互补：
- DocumentViewSet：restore / hard_delete / reparse / download / raw_content
- DocumentUploadView 分支：node 不存在 / 无上传权限 / 不支持扩展名 / MIME 不匹配
  / 非法可见性 / 旧版 visible_scope / 同版本去重 / visibility_teams 建跨团队共享
- PendingDocsView GET/POST（重试解析）
"""
import json
import os
import tempfile
import uuid as uuid_lib
from unittest.mock import patch, MagicMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.knowledge.models import (
    KnowledgeNode, Document, ResourceShare, ResourceBlockList,
    VisibilityLevel, ResourceType, ShareScopeType, AccessLevel, ShareStatus,
)
from apps.knowledge.views import (
    _normalize_visibility_level, _encode_ticket_reason, _decode_ticket_reason,
    _extract_last_comment, _detect_file_type, _build_tree, _get_user_role,
    DocumentUploadView,
)
from apps.knowledge.tests.test_views import (
    _get_or_create_role, _create_test_user, _auth_headers, _create_document,
    KnowledgeViewsExtraBase,
)
from apps.users.models import (
    User, Role, UserRoleRel, GrantStatus, Department, Team,
    PermissionApprovalTicket, TicketStatus, TicketChangeType,
)


class TestDocumentRestore(KnowledgeViewsExtraBase):
    """DocumentViewSet.restore 测试"""

    def _soft_delete(self, doc):
        doc.is_deleted = True
        doc.delete_time = timezone.now()
        doc.save(update_fields=['is_deleted', 'delete_time'])

    @pytest.mark.integration
    def test_restore_owner_200(self):
        """Owner 恢复已删文档 → is_deleted=False + restored_by 落审计"""
        self._soft_delete(self.doc_own_private)
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/restore/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.is_deleted is False
        assert self.doc_own_private.restored_by_id == self.normal_user.id

    @pytest.mark.integration
    def test_restore_not_deleted_400(self):
        """文档未删除 → 400"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/restore/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 400
        assert '未被删除' in resp.json()['detail']

    @pytest.mark.integration
    def test_restore_non_owner_403(self):
        """非 Owner 恢复他人 PUBLIC 文档 → 403（can_read 但非 owner/manager）"""
        self._soft_delete(self.doc_other_public)
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/restore/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

class TestDocumentHardDelete(KnowledgeViewsExtraBase):
    """DocumentViewSet.hard_delete 物理删除测试（test_settings DEBUG=True 免 30 天等待）"""

    @pytest.mark.integration
    def test_hard_delete_not_deleted_400(self):
        """文档未逻辑删除 → 400"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/hard_delete/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 400
        assert '逻辑删除' in resp.json()['detail']

    @pytest.mark.integration
    def test_hard_delete_no_file_400(self):
        """已删除但无物理文件 → 400"""
        self.doc_own_private.is_deleted = True
        self.doc_own_private.file_path = ''
        self.doc_own_private.save(update_fields=['is_deleted', 'file_path'])
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/hard_delete/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 400
        assert '物理文件' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.storage.get_document_storage')
    def test_hard_delete_success(self, mock_storage):
        """已删除且有物理文件 → 删除存储文件 + 清空 file_path + 200

        get_document_storage 在视图函数内 `from apps.knowledge.storage import ...`
        导入，因此需 patch 定义处（storage 模块）。
        delete_time 保持 None：test_settings 非 DEBUG 时若设置了 delete_time
        会命中 30 天保留期检查返回 403，因此不设置删除时间以走通物理删除。
        """
        self.doc_own_private.is_deleted = True
        self.doc_own_private.file_path = '/tmp/to_delete.txt'
        self.doc_own_private.save(update_fields=['is_deleted', 'file_path'])
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/hard_delete/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        mock_storage.return_value.delete.assert_called_once_with('/tmp/to_delete.txt')
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.file_path == ''

    @pytest.mark.integration
    def test_hard_delete_non_owner_403(self):
        """非 Owner 物理删除他人文档 → 403"""
        self.doc_other_public.is_deleted = True
        self.doc_other_public.save(update_fields=['is_deleted'])
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/hard_delete/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

class TestDocumentReparse(KnowledgeViewsExtraBase):
    """DocumentViewSet.reparse 测试"""

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    def test_reparse_owner_200(self, mock_parse):
        """Owner 重新解析 → 状态置 pending + 派发解析任务"""
        self.doc_own_private.status = 'failed'
        self.doc_own_private.error_message = '旧错误'
        self.doc_own_private.save(update_fields=['status', 'error_message'])
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/reparse/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['status'] == 'pending'
        mock_parse.delay.assert_called_once_with(self.doc_own_private.id)
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.status == 'pending'
        assert self.doc_own_private.error_message == ''

    @pytest.mark.integration
    def test_reparse_non_owner_403(self):
        """非 Owner 重新解析他人文档 → 403"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/reparse/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

class TestDocumentDownload(KnowledgeViewsExtraBase):
    """DocumentViewSet.download 测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上跟踪临时文件，测试结束后统一清理

        （测试结束后统一清理临时文件）。
        """
        self._init_env()
        self._tmp_paths = []
        yield
        for p in getattr(self, '_tmp_paths', []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _make_real_file(self, content=b'file-content'):
        fd, path = tempfile.mkstemp(suffix='.txt')
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        self._tmp_paths.append(path)
        return path

    @pytest.mark.integration
    def test_download_owner_200(self):
        """Owner 下载本地文件 → 200 文件流"""
        path = self._make_real_file()
        self.doc_own_private.file_path = path
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/download/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert b''.join(resp.streaming_content) == b'file-content'

    @pytest.mark.integration
    def test_download_no_permission_403(self):
        """PUBLIC 文档可读但 allow_download=False → 403"""
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/download/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_download_file_missing_404(self):
        """Owner 下载但文件已不存在 → 404"""
        self.doc_own_private.file_path = '/tmp/definitely_missing.txt'
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/download/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404

    @pytest.mark.integration
    @patch('apps.knowledge.views.get_document_storage')
    def test_download_oss_redirect(self, mock_storage):
        """OSS 文档下载 → 302 跳转签名 URL"""
        mock_storage.return_value.get_url.return_value = 'http://signed/url'
        self.doc_own_private.file_path = 'oss://bucket/docs/x.txt'
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/download/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 302
        assert resp.url == 'http://signed/url'

class TestDocumentRawContent(KnowledgeViewsExtraBase):
    """DocumentViewSet.raw_content 预览测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上跟踪临时文件，测试结束后统一清理

        （测试结束后统一清理临时文件）。
        """
        self._init_env()
        self._tmp_paths = []
        yield
        for p in getattr(self, '_tmp_paths', []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _make_real_file(self, content):
        fd, path = tempfile.mkstemp(suffix='.txt')
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        self._tmp_paths.append(path)
        return path

    @pytest.mark.integration
    def test_raw_content_owner_200(self):
        """Owner 预览 txt 全文 → 200 + 分页元信息"""
        path = self._make_real_file('第一行\n第二行\n第三行'.encode('utf-8'))
        self.doc_own_private.file_path = path
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert '第一行' in data['content']
        assert data['total_chars'] == len('第一行\n第二行\n第三行')
        assert data['total_pages'] == 1
        assert data['can_copy'] is False

    @pytest.mark.integration
    def test_raw_content_pagination(self):
        """page_size 较小时内容截断并带省略号提示"""
        path = self._make_real_file(('x' * 100 + '\n').encode('utf-8') * 10)
        self.doc_own_private.file_path = path
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/'
            '?page_size=100',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['total_pages'] > 1
        assert data['current_page'] == 1
        assert data['content'].endswith('...')

    @pytest.mark.integration
    def test_raw_content_file_missing_404(self):
        """文件不存在 → 404"""
        self.doc_own_private.file_path = '/tmp/missing_raw.txt'
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404

class TestDocumentUploadExtra(KnowledgeViewsExtraBase):
    """上传接口补充分支测试（mock magic/_save_file/Celery）"""

    def _upload(self, user, **overrides):
        """构造上传请求，返回 (resp, upload_file)"""
        data = {
            'file': SimpleUploadedFile(
                overrides.pop('filename', '文档.txt'), b'hello world',
                content_type='text/plain'),
            'node_id': self.category_node.id,
            'visibility_level': 'TEAM_ONLY',
        }
        data.update(overrides)
        return self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data=data,
            **_auth_headers(user))

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_node_not_found_404(self, mock_magic):
        """node_id 不存在 → 404"""
        mock_magic.from_buffer.return_value = 'text/plain'
        upload = SimpleUploadedFile('a.txt', b'x', content_type='text/plain')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': 999999},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_upload_no_permission_403(self):
        """普通用户向非本团队节点上传 → 403"""
        upload = SimpleUploadedFile('a.txt', b'x', content_type='text/plain')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': self.category_node.id},
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_upload_unsupported_extension_400(self):
        """不支持的扩展名 → 400（扩展名校验先于 MIME 校验）"""
        upload = SimpleUploadedFile('a.exe', b'x', content_type='application/octet-stream')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': self.category_node.id},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '不支持的文件类型' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_mime_mismatch_400(self, mock_magic):
        """扩展名与真实 MIME 不匹配 → 400（防文件伪装）"""
        mock_magic.from_buffer.return_value = 'application/octet-stream'
        upload = SimpleUploadedFile('a.txt', b'x', content_type='text/plain')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': self.category_node.id},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '文件类型不匹配' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_invalid_visibility_400(self, mock_magic):
        """非法可见性参数 → 400"""
        mock_magic.from_buffer.return_value = 'text/plain'
        upload = SimpleUploadedFile('a.txt', b'x', content_type='text/plain')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': self.category_node.id,
                  'visibility_level': 'INVALID'},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert 'visibility_level' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/legacy.txt')
    def test_upload_legacy_visible_scope(self, mock_save, mock_magic, mock_parse):
        """旧版 visible_scope=team 兼容上传 → 201 归一化为 TEAM_ONLY"""
        mock_magic.from_buffer.return_value = 'text/plain'
        upload = SimpleUploadedFile('旧文档.txt', b'x', content_type='text/plain')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': self.category_node.id,
                  'visible_scope': 'team'},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 201
        doc = Document.objects.get(pk=resp.json()['document_id'])
        assert doc.visibility_level == VisibilityLevel.TEAM_ONLY

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_no_org_restricted_visibility_400(self, mock_magic):
        """root 下公共文件夹（无 ORG 祖先）+ 上传者无部门/团队 + 非 PUBLIC → 400

        原实现会静默降级为 PUBLIC（越权公开风险），现改为显式报错，
        且不得创建任何文档（在写文件前校验，无孤儿文件）。
        """
        mock_magic.from_buffer.return_value = 'text/plain'
        # 手动创建 root 下手动文件夹（node_kind=FOLDER，祖先链无 ORG 节点）
        folder = KnowledgeNode.objects.create(
            name='公共文件夹', node_type='folder', node_level=2, node_kind='FOLDER',
            root_type='company_doc', parent=self.root_node,
            depth=1, created_by=self.super_admin)
        padded = f'{folder.id:04d}'
        folder.path = f'{self.root_node.path}{padded}/'
        folder.save(update_fields=['path'])

        upload = SimpleUploadedFile('a.txt', b'x', content_type='text/plain')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': folder.id,
                  'visibility_level': 'TEAM_ONLY'},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '全局公开' in resp.json()['detail']
        # 不静默降级：文档不应被创建
        assert not Document.objects.filter(node=folder, is_deleted=False).exists()

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_no_org_public_ok_201(self, mock_magic):
        """root 下公共文件夹 + 上传者无部门/团队 + 显式 PUBLIC → 201（合法路径）"""
        mock_magic.from_buffer.return_value = 'text/plain'
        folder = KnowledgeNode.objects.create(
            name='公共文件夹', node_type='folder', node_level=2, node_kind='FOLDER',
            root_type='company_doc', parent=self.root_node,
            depth=1, created_by=self.super_admin)
        padded = f'{folder.id:04d}'
        folder.path = f'{self.root_node.path}{padded}/'
        folder.save(update_fields=['path'])

        upload = SimpleUploadedFile('a.txt', b'x', content_type='text/plain')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': folder.id,
                  'visibility_level': 'PUBLIC'},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 201
        doc = Document.objects.get(pk=resp.json()['document_id'])
        assert doc.visibility_level == VisibilityLevel.PUBLIC
        assert doc.dept_id is None and doc.team_id is None

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/dedup.txt')
    def test_upload_same_version_tag_dedup(self, mock_save, mock_magic, mock_parse):
        """同版本标签重复上传 → 旧文档软删 + dedup=True"""
        mock_magic.from_buffer.return_value = 'text/plain'
        payload = {'file': SimpleUploadedFile('版本文档.txt', b'v1 content'),
                   'node_id': self.category_node.id,
                   'visibility_level': 'TEAM_ONLY', 'version_tag': 'v1'}
        headers = _auth_headers(self.super_admin)
        first = self.client.post('/api/v1/knowledge/documents/upload/', data=payload, **headers)
        assert first.status_code == 201
        assert first.json()['dedup'] is False
        doc1 = Document.objects.get(pk=first.json()['document_id'])

        payload['file'] = SimpleUploadedFile('版本文档.txt', b'v1 content')
        second = self.client.post('/api/v1/knowledge/documents/upload/', data=payload, **headers)
        assert second.status_code == 201
        assert second.json()['dedup'] is True
        assert second.json()['version'] == 1
        doc1.refresh_from_db()
        assert doc1.is_deleted is True

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/shared.txt')
    def test_upload_visibility_teams_creates_share(self, mock_save, mock_magic, mock_parse):
        """visibility_teams 指定跨团队共享 → 创建 ResourceShare 并置 has_resource_share"""
        mock_magic.from_buffer.return_value = 'text/plain'
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': SimpleUploadedFile('共享文档.txt', b'x'),
                  'node_id': self.category_node.id,
                  'visibility_level': 'TEAM_ONLY',
                  'visibility_teams': [str(self.team.id)]},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 201
        doc = Document.objects.get(pk=resp.json()['document_id'])
        assert doc.has_resource_share is True
        assert ResourceShare.objects.filter(
            resource_type=ResourceType.DOCUMENT, resource_id=doc.id,
            share_scope_type=ShareScopeType.TEAM,
            share_scope_id=self.team.id, status=ShareStatus.ACTIVE).exists()


# ============================================================================
# PendingDocsView（待处理文档列表 + 重试解析）
# ============================================================================

class TestPendingDocs(KnowledgeViewsExtraBase):
    """PendingDocsView GET/POST 测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充待处理文档"""
        self._init_env()
        self.pending_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='待解析文档', file_name='pending.txt',
            status='pending')
        self.failed_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='嵌入失败文档', file_name='emb.txt',
            status='embedding_failed')

    @pytest.mark.integration
    def test_get_lists_processing_docs(self):
        """GET 仅返回当前用户进行中的文档（pending/embedding_failed）"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2
        titles = {d['title'] for d in data['documents']}
        assert '待解析文档' in titles
        assert '嵌入失败文档' in titles

    @pytest.mark.integration
    def test_get_excludes_others_docs(self):
        """GET 不返回他人文档"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending/',
            **_auth_headers(self.other_user))
        assert resp.json()['total'] == 0

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    def test_post_retrigger(self, mock_parse):
        """POST 重新派发当前用户所有 pending/embedding_failed 文档"""
        resp = self.client.post(
            '/api/v1/knowledge/documents/pending/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['retriggered'] == 2
        assert mock_parse.delay.call_count == 2

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    def test_post_partial_failure_reported(self, mock_parse):
        """部分派发失败 → failed 列表记录 doc_id 与错误"""
        mock_parse.delay.side_effect = RuntimeError('broker down')
        resp = self.client.post(
            '/api/v1/knowledge/documents/pending/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['retriggered'] == 0
        assert len(data['failed']) == 2


# ============================================================================
# 文档双审（pending-audits / audit-approve / audit-reject）
# ============================================================================

