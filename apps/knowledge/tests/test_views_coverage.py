"""
apps.knowledge.views 补充覆盖率测试 —— API/视图缺失分支（DB 集成）

覆盖范围（行号以 apps/knowledge/views.py 当前文件为准）：
- AllowedVisibilityView 缓存未命中全量返回（963-989）
- DocumentFilter graph/wiki 流水线状态筛选（1246-1266）
- DocumentViewSet.list 非分页直出（1344-1345）
- _build_version_count_map 空输入（1355, 1361）
- DocumentViewSet.status_counts 各维度统计（1463-1511）
- perform_update 非法可见性层级 403（1546）
- reparse 图谱/Wiki 失败阶段仅重试对应构建（1693-1701）
- preview / preview_page / raw_content 无权限 403 与无物理文件 404
  （1749, 1751, 1852, 1854, 1892）
- _line_mode_response 内容获取失败 500 / offset 越界收敛（1803, 1833）
- _preview_text_response fallback_notice（1882）
- approve_access_request：node 不存在 404 / 无目标仅管理员
  / 节点可见性「目标值」写回 / 文档可见性旧格式匹配写回
  （2261-2262, 2274-2276, 2349, 2352-2355, 2358-2360）
- reject_access_request：工单不存在 404 / 节点工单非所有者 403 / 无目标仅管理员
  （2418-2419, 2425-2429, 2436-2438）
- DocumentUploadView：非法可见性 403 / 孤儿文件清理失败仍 500（2591, 2784-2785）
- QueueDepthView 正常 / 快照异常兜底（2990-2996）
- 文档审核：组长审批 pending_team（3342）/ 管理员驳回复核（3414）
  / 驳回列表越界过滤（3225）/ 审核记录删除文档与越界过滤（3292, 3294）

采用 pytest-django（django_db）+ JWT，mock 外部依赖（libmagic/存储/Celery/图谱同步）。
"""
import json
import os
import tempfile
import uuid as uuid_lib
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIRequestFactory

from apps.knowledge.models import (
    DocOperationLog, Document, VisibilityLevel,
)
from apps.knowledge.tests.test_views import (
    _auth_headers, _create_document, _create_test_user, _get_or_create_role,
    KnowledgeViewsExtraBase,
)
from apps.knowledge.views import (
    DocumentUploadView, DocumentViewSet, KnowledgeNodeViewSet,
)
from apps.users.models import (
    GrantStatus, Permission, RolePermissionRel, Team, TicketChangeType,
    TicketList, TicketPermissionDetail, TicketStatus, UserRoleRel, ScopeType,
)


