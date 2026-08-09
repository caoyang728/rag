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
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
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
    Permission, RolePermissionRel,
    TicketList, TicketStatus, TicketChangeType,
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


# ============================================================================
# DocumentViewSet 列表/查询分支
# ============================================================================
class TestDocumentListFilters(KnowledgeViewsExtraBase):
    """get_queryset 的 dept_id / visible_scope 过滤与 available_depts 缓存命中"""

    @pytest.mark.integration
    def test_list_filter_by_dept_id(self):
        """dept_id 查询参数 → 仅返回该部门文档"""
        resp = self.client.get(
            f'/api/v1/knowledge/documents/?dept_id={self.dept.id}',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        ids = {d['id'] for d in resp.json()['results']}
        assert self.doc_own_private.id in ids
        assert self.doc_other_public.id in ids

    @pytest.mark.integration
    def test_list_filter_by_visible_scope(self):
        """旧版 visible_scope=team 查询参数 → 归一化为 TEAM_ONLY 过滤"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/?visible_scope=team',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        levels = {d['visibility_level'] for d in resp.json()['results']}
        assert levels == {VisibilityLevel.TEAM_ONLY}

    @pytest.mark.integration
    def test_available_depts_cached(self):
        """第二次请求命中缓存（cache key: available_depts_list）"""
        cache.delete('available_depts_list')
        first = self.client.get(
            '/api/v1/knowledge/documents/available_depts/',
            **_auth_headers(self.super_admin))
        assert first.status_code == 200
        assert cache.get('available_depts_list') is not None
        second = self.client.get(
            '/api/v1/knowledge/documents/available_depts/',
            **_auth_headers(self.super_admin))
        assert second.status_code == 200
        assert second.json() == first.json()


# ============================================================================
# DocumentViewSet 更新/删除/硬删/重解析分支
# ============================================================================
class TestDocumentUpdateBranch(KnowledgeViewsExtraBase):
    """perform_update 可见性收窄 + 操作日志分支"""

    @pytest.mark.integration
    def test_narrow_visibility_logs_change(self):
        """可见性收窄（PUBLIC→TEAM_ONLY）→ 200 并记录 doc_visibility_change 日志"""
        doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='公开文档', file_name='pub.txt',
            visibility_level=VisibilityLevel.PUBLIC)
        resp = self.client.patch(
            f'/api/v1/knowledge/documents/{doc.id}/',
            data=json.dumps({'visibility_level': VisibilityLevel.TEAM_ONLY}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        doc.refresh_from_db()
        assert doc.visibility_level == VisibilityLevel.TEAM_ONLY
        from apps.knowledge.models import DocOperationLog
        assert DocOperationLog.objects.filter(
            action='doc_visibility_change', document=doc).exists()


class TestDocumentDestroyBranch(KnowledgeViewsExtraBase):
    """destroy 的向量/图谱清理失败兜底分支"""

    @pytest.mark.integration
    @patch('apps.retrieval.vector_store.delete_by_document',
           side_effect=RuntimeError('vector down'))
    @patch('apps.graph.sync.on_document_deleted',
           side_effect=RuntimeError('graph down'))
    def test_destroy_cleanup_failure_still_204(self, mock_graph, mock_vector):
        """向量/图谱清理异常不阻断删除 → 仍 204"""
        resp = self.client.delete(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 204
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.is_deleted is True


class TestDocumentHardDeleteBranch(KnowledgeViewsExtraBase):
    """hard_delete 的保留期等待 / 存储异常分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.doc_own_private.is_deleted = True
        self.doc_own_private.delete_time = timezone.now()
        self.doc_own_private.save(update_fields=['is_deleted', 'delete_time'])

    @pytest.mark.integration
    @override_settings(DEBUG=False)
    def test_hard_delete_within_retention_403(self):
        """DEBUG=False 且删除未满 30 天 → 403 含剩余天数"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/hard_delete/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403
        assert 'remaining_days' in resp.json()

    @pytest.mark.integration
    @override_settings(DEBUG=True)
    @patch('apps.knowledge.storage.get_document_storage')
    def test_hard_delete_storage_error_500(self, mock_storage):
        """物理文件删除异常 → 500（DEBUG=True 跳过保留期检查）"""
        mock_storage.return_value.delete.side_effect = RuntimeError('disk io')
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/hard_delete/'
            '?include_deleted=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 500
        assert '物理删除失败' in resp.json()['detail']


class TestDocumentReparseBranch(KnowledgeViewsExtraBase):
    """reparse 的 Celery 派发失败兜底"""

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    def test_reparse_dispatch_failure_still_200(self, mock_parse):
        """parse_document.delay 异常 → 仍返回 200（待手动重试）"""
        mock_parse.delay.side_effect = RuntimeError('broker down')
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/reparse/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['status'] == 'pending'


class TestDocumentDownloadBranch(KnowledgeViewsExtraBase):
    """download 无物理文件分支"""

    @pytest.mark.integration
    def test_download_no_file_path_404(self):
        """file_path 为空 → 404"""
        self.doc_own_private.file_path = ''
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/download/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404


class TestDocumentRawContentBranch(KnowledgeViewsExtraBase):
    """raw_content 的文件缺失 / OSS 拉取 / 多页前缀分支"""

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

    def _make_real_file(self, content):
        fd, path = tempfile.mkstemp(suffix='.txt')
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        self._tmp_paths.append(path)
        return path

    @pytest.mark.integration
    def test_raw_content_no_file_path_404(self):
        """file_path 为空 → 404"""
        self.doc_own_private.file_path = ''
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404

    @pytest.mark.integration
    @patch('apps.knowledge.views.get_document_storage')
    def test_raw_content_oss_success(self, mock_storage):
        """OSS 文档预览 → 经 _get_document_text 拉取签名 URL 内容"""
        mock_storage.return_value.get_url.return_value = 'http://signed/raw'
        self.doc_own_private.file_path = 'oss://bucket/docs/x.txt'
        self.doc_own_private.save(update_fields=['file_path'])

        class _FakeResp:
            def iter_content(self, chunk_size=1024 * 1024):
                yield 'OSS 内容片段'.encode('utf-8')

        with patch('requests.get', return_value=_FakeResp()):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert 'OSS 内容片段' in resp.json()['content']

    @pytest.mark.integration
    @patch('apps.knowledge.views.get_document_storage')
    def test_raw_content_oss_fetch_failed_500(self, mock_storage):
        """OSS 拉取异常 → _get_document_text 返回 None → 500"""
        mock_storage.return_value.get_url.return_value = 'http://signed/raw'
        self.doc_own_private.file_path = 'oss://bucket/docs/x.txt'
        self.doc_own_private.save(update_fields=['file_path'])
        with patch('requests.get', side_effect=RuntimeError('network')):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 500

    @pytest.mark.integration
    def test_raw_content_page_2_has_prefix_ellipsis(self):
        """page=2 时内容带前缀省略号"""
        path = self._make_real_file(('x' * 100 + '\n').encode('utf-8') * 10)
        self.doc_own_private.file_path = path
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/'
            '?page=2&page_size=100',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['content'].startswith('...')


# ============================================================================
# DocumentViewSet 授权操作分支
# ============================================================================
class TestGrantRevokeBranch(KnowledgeViewsExtraBase):
    """grant_access / revoke_grant 参数缺失分支"""

    @pytest.mark.integration
    def test_grant_allow_user_missing_uid_400(self):
        """allow_user 未传 uid → 400"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'allow_user'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_revoke_invalid_grant_type_400(self):
        """非法授权类型 → 400"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/revoke_grant/',
            data=json.dumps({'grant_type': 'INVALID', 'grant_id': 1}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_pending_access_requests_no_owned_docs_empty(self):
        """无自有文档的非管理员待审列表 → 空"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending_access_requests/',
            **_auth_headers(self.other_user))
        assert resp.status_code == 200
        assert resp.json() == []


