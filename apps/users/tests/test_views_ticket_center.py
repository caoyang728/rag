"""
apps.users.views 统一工单中心 API 测试 —— 四类工单（权限/配置/定时/模型）一页展示 + 统一审批操作

覆盖：
- TicketCenterView：view=pending/processed/mine/all + type/status/search 过滤 + 可见范围按角色过滤
- TicketCenterApproveView：permission 委托权限域审批；config 走系统域审批（审核+复核+生效）
- TicketCenterRejectView：统一驳回（系统域驳回必填理由）
- TicketCenterWithdrawView：创建人撤回（仅本人 + PENDING）

数据源约定：
- 权限工单走 ticket_service.create_ticket（审批链降级为超管单审，便于管理员直接审批）
- 配置工单走 system.views._create_system_ticket（SYSTEM_AUDITOR 单节点链）
"""
import json

import pytest

from apps.system.models import SystemConfig
from apps.system.views import _create_system_ticket
from apps.users.models import (
    TicketList, TicketFlowLog, TicketStatus, TicketBizType,
    TicketPermissionDetail, TicketChangeType, ScopeType, RoleType, DataScope,
)
from apps.users.ticket_service import _gen_ticket_no
from apps.users.tests.test_views_base import (
    _auth_headers, _create_user, _get_or_create_role, _grant_permission, _grant_global_role,
    UsersAPIExtraBase,
)

# 统一工单中心 API（users urls 挂在 /api/v1/auth/ 前缀下）
TICKET_API = '/api/v1/auth/tickets/'


class TicketCenterTestBase(UsersAPIExtraBase):
    """工单中心测试公共基座 —— 提供配置/权限/组织三类工单的创建辅助"""

    def _make_user_with_perm(self, username, perm_key, role_key, role_name):
        """创建带指定功能权限的用户（绑定角色 + 权限点），归属 dept_a/team_a"""
        user = _create_user(username, department=self.dept_a, team=self.team_a)
        role = _get_or_create_role(role_key, name=role_name, is_builtin=True,
                                   role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL)
        _grant_permission(role, perm_key)
        _grant_global_role(user, role_key)
        return user

    def _make_config_ticket(self, applicant=None, key='search.top_k', risk_level='normal',
                            new_value='10'):
        """创建配置变更工单（系统域统一建单入口，PENDING）

        普通项走 SYSTEM_AUDITOR 单节点链，管理员可直接审批；
        key 对应的 SystemConfig 记录预先补齐，供审批通过后写入新值。
        """
        applicant = applicant or self.normal_user
        SystemConfig.objects.get_or_create(
            key=key,
            defaults={'value': '5', 'label': '测试配置', 'category': 'retrieval',
                      'risk_level': risk_level, 'value_type': 'int'},
        )
        return _create_system_ticket(
            applicant=applicant,
            biz_type=TicketBizType.CONFIG,
            title=f'修改配置 {key}',
            risk_level=risk_level,
            detail={'config_label': '测试配置', 'key': key,
                    'old_value': '5', 'new_value': new_value, 'reason': '测试变更'},
            operation='modify',
            config_key=key,
        )

    def _make_permission_ticket(self, applicant=None):
        """创建跨部门 viewer 权限工单（审批链降级为超管单审，PENDING）"""
        return self._create_pending_ticket(applicant=applicant or self.normal_user)