# ============================================================================
# AllowedVisibilityView 缓存未命中
# ============================================================================
class TestAllowedVisibilityCacheMiss(KnowledgeViewsExtraBase):
    """allowed_visibility 缓存未命中时全量返回部门/团队"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_cache_miss_returns_full_result(self):
        resp = self.client.get(
            '/api/v1/knowledge/documents/allowed_visibility/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['role'] == 'super_admin'
        dept_ids = {d['id'] for d in data['departments']}
        assert self.dept.id in dept_ids
        team_ids = {t['id'] for t in data['teams']}
        assert self.team.id in team_ids


# ============================================================================
# DocumentFilter graph/wiki 流水线状态筛选
# ============================================================================
class TestDocumentFilterPipelineStatus(KnowledgeViewsExtraBase):
    """status=graph_*/wiki_* 筛选分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        # 基类文档统一为已完成流水线，避免干扰筛选
        Document.objects.filter(id__in=[
            self.doc_own_private.id, self.doc_other_public.id,
            self.doc_other_private.id,
        ]).update(graph_status='done', wiki_status='done')
        kw = dict(node=self.category_node, owner=self.super_admin,
                  team_id=self.team.id, dept_id=self.dept.id)
        self.g_pending = _create_document(
            **kw, title='图谱待构建', file_name='f_gp.txt',
            status='done', graph_status='pending', wiki_status='done')
        self.g_extracting = _create_document(
            **kw, title='图谱抽取中', file_name='f_ge.txt',
            status='done', graph_status='extracting', wiki_status='done')
        self.g_failed = _create_document(
            **kw, title='图谱失败', file_name='f_gf.txt',
            status='done', graph_status='failed', wiki_status='done')
        self.w_pending = _create_document(
            **kw, title='Wiki待建', file_name='f_wp.txt',
            status='done', graph_status='done', wiki_status='pending')
        self.w_extracting = _create_document(
            **kw, title='Wiki抽取中', file_name='f_we.txt',
            status='done', graph_status='done', wiki_status='extracting')
        self.w_failed = _create_document(
            **kw, title='Wiki失败', file_name='f_wf.txt',
            status='done', graph_status='done', wiki_status='failed')

    def _titles(self, status):
        resp = self.client.get(
            f'/api/v1/knowledge/documents/?status={status}',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        return {d['title'] for d in resp.json()['results']}

    @pytest.mark.integration
    def test_filter_graph_and_wiki_statuses(self):
        assert '图谱待构建' in self._titles('graph_pending')
        assert '图谱抽取中' in self._titles('graph_extracting')
        assert '图谱失败' in self._titles('graph_failed')
        assert 'Wiki待建' in self._titles('wiki_pending')
        assert 'Wiki抽取中' in self._titles('wiki_extracting')
        assert 'Wiki失败' in self._titles('wiki_failed')
        # done：整条流水线全部结束（基类文档 graph/wiki 已置 done）
        assert '我的私有文档' in self._titles('done')

    @pytest.mark.integration
    def test_filter_unknown_status_fallback(self):
        """未知状态值 → 回退到 status 精确筛选（空结果）"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/?status=some_random_status',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['results'] == []


# ============================================================================
# DocumentViewSet.list 非分页 / 版本计数空输入
# ============================================================================
class TestDocumentListNoPagination(KnowledgeViewsExtraBase):
    """list 无分页器时直接返回数组"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_list_without_pagination_returns_list(self):
        with patch.object(DocumentViewSet, 'paginate_queryset',
                          return_value=None):
            resp = self.client.get(
                '/api/v1/knowledge/documents/',
                **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) >= 3


class TestVersionCountMapEmpty(KnowledgeViewsExtraBase):
    """_build_version_count_map 空输入分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_no_ids_returns_empty(self):
        vs = DocumentViewSet()
        assert vs._build_version_count_map([MagicMock(id=None)]) == {}

    @pytest.mark.integration
    def test_unknown_ids_returns_empty(self):
        vs = DocumentViewSet()
        assert vs._build_version_count_map([MagicMock(id=99999999)]) == {}


# ============================================================================
# status_counts 各维度统计
# ============================================================================
class TestDocumentStatusCounts(KnowledgeViewsExtraBase):
    """DocumentViewSet.status_counts 各状态维度计数"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        # 基类文档统一为已完成流水线
        Document.objects.filter(id__in=[
            self.doc_own_private.id, self.doc_other_public.id,
            self.doc_other_private.id,
        ]).update(graph_status='done', wiki_status='done')
        kw = dict(node=self.category_node, owner=self.super_admin,
                  team_id=self.team.id, dept_id=self.dept.id)
        self.d_pending = _create_document(
            **kw, title='待处理', file_name='st_pending.txt', status='pending')
        self.d_parsing = _create_document(
            **kw, title='解析中', file_name='st_parsing.txt', status='parsing')
        self.d_desensitizing = _create_document(
            **kw, title='脱敏中', file_name='st_des.txt', status='desensitizing')
        self.d_chunking = _create_document(
            **kw, title='分块中', file_name='st_chunk.txt', status='chunking')
        self.d_failed = _create_document(
            **kw, title='失败', file_name='st_fail.txt', status='failed')
        self.d_done_all = _create_document(
            **kw, title='全完成', file_name='st_done.txt',
            status='done', graph_status='done', wiki_status='done')
        self.d_graph_failed = _create_document(
            **kw, title='图谱失败', file_name='st_gf.txt',
            status='done', graph_status='failed', wiki_status='done')
        self.d_wiki_pending = _create_document(
            **kw, title='Wiki待建', file_name='st_wp.txt',
            status='done', graph_status='done', wiki_status='pending')

    @pytest.mark.integration
    def test_status_counts_all_dimensions(self):
        resp = self.client.get(
            '/api/v1/knowledge/documents/status_counts/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['pending'] == 1
        assert data['parsing'] == 2            # parsing + desensitizing 合并
        assert data['chunking'] == 1
        assert data['failed'] == 1
        assert data['graph_failed'] == 1
        assert data['wiki_pending'] == 1
        # done：仅全流水线（graph/wiki done 或 skipped）结束的文档
        # = 基类 3 篇 + doc_done_all（图谱失败/Wiki待建不计入）
        assert data['done'] == 4


# ============================================================================
# perform_update 非法可见性层级
# ============================================================================
class TestDocumentUpdateInvalidVisibility(KnowledgeViewsExtraBase):
    """perform_update 可见性层级非法 → 403"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    @patch('apps.knowledge.views._validate_visibility_level',
           return_value=(False, '无权限设置该可见性层级'))
    def test_invalid_visibility_level_403(self, mock_validate):
        resp = self.client.patch(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403


# ============================================================================
# reparse 图谱/Wiki 失败阶段重试
# ============================================================================
class TestDocumentReparseStageRetry(KnowledgeViewsExtraBase):
    """reparse：解析已完成但图谱/Wiki 构建失败 → 仅重派对应构建"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.doc_own_private.status = 'done'
        self.doc_own_private.graph_status = 'failed'
        self.doc_own_private.wiki_status = 'failed'
        self.doc_own_private.save(
            update_fields=['status', 'graph_status', 'wiki_status'])

    @pytest.mark.integration
    @patch('apps.wiki.sync.on_document_done_for_wiki')
    @patch('apps.graph.sync.on_document_done')
    def test_reparse_only_retries_failed_stage(self, mock_graph, mock_wiki):
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/reparse/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['status'] == 'done'
        assert '失败阶段' in resp.json()['detail']
        mock_graph.assert_called_once_with(self.doc_own_private.id)
        mock_wiki.assert_called_once_with(self.doc_own_private.id)
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.status == 'done'


# ============================================================================
# preview / preview_page / raw_content 权限与文件缺失分支
# ============================================================================
class TestPreviewPermissionBranches(KnowledgeViewsExtraBase):
    """预览类接口 can_read 校验 / 物理文件缺失分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_preview_can_read_false_403(self):
        """get_object 通过但 preview 二次校验 can_read 失败 → 403"""
        with patch.object(DocumentViewSet, '_access',
                          side_effect=[{'can_read': True},
                                       {'can_read': False}]):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_preview_page_can_read_false_403(self):
        with patch.object(DocumentViewSet, '_access',
                          side_effect=[{'can_read': True},
                                       {'can_read': False}]):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/'
                f'preview_page/?page=1',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_raw_content_can_read_false_403(self):
        with patch.object(DocumentViewSet, '_access',
                          side_effect=[{'can_read': True},
                                       {'can_read': False}]):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/raw_content/',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_preview_no_file_path_404(self):
        self.doc_own_private.file_path = ''
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_preview_page_no_file_path_404(self):
        self.doc_own_private.file_path = ''
        self.doc_own_private.save(update_fields=['file_path'])
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/'
            f'preview_page/?page=1',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404


class TestLineModeBranches(KnowledgeViewsExtraBase):
    """行模式预览：内容获取失败 500 / offset 越界收敛 / fallback_notice"""

    @pytest.fixture(autouse=True)
    def _env(self):
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
            f.write(content if isinstance(content, bytes)
                    else content.encode('utf-8'))
        self._tmp_paths.append(path)
        return path

    def _set_txt_doc(self, path):
        self.doc_own_private.file_path = path
        self.doc_own_private.file_type = 'txt'
        self.doc_own_private.save(update_fields=['file_path', 'file_type'])

    @pytest.mark.integration
    def test_preview_text_fetch_failed_500(self):
        path = self._make_real_file('hello')
        self._set_txt_doc(path)
        with patch.object(DocumentViewSet, '_get_document_text',
                          return_value=None):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{self.doc_own_private.id}/preview/',
                **_auth_headers(self.normal_user))
        assert resp.status_code == 500
        assert resp.json()['error'] == '无法获取文件内容'

    @pytest.mark.integration
    def test_preview_offset_beyond_end_converged(self):
        """offset 越界（引用页码超出文件长度）→ 收敛到最后一屏"""
        content = '\n'.join(f'line{i}' for i in range(2000))
        path = self._make_real_file(content, suffix='.txt')
        self._set_txt_doc(path)
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/'
            f'preview/?offset=999999&limit=500',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['whole'] is False
        assert data['start_line'] == 1501
        assert data['has_more'] is False

    @pytest.mark.integration
    def test_preview_text_response_fallback_notice(self):
        """文本分页响应携带降级提示"""
        from rest_framework.request import Request
        req = Request(APIRequestFactory().get('/x/?page_size=2000'))
        vs = DocumentViewSet()
        with patch.object(DocumentViewSet, '_get_document_text',
                          return_value='hello world'):
            resp = vs._preview_text_response(
                self.doc_own_private, 1, req, fallback_notice='降级提示')
        assert resp.data['fallback_notice'] == '降级提示'
        assert resp.data['total_chars'] == 11


# ============================================================================
# approve_access_request 节点/无目标/可见性写回分支
# ============================================================================
class TestApproveAccessRequestCoverage(KnowledgeViewsExtraBase):
    """approve_access_request 缺失分支覆盖"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    def _make_dept_manager(self):
        mgr = _create_test_user('mgr_cov')
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

    def _create_ticket(self, applicant, reason, chain=None,
                       change_type=None):
        ticket = TicketList.objects.create(
            ticket_no=f'QX-COV-{uuid_lib.uuid4().hex[:12].upper()}',
            title='文档权限·工单',
            biz_type='permission',
            applicant=applicant,
            status=TicketStatus.PENDING,
            risk_level='normal',
            approval_chain=chain or [
                {'step': 0, 'approver_id': None, 'status': 'pending',
                 'comment': '', 'approved_at': None},
            ],
            current_step=0,
        )
        TicketPermissionDetail.objects.create(
            ticket=ticket,
            target_user=applicant,
            change_type=change_type or TicketChangeType.GRANT,
            role=None,
            scope_type=ScopeType.NONE,
            scope_id=None,
            reason=reason,
        )
        return ticket

    @pytest.mark.integration
    def test_approve_node_ticket_node_missing_404(self):
        """节点可见范围工单但目标节点不存在 → 404"""
        ticket = self._create_ticket(
            self.normal_user, '[node:999999:visibility_change] 目标节点已删')
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '同意'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_approve_no_target_normal_user_403(self):
        """无目标编码的工单仅管理员可审批 → 普通用户 403"""
        ticket = self._create_ticket(self.normal_user, '无前缀的普通工单理由')
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_node_visibility_writes_back_target_value(self):
        """节点可见性工单通过 → 按「目标值:」编码写回节点可见范围"""
        mgr = self._make_dept_manager()
        ticket = self._create_ticket(
            self.team_leader,
            f'[node:{self.category_node.id}:visibility_change] '
            f'申请将节点可见范围从「仅团队」调整为「仅部门」 目标值:DEPT_ONLY',
            chain=[{'step': 0, 'approver_role': 'DEPT_LEADER',
                    'approver_scope_id': self.dept.id, 'approver_id': None,
                    'status': 'pending', 'comment': '', 'approved_at': None}],
            change_type=TicketChangeType.SCOPE_CHANGE,
        )
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '同意'}),
            content_type='application/json',
            **_auth_headers(mgr))
        assert resp.status_code == 200, resp.content
        self.category_node.refresh_from_db()
        assert self.category_node.visibility_level == VisibilityLevel.DEPT_ONLY
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.EXECUTED

    @pytest.mark.integration
    def test_approve_doc_visibility_legacy_level_match(self):
        """旧格式工单（无目标值标记）→ 从申请文本匹配枚举值写回"""
        ticket = self._create_ticket(
            self.normal_user,
            f'[doc:{self.doc_own_private.id}:visibility_change] '
            f'申请将文档可见性从「仅团队」扩大为「PUBLIC」',
            change_type=TicketChangeType.SCOPE_CHANGE,
        )
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '同意'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200, resp.content
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.visibility_level == VisibilityLevel.PUBLIC