# ============================================================================
# DocumentUploadView 补充分支
# ============================================================================
class TestDocumentUploadBranch(KnowledgeViewsExtraBase):
    """上传接口分支：非文件夹节点 / MIME 列表不匹配 / magic 异常 / 超限 / 继承可见性"""

    def _upload(self, user, **overrides):
        data = {
            'file': SimpleUploadedFile(
                overrides.pop('filename', '文档.txt'), b'hello world',
                content_type='text/plain'),
            'node_id': overrides.pop('node_id', self.category_node.id),
            'visibility_level': 'TEAM_ONLY',
        }
        data.update(overrides)
        return self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data=data,
            **_auth_headers(user))

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_to_org_node_400(self, mock_magic):
        """ROOT/ORG 节点不可直接挂文档 → 400"""
        mock_magic.from_buffer.return_value = 'text/plain'
        resp = self._upload(self.super_admin, node_id=self.dept_node.id)
        assert resp.status_code == 400
        assert '只能上传到文件夹' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_mime_list_mismatch_400(self, mock_magic):
        """扩展名映射为 MIME 列表时检测到不匹配 → 400"""
        mock_magic.from_buffer.return_value = 'application/octet-stream'
        resp = self._upload(self.super_admin, filename='a.md')
        assert resp.status_code == 400
        assert '文件类型不匹配' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    def test_upload_magic_error_400(self, mock_magic):
        """magic 检测异常 → 400 文件类型检测失败"""
        mock_magic.from_buffer.side_effect = RuntimeError('libmagic error')
        resp = self._upload(self.super_admin)
        assert resp.status_code == 400
        assert '文件类型检测失败' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    @patch('apps.knowledge.views.MAX_FILE_SIZE', 1)
    def test_upload_too_large_400(self, mock_magic):
        """文件超过大小上限 → 400"""
        mock_magic.from_buffer.return_value = 'text/plain'
        resp = self._upload(self.super_admin)
        assert resp.status_code == 400
        assert '文件大小超过限制' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    def test_upload_inherits_node_visibility(self, mock_magic, mock_parse):
        """未指定可见性 → 继承挂载节点可见范围"""
        mock_magic.from_buffer.return_value = 'text/plain'
        self.category_node.visibility_level = VisibilityLevel.DEPT_ONLY
        self.category_node.save(update_fields=['visibility_level'])
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': SimpleUploadedFile('继承.txt', b'x'),
                  'node_id': self.category_node.id},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 201
        doc = Document.objects.get(pk=resp.json()['document_id'])
        assert doc.visibility_level == VisibilityLevel.DEPT_ONLY

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value=None)
    def test_upload_save_file_none_500(self, mock_save, mock_magic, mock_parse):
        """_save_file 返回空 → 500 文件存储失败"""
        mock_magic.from_buffer.return_value = 'text/plain'
        resp = self._upload(self.super_admin)
        assert resp.status_code == 500
        assert '文件存储失败' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.views.get_document_storage')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/orphan.txt')
    def test_upload_create_failure_cleans_orphan_file(
            self, mock_save, mock_storage):
        """文档落库异常 → 清理已存文件并返回 500"""
        from apps.knowledge.models import Document as _Doc
        mock_magic = MagicMock()
        mock_magic.from_buffer.return_value = 'text/plain'
        with patch('apps.knowledge.views.magic', mock_magic), \
             patch.object(_Doc.objects, 'create', side_effect=RuntimeError('boom')):
            resp = self._upload(self.super_admin)
        assert resp.status_code == 500
        mock_storage.return_value.delete.assert_called_once_with('/tmp/orphan.txt')