# ============================================================================
# TicketCenterView —— 统一工单列表（视角/类型/状态/搜索过滤 + 权限校验）
# ============================================================================
class TestTicketCenterListView(TicketCenterTestBase):
    """工单中心列表：视角过滤 + 筛选参数 + 页面权限"""

    @pytest.mark.integration
    def test_pending_view_shows_approvable_tickets(self):
        """待我审批：管理员可见当前节点可审批的权限+配置工单"""
        self._make_permission_ticket()          # SUPER_ADMIN 单审链
        self._make_config_ticket()              # SYSTEM_AUDITOR 单节点链
        resp = self.client.get(TICKET_API + '?view=pending', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['count'] == 2
        assert {r['biz_type'] for r in data['rows']} == {'permission', 'config'}

    @pytest.mark.integration
    def test_pending_view_normal_user_returns_empty(self):
        """普通用户（无管理角色）访问待我审批 → 200，无可审批工单（count=0）"""
        self._make_permission_ticket()
        resp = self.client.get(TICKET_API + '?view=pending', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 0

    @pytest.mark.integration
    def test_all_view_open_to_all_filtered_by_visible_scope(self):
        """全部工单视角对所有登录用户开放；可见范围按角色过滤（个人仅见自己的工单）"""
        self._make_config_ticket()                            # normal_user 发起
        self._make_config_ticket(applicant=self.team_leader)  # 他人发起
        resp = self.client.get(TICKET_API + '?view=all', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 1                     # 个人仅见自己的工单
        resp = self.client.get(TICKET_API + '?view=all', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 2                     # 超管全量

    @pytest.mark.integration
    def test_type_filter(self):
        """type 过滤：只返回对应类型工单"""
        self._make_config_ticket()
        self._make_permission_ticket()
        resp = self.client.get(TICKET_API + '?view=all&type=config', **self.admin_headers)
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['biz_type'] == 'config'
        resp = self.client.get(TICKET_API + '?view=all&type=permission', **self.admin_headers)
        assert all(r['biz_type'] == 'permission' for r in resp.json()['rows'])

    @pytest.mark.integration
    def test_invalid_type_400(self):
        """非法 type 返回 400 并提示合法取值"""
        resp = self.client.get(TICKET_API + '?view=all&type=bogus', **self.admin_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_invalid_status_400(self):
        """非法 status 返回 400"""
        resp = self.client.get(TICKET_API + '?view=all&status=NOPE', **self.admin_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_search_by_ticket_no(self):
        """search 支持工单号精确匹配"""
        t = self._make_config_ticket()
        resp = self.client.get(TICKET_API + '?view=all&search=' + t.ticket_no,
                               **self.admin_headers)
        assert resp.json()['count'] == 1
        assert resp.json()['rows'][0]['id'] == t.id

    @pytest.mark.integration
    def test_search_by_title_fuzzy(self):
        """search 任务名模糊匹配"""
        self._make_config_ticket()
        resp = self.client.get(TICKET_API + '?view=all&search=修改配置', **self.admin_headers)
        assert resp.json()['count'] >= 1

    @pytest.mark.integration
    def test_mine_view_only_own_tickets(self):
        """我的工单：仅返回本人发起的工单"""
        self._make_config_ticket()                            # normal_user 发起
        self._make_config_ticket(applicant=self.team_leader)  # 他人发起
        resp = self.client.get(TICKET_API + '?view=mine', **self.normal_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['applicant_id'] == self.normal_user.id

    @pytest.mark.integration
    def test_processed_view_after_approve(self):
        """我已审批：审批通过后的工单进入已处理列表"""
        t = self._make_config_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/approve/',
                                data=json.dumps({'comment': '同意'}),
                                content_type='application/json', **self.admin_headers)
        assert resp.status_code == 200
        resp = self.client.get(TICKET_API + '?view=processed', **self.admin_headers)
        rows = resp.json()['rows']
        assert any(r['id'] == t.id for r in rows)

    @pytest.mark.integration
    def test_pending_permission_row_has_detail_fields(self):
        """权限工单行包含详情字段（目标用户/角色），供前端按类型渲染"""
        self._make_permission_ticket()
        resp = self.client.get(TICKET_API + '?view=pending', **self.admin_headers)
        row = next(r for r in resp.json()['rows'] if r['biz_type'] == 'permission')
        assert row['target_user_id'] == self.normal_user.id
        assert row['role_name'] == _get_or_create_role('viewer').name
        assert row['total_steps'] == len(row['approval_chain'])


# ============================================================================
# 展示权限矩阵 —— 所有登录用户可访问，可见范围按角色过滤（_ticket_visible_scope）
# ============================================================================
class TestTicketCenterVisibleScope(TicketCenterTestBase):
    """工单中心展示矩阵：超管全量 / user_admin 角色工单 / maintain 配置工单 /
    kb_admin 文档工单 / 部门经理管辖部门 / 组长管辖团队 / 个人仅自己"""

    def _make_doc_ticket(self, applicant=None, target_user=None):
        """创建文档/节点授权工单（permission 域 + role=None，等价 knowledge._create_doc_ticket）"""
        applicant = applicant or self.normal_user
        target_user = target_user or self.normal_user
        ticket = TicketList.objects.create(
            ticket_no=_gen_ticket_no(TicketBizType.PERMISSION),
            title='文档权限·GRANT',
            biz_type=TicketBizType.PERMISSION,
            status=TicketStatus.PENDING,
            risk_level='normal',
            applicant=applicant,
            approval_chain=[{
                'step': 0, 'approver_role': 'SUPER_ADMIN', 'approver_id': None,
                'status': 'pending', 'comment': '', 'approved_at': None,
            }],
            current_step=0,
        )
        TicketPermissionDetail.objects.create(
            ticket=ticket, target_user=target_user, change_type=TicketChangeType.GRANT,
            role=None, scope_type=ScopeType.NONE, scope_id=None,
            reason='[doc:1:GRANT] 测试文档授权',
        )
        return ticket

    @pytest.mark.integration
    def test_super_admin_sees_all_types(self):
        """超管 view=all → 权限（含文档授权）+ 配置工单全量可见"""
        self._make_permission_ticket()
        self._make_doc_ticket()
        self._make_config_ticket()
        resp = self.client.get(TICKET_API + '?view=all', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 3

    @pytest.mark.integration
    def test_user_admin_sees_role_permission_tickets(self):
        """user_admin（user.manage_all）→ 仅见角色授权类权限工单（详情 role 非空）"""
        user_admin = self._make_user_with_perm('useradmin', 'user.manage_all',
                                               'user_admin', '用户管理员')
        self._make_permission_ticket()          # 角色授权工单（role 非空）
        self._make_doc_ticket()                 # 文档授权工单（role 为空）
        self._make_config_ticket()              # 配置工单
        resp = self.client.get(TICKET_API + '?view=all', **_auth_headers(user_admin))
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['biz_type'] == 'permission'
        assert rows[0]['role_name'] == _get_or_create_role('viewer').name

    @pytest.mark.integration
    def test_maintain_admin_sees_config_tickets(self):
        """maintain_admin（system.config.write）→ 仅见配置类工单"""
        maintain = self._make_user_with_perm('maintain', 'system.config.write',
                                             'maintain_admin', '配置管理员')
        self._make_config_ticket()
        self._make_permission_ticket()
        resp = self.client.get(TICKET_API + '?view=all', **_auth_headers(maintain))
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['biz_type'] == 'config'

    @pytest.mark.integration
    def test_kb_admin_sees_doc_permission_tickets(self):
        """kb_admin（kb.manage_all）→ 仅见文档/节点授权工单（permission 且 role 为空）"""
        kb_admin = self._make_user_with_perm('kbadmin', 'kb.manage_all',
                                             'kb_admin', '知识库管理员')
        self._make_doc_ticket()
        self._make_permission_ticket()          # 角色授权工单不可见
        self._make_config_ticket()              # 配置工单不可见
        resp = self.client.get(TICKET_API + '?view=all', **_auth_headers(kb_admin))
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['biz_type'] == 'permission'
        assert rows[0]['target_user_id'] == self.normal_user.id

    @pytest.mark.integration
    def test_dept_manager_sees_own_dept_tickets(self):
        """部门经理（Department.leader_id）→ 申请人属于管辖部门（含多部门）的工单可见"""
        self.dept_a.leader = self.dept_mgr
        self.dept_a.save(update_fields=['leader'])
        self._make_config_ticket()              # normal_user 归属 dept_a → 可见
        other = _create_user('other_dept', department=self.dept_b, team=self._team_c())
        self._make_config_ticket(applicant=other)  # dept_b 工单 → 不可见
        resp = self.client.get(TICKET_API + '?view=all', **self.dept_mgr_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['applicant_id'] == self.normal_user.id

    @pytest.mark.integration
    def test_team_leader_sees_own_team_tickets(self):
        """组长（Team.leader_id）→ 申请人属于管辖团队（支持跨部门团队）的工单可见"""
        self._make_config_ticket()              # normal_user 归属 team_a → 可见
        other = _create_user('other_team', department=self.dept_b, team=self._team_c())
        self._make_config_ticket(applicant=other)  # 其他团队工单 → 不可见
        resp = self.client.get(TICKET_API + '?view=all', **self.leader_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['applicant_id'] == self.normal_user.id


# ============================================================================
# TicketCenterApproveView —— 统一审批通过（跨类型路由）
# ============================================================================
class TestTicketCenterApproveView(TicketCenterTestBase):
    """统一审批通过：config 走系统域生效；permission 委托权限域执行授权"""

    @pytest.mark.integration
    def test_approve_config_ticket_applies_config(self):
        """配置工单审批通过 → EXECUTED + 配置值写入 + EXECUTE 流转日志"""
        t = self._make_config_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/approve/',
                                data=json.dumps({'comment': '同意'}),
                                content_type='application/json', **self.admin_headers)
        assert resp.status_code == 200, resp.content
        t.refresh_from_db()
        assert t.status == TicketStatus.EXECUTED
        assert SystemConfig.objects.get(key=t.config_key).value == '10'
        assert TicketFlowLog.objects.filter(ticket=t, action='EXECUTE').exists()

    @pytest.mark.integration
    def test_approve_permission_ticket_executes_grant(self):
        """权限工单审批通过 → EXECUTED（委托权限域审批）"""
        t = self._make_permission_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/approve/',
                                data=json.dumps({'comment': '同意'}),
                                content_type='application/json', **self.admin_headers)
        assert resp.status_code == 200, resp.content
        t.refresh_from_db()
        assert t.status == TicketStatus.EXECUTED

    @pytest.mark.integration
    def test_approve_self_created_forbidden(self):
        """防自审：创建人审批自己提交的工单 → 403"""
        t = self._make_config_ticket(applicant=self.super_admin)
        resp = self.client.post(TICKET_API + f'{t.id}/approve/',
                                data=json.dumps({'comment': '同意'}),
                                content_type='application/json', **self.admin_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_not_found_404(self):
        """工单不存在 → 404"""
        resp = self.client.post(TICKET_API + '999999/approve/',
                                data=json.dumps({}), content_type='application/json',
                                **self.admin_headers)
        assert resp.status_code == 404


# ============================================================================
# TicketCenterRejectView —— 统一驳回
# ============================================================================
class TestTicketCenterRejectView(TicketCenterTestBase):
    """统一驳回：系统域驳回必填理由，工单终态 REJECTED"""

    @pytest.mark.integration
    def test_reject_config_ticket(self):
        """配置工单驳回 → REJECTED 终态"""
        t = self._make_config_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/reject/',
                                data=json.dumps({'comment': '不同意'}),
                                content_type='application/json', **self.admin_headers)
        assert resp.status_code == 200, resp.content
        t.refresh_from_db()
        assert t.status == TicketStatus.REJECTED

    @pytest.mark.integration
    def test_reject_without_comment_400(self):
        """系统域驳回必须填写理由 → 400"""
        t = self._make_config_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/reject/',
                                data=json.dumps({'comment': ''}),
                                content_type='application/json', **self.admin_headers)
        assert resp.status_code == 400
        t.refresh_from_db()
        assert t.status == TicketStatus.PENDING


# ============================================================================
# TicketCenterWithdrawView —— 创建人撤回
# ============================================================================
class TestTicketCenterWithdrawView(TicketCenterTestBase):
    """统一撤回：仅创建人本人 + PENDING 状态可撤回"""

    @pytest.mark.integration
    def test_withdraw_own_pending(self):
        """创建人撤回 PENDING 工单 → CANCELLED"""
        t = self._make_config_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/withdraw/',
                                data=json.dumps({}), content_type='application/json',
                                **self.normal_headers)
        assert resp.status_code == 200
        t.refresh_from_db()
        assert t.status == TicketStatus.CANCELLED

    @pytest.mark.integration
    def test_withdraw_others_forbidden(self):
        """非创建人撤回 → 403"""
        t = self._make_config_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/withdraw/',
                                data=json.dumps({}), content_type='application/json',
                                **self.admin_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_withdraw_non_pending_409(self):
        """已执行工单不可撤回 → 409（状态冲突，system 域统一语义）"""
        t = self._make_config_ticket()
        self.client.post(TICKET_API + f'{t.id}/approve/',
                         data=json.dumps({'comment': '同意'}),
                         content_type='application/json', **self.admin_headers)
        resp = self.client.post(TICKET_API + f'{t.id}/withdraw/',
                                data=json.dumps({}), content_type='application/json',
                                **self.normal_headers)
        assert resp.status_code == 409
        t.refresh_from_db()
        assert t.status == TicketStatus.EXECUTED


# ============================================================================
# org 组织变更工单 —— 工单中心序列化 / 可见范围 / 统一审批路由
# ============================================================================
class TestOrgTicketCenter(TicketCenterTestBase):
    """org 工单在工单中心：行序列化字段 / user_admin 可见范围 / 审批生效"""

    def _make_org_ticket(self, applicant=None, name='工单中心新部门', operation='add'):
        """创建部门新增 org 工单（normal 单审链 [USER_ADMIN]）"""
        from apps.users.ticket_service import create_org_ticket
        return create_org_ticket(
            actor=applicant or self.normal_user,
            org_type='dept',
            operation=operation,
            target_data={'name': name, 'code': 'gzzx'},
            reason=f'测试{operation}: {name}',
            new_data={'name': name, 'code': 'gzzx'} if operation != 'delete' else None,
            old_data={'name': name, 'code': 'gzzx'} if operation in ('edit', 'delete') else None,
        )

    @pytest.mark.integration
    def test_org_row_has_detail_fields(self):
        """org 工单行包含组织类型/操作/目标名/变更前后快照，供前端按类型渲染"""
        t = self._make_org_ticket()
        resp = self.client.get(TICKET_API + '?view=all', **self.admin_headers)
        assert resp.status_code == 200
        row = next(r for r in resp.json()['rows'] if r['biz_type'] == 'org')
        assert row['ticket_no'] == t.ticket_no
        assert row['org_type'] == 'dept'
        assert row['org_type_display'] == '部门'
        assert row['operation'] == 'add'
        assert row['operation_display'] == '部门新增'
        assert row['org_name'] == '工单中心新部门'
        assert row['old_data'] is None
        assert row['new_data']['name'] == '工单中心新部门'
        assert '组织变更' in row['title']

    @pytest.mark.integration
    def test_org_edit_row_shows_operation_and_old_new(self):
        """org 编辑工单行：操作显示"部门编辑"，old/new 双快照齐全"""
        from apps.users.models import Department
        d = Department.objects.create(name='老名字', code='old')
        t = self._make_org_ticket(operation='edit', name='老名字')
        od = t.org_detail
        od.target_data = {'id': d.id, 'name': '老名字'}
        od.old_data = {'name': '老名字', 'code': 'old'}
        od.new_data = {'name': '新名字', 'code': 'old'}
        od.save()
        resp = self.client.get(TICKET_API + '?view=all', **self.admin_headers)
        row = next(r for r in resp.json()['rows'] if r['biz_type'] == 'org')
        assert row['operation_display'] == '部门编辑'
        assert row['old_data']['name'] == '老名字'
        assert row['new_data']['name'] == '新名字'

    @pytest.mark.integration
    def test_user_admin_sees_org_tickets_in_all_view(self):
        """user_admin（user.manage_all）→ view=all 可见 org 工单（与角色工单同视角）"""
        user_admin = self._make_user_with_perm('useradmin_org', 'user.manage_all',
                                               'user_admin', '用户管理员')
        self._make_org_ticket()          # org 工单
        self._make_config_ticket()       # 配置工单（user_admin 不可见）
        resp = self.client.get(TICKET_API + '?view=all', **_auth_headers(user_admin))
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['biz_type'] == 'org'

    @pytest.mark.integration
    def test_approve_org_ticket_via_center_executes(self):
        """工单中心统一审批入口审批 org 工单 → EXECUTED + 部门落库"""
        t = self._make_org_ticket()
        # org 审批链首节点为 USER_ADMIN：构造 user_admin 审批人走中心入口
        from apps.users.tests.test_views_base import _grant_global_role
        ua = _create_user('ua_center')
        _grant_global_role(ua, 'user_admin')
        resp = self.client.post(TICKET_API + f'{t.id}/approve/',
                                data=json.dumps({'comment': '同意'}),
                                content_type='application/json',
                                **_auth_headers(ua))
        assert resp.status_code == 200, resp.content
        t.refresh_from_db()
        assert t.status == TicketStatus.EXECUTED
        from apps.users.models import Department
        assert Department.objects.filter(name='工单中心新部门', is_deleted=False).exists()

    @pytest.mark.integration
    def test_reject_org_ticket_via_center(self):
        """工单中心统一驳回入口驳回 org 工单 → REJECTED 终态"""
        t = self._make_org_ticket()
        from apps.users.tests.test_views_base import _grant_global_role
        ua = _create_user('ua_reject')
        _grant_global_role(ua, 'user_admin')
        resp = self.client.post(TICKET_API + f'{t.id}/reject/',
                                data=json.dumps({'comment': '不同意'}),
                                content_type='application/json',
                                **_auth_headers(ua))
        assert resp.status_code == 200, resp.content
        t.refresh_from_db()
        assert t.status == TicketStatus.REJECTED

    @pytest.mark.integration
    def test_withdraw_org_ticket_by_applicant(self):
        """创建人撤回 org 工单 → CANCELLED"""
        t = self._make_org_ticket()
        resp = self.client.post(TICKET_API + f'{t.id}/withdraw/',
                                data=json.dumps({}), content_type='application/json',
                                **self.normal_headers)
        assert resp.status_code == 200
        t.refresh_from_db()
        assert t.status == TicketStatus.CANCELLED