# ============================================================================
# reject_access_request 缺失分支
# ============================================================================
class TestRejectAccessRequestCoverage(KnowledgeViewsExtraBase):
    """reject_access_request 缺失分支覆盖"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    def _create_ticket(self, applicant, reason):
        ticket = TicketList.objects.create(
            ticket_no=f'QX-REJ-{uuid_lib.uuid4().hex[:12].upper()}',
            title='文档权限·工单',
            biz_type='permission',
            applicant=applicant,
            status=TicketStatus.PENDING,
            risk_level='normal',
            approval_chain=[
                {'step': 0, 'approver_id': None, 'status': 'pending',
                 'comment': '', 'approved_at': None},
            ],
            current_step=0,
        )
        TicketPermissionDetail.objects.create(
            ticket=ticket,
            target_user=applicant,
            change_type=TicketChangeType.GRANT,
            role=None,
            scope_type=ScopeType.NONE,
            scope_id=None,
            reason=reason,
        )
        return ticket

    @pytest.mark.integration
    def test_reject_ticket_missing_404(self):
        resp = self.client.post(
            '/api/v1/knowledge/documents/reject_access_request/',
            data=json.dumps({'request_id': 999999, 'comment': '驳回'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_reject_node_ticket_non_owner_403(self):
        """节点可见范围工单：非节点所有者且非管理员 → 403"""
        self.category_node.owner_user = self.super_admin
        self.category_node.save(update_fields=['owner_user'])
        ticket = self._create_ticket(
            self.super_admin,
            f'[node:{self.category_node.id}:visibility_change] 调整可见范围')
        resp = self.client.post(
            '/api/v1/knowledge/documents/reject_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '驳回'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_no_target_normal_user_403(self):
        """无目标编码的工单仅管理员可驳回 → 普通用户 403"""
        ticket = self._create_ticket(self.normal_user, '无前缀工单')
        resp = self.client.post(
            '/api/v1/knowledge/documents/reject_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '驳回'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403


# ============================================================================
# DocumentUploadView 非法可见性 / 孤儿文件清理失败
# ============================================================================
class TestDocumentUploadCoverage(KnowledgeViewsExtraBase):
    """上传接口补充分支（mock libmagic / _save_file / 存储清理）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    def _upload(self, user, **overrides):
        data = {
            'file': SimpleUploadedFile(
                '覆盖.txt', b'hello world', content_type='text/plain'),
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
    @patch('apps.knowledge.views._validate_visibility_level',
           return_value=(False, '当前角色无权限设置该可见性'))
    def test_upload_invalid_visibility_403(self, mock_validate, mock_magic):
        """可见性层级校验失败 → 403"""
        mock_magic.from_buffer.return_value = 'text/plain'
        resp = self._upload(self.super_admin)
        assert resp.status_code == 403
        assert '无权限设置该可见性' in resp.json()['detail']

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    @patch('apps.knowledge.views.get_document_storage')
    @patch.object(DocumentUploadView, '_save_file',
                  return_value='/tmp/orphan_cov.txt')
    def test_upload_cleanup_failure_still_500(
            self, mock_save, mock_storage, mock_magic):
        """文档落库失败 + 孤儿文件清理也失败 → 仍返回 500 不抛异常"""
        mock_magic.from_buffer.return_value = 'text/plain'
        mock_storage.return_value.delete.side_effect = \
            RuntimeError('delete failed')
        with patch.object(Document.objects, 'create',
                          side_effect=RuntimeError('db boom')):
            resp = self._upload(self.super_admin)
        assert resp.status_code == 500
        mock_storage.return_value.delete.assert_called_once_with(
            '/tmp/orphan_cov.txt')


# ============================================================================
# QueueDepthView
# ============================================================================
class TestQueueDepthView(KnowledgeViewsExtraBase):
    """queues/depth 队列深度快照正常 / 异常兜底"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    @patch('apps.analytics.services.realtime_service.get_queue_depth_snapshot',
           return_value={'parse': {'length': 3, 'active': 1}})
    def test_depth_snapshot_ok(self, mock_snap):
        resp = self.client.get(
            '/api/v1/knowledge/queues/depth/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['queues']['parse']['length'] == 3

    @pytest.mark.integration
    @patch('apps.analytics.services.realtime_service.get_queue_depth_snapshot',
           side_effect=RuntimeError('redis down'))
    def test_depth_snapshot_exception_returns_empty(self, mock_snap):
        resp = self.client.get(
            '/api/v1/knowledge/queues/depth/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['queues'] == {}


# ============================================================================
# 文档审核：组长审批 / 管理员驳回 / 列表范围过滤
# ============================================================================
class TestDocAuditApproveByTeamLeader(KnowledgeViewsExtraBase):
    """DocAuditApproveView 团队组长审核分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.pending_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='待审文档', file_name='audit_leader.txt',
            audit_status='pending_team')

    @pytest.mark.integration
    def test_approve_pending_team_by_team_leader(self):
        """组长审核本团队待审文档 → 进入复核阶段"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-approve/',
            data=json.dumps({'comment': '组长审核通过'}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 200
        assert resp.json()['audit_status'] == 'pending_compliance'


class TestDocAuditRejectByAdmin(KnowledgeViewsExtraBase):
    """DocAuditRejectView 管理员驳回复核阶段文档"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.compliance_doc = _create_document(
            self.category_node, self.normal_user, dept_id=self.dept.id,
            title='复核文档', file_name='audit_admin.txt',
            audit_status='pending_compliance')

    @pytest.mark.integration
    def test_reject_pending_compliance_by_super_admin(self):
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.compliance_doc.id}/audit-reject/',
            data=json.dumps({'comment': '管理员驳回'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['audit_status'] == 'rejected'


class TestDocAuditRejectedScope(KnowledgeViewsExtraBase):
    """DocAuditRejectedView 越界过滤（他组文档不进入组长视野）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.team2 = Team.objects.create(
            name='前端组', code='rd-frontend', department=self.dept)
        self.rejected_other = _create_document(
            self.category_node, self.normal_user, team_id=self.team2.id,
            dept_id=self.dept.id, title='他组驳回文档', file_name='other_rej.txt',
            audit_status='rejected')
        self.rejected_own = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='本组驳回文档', file_name='own_rej.txt',
            audit_status='rejected')

    @pytest.mark.integration
    def test_team_leader_scope_filters_other_team(self):
        resp = self.client.get(
            '/api/v1/knowledge/documents/audit-rejected/',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 200
        ids = {r['id'] for r in resp.json()['rows']}
        assert self.rejected_own.id in ids
        assert self.rejected_other.id not in ids


class TestDocAuditRecordScope(KnowledgeViewsExtraBase):
    """DocAuditRecordView 已删除文档 / 越界文档过滤"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        # 已删除文档的审核记录
        self.deleted_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='已删文档', file_name='del_doc.txt')
        self.deleted_doc.is_deleted = True
        self.deleted_doc.save(update_fields=['is_deleted'])
        DocOperationLog.objects.create(
            action='doc_audit_approve', operator=self.super_admin,
            operator_name='admin', document=self.deleted_doc,
            detail={'to_status': 'passed', 'comment': ''})
        # 他组文档的审核记录
        self.team2 = Team.objects.create(
            name='前端组', code='rd-frontend2', department=self.dept)
        self.other_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team2.id,
            dept_id=self.dept.id, title='他组记录文档', file_name='other_rec.txt',
            audit_status='passed')
        DocOperationLog.objects.create(
            action='doc_audit_approve', operator=self.super_admin,
            operator_name='admin', document=self.other_doc,
            detail={'to_status': 'passed', 'comment': ''})
        # 本组文档的审核记录
        self.own_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='本组记录文档', file_name='own_rec.txt',
            audit_status='passed')
        DocOperationLog.objects.create(
            action='doc_audit_approve', operator=self.super_admin,
            operator_name='admin', document=self.own_doc,
            detail={'to_status': 'passed', 'comment': ''})

    @pytest.mark.integration
    def test_super_admin_excludes_deleted_doc_logs(self):
        resp = self.client.get(
            '/api/v1/knowledge/documents/audit-records/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        doc_ids = {r['document_id'] for r in resp.json()['rows']}
        assert self.deleted_doc.id not in doc_ids
        assert self.own_doc.id in doc_ids

    @pytest.mark.integration
    def test_team_leader_scope_excludes_other_team(self):
        resp = self.client.get(
            '/api/v1/knowledge/documents/audit-records/',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 200
        doc_ids = {r['document_id'] for r in resp.json()['rows']}
        assert self.own_doc.id in doc_ids
        assert self.other_doc.id not in doc_ids


# ============================================================================
# 节点创建领地兜底 / perform_update 防御性校验
# ============================================================================
class TestDeptManagerNodeCreateOutOfScope(KnowledgeViewsExtraBase):
    """部门经理在非本部门节点下创建文件夹 → 403 兜底分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    def _make_dept_manager(self):
        mgr = _create_test_user('mgr_scope')
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
    def test_create_folder_under_root_denied(self):
        """根节点不在部门节点子树内 → 领地检查兜底拒绝"""
        mgr = self._make_dept_manager()
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({
                'name': '越界文件夹',
                'parent': self.root_node.id,
                'node_type': 'folder',
            }),
            content_type='application/json',
            **_auth_headers(mgr))
        assert resp.status_code == 403


class TestNodeUpdateInvalidVisibilityDefensive(KnowledgeViewsExtraBase):
    """perform_update 防御性校验：可见范围非法直接抛 ValidationError"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_perform_update_invalid_visibility_level(self):
        from rest_framework.exceptions import ValidationError
        from rest_framework.request import Request
        vs = KnowledgeNodeViewSet()
        vs.action = 'update'
        vs.kwargs = {'pk': self.category_node.id}
        req = Request(APIRequestFactory().get(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/'))
        req.user = self.super_admin
        vs.request = req
        serializer = MagicMock()
        serializer.validated_data = {'visibility_level': 'BOGUS_LEVEL'}
        with pytest.raises(ValidationError):
            vs.perform_update(serializer)