class TestDocumentUploadRolePermission(KnowledgeViewsExtraBase):
    """_check_node_upload_permission 部门经理 / 团队组长分支"""

    def _make_dept_manager(self, dept):
        """构造部门经理：user.manage 权限 + 主部门归属"""
        user = _create_test_user('mgr_' + dept.code)
        user.department = dept
        user.save(update_fields=['department'])
        role = _get_or_create_role('dept_manager')
        perm, _ = Permission.objects.get_or_create(
            permission_key='user.manage',
            defaults={'permission_name': '用户管理', 'module': 'user'})
        RolePermissionRel.objects.get_or_create(
            role=role, permission=perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=user, role=role,
            defaults={'status': GrantStatus.ACTIVE})
        return user

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    def test_dept_manager_upload_in_own_dept(self, mock_magic, mock_parse):
        """部门经理向本部门子树文件夹上传 → 201"""
        mock_magic.from_buffer.return_value = 'text/plain'
        mgr = self._make_dept_manager(self.dept)
        upload = SimpleUploadedFile('部门文档.txt', b'x')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': self.category_node.id,
                  'visibility_level': 'TEAM_ONLY'},
            **_auth_headers(mgr))
        assert resp.status_code == 201

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    def test_team_leader_upload_in_own_team(self, mock_magic, mock_parse):
        """团队组长向本团队子树文件夹上传 → 201"""
        mock_magic.from_buffer.return_value = 'text/plain'
        upload = SimpleUploadedFile('组长文档.txt', b'x')
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'file': upload, 'node_id': self.category_node.id,
                  'visibility_level': 'TEAM_ONLY'},
            **_auth_headers(self.team_leader))
        assert resp.status_code == 201


class TestDocumentChunksBranch(KnowledgeViewsExtraBase):
    """DocumentChunksView 文档不存在分支"""

    @pytest.mark.integration
    def test_chunks_doc_not_found_404(self):
        """文档不存在 → 404"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/999999/chunks/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404


# ============================================================================
# PendingDocsView / CeleryStatusView 兜底分支
# ============================================================================
class TestPendingDocsCeleryFailure(KnowledgeViewsExtraBase):
    """PendingDocsView POST 导入 Celery 失败 → 500"""

    @pytest.mark.integration
    @patch.dict('sys.modules', {'apps.knowledge.tasks': None})
    def test_post_celery_import_failure_500(self):
        """tasks 模块不可导入（sys.modules 置 None）→ 500 Celery 连接失败"""
        resp = self.client.post(
            '/api/v1/knowledge/documents/pending/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 500
        assert resp.json()['ok'] is False


class TestCeleryStatusFallbackBranch(KnowledgeViewsExtraBase):
    """CeleryStatusView 队列空 → 发送测试任务探测 worker"""

    @pytest.mark.integration
    @patch('time.sleep')
    @patch('rag_project.celery.app.send_task')
    @patch('rag_project.celery.app.control.ping',
           side_effect=RuntimeError('worker unreachable'))
    @patch('redis.Redis.from_url')
    def test_ping_failed_empty_queue_probe_worker(
            self, mock_redis, mock_ping, mock_send, mock_sleep):
        """ping 失败 + 队列为空 → send_task 探测 worker，ready=True 判定正常"""
        fake_conn = MagicMock()
        # default/parse 队列为 0 触发探测分支
        fake_conn.llen.side_effect = lambda q: 0
        mock_redis.return_value = fake_conn
        probe = MagicMock()
        probe.ready.return_value = True
        mock_send.return_value = probe
        resp = self.client.get(
            '/api/v1/knowledge/celery/status/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['celery_ok'] is True
        mock_send.assert_called_once()
        mock_sleep.assert_called_once()

