"""权限申请业务逻辑：任命权限判定、资源所有者判定、可申请角色、审批链预览"""
from loguru import logger

from apps.users.models import Role, ScopeType, Team, TicketChangeType
from apps.users.ticket_service import ApproverRole
from apps.users.utils import _resolve_scope_name
# 管理岗/高权角色:仅允许上级(部门经理/用户管理员/超管)发起任命工单
SELF_APPLY_FORBIDDEN_KEYS = (
    'team_leader', 'dept_manager',
    'kb_admin', 'compliance_admin', 'user_admin', 'super_admin',
)
# 全局高权管理岗:仅用户管理员/超管可发起任命
GLOBAL_MANAGEMENT_KEYS = ('dept_manager', 'kb_admin', 'compliance_admin', 'user_admin')


def can_nominate(user, role_key, scope_type, scope_id):
    """上级发起任命的权限判定(管理岗专用)

    判定规则(遵循权限模型:超管快路径,其余走 permission_key 判定):
    - 超管:可发起任何管理岗任命
    - team_leader:目标团队所属部门的部门经理(dept_manager 授权)或用户管理员可发起
    - dept_manager/kb_admin/compliance_admin/user_admin:仅用户管理员/超管可发起
    """
    if user.is_super_admin:
        return True
    if role_key in GLOBAL_MANAGEMENT_KEYS:
        return user.is_user_admin
    if role_key == 'team_leader':
        if user.is_user_admin:
            return True
        # 目标团队所属部门解析:scope_type=TEAM → team.department_id;DEPT → scope_id 即部门
        dept_id = None
        if scope_type == ScopeType.TEAM and scope_id:
            dept_id = Team.objects.filter(
                id=scope_id, is_deleted=False,
            ).values_list('department_id', flat=True).first()
        elif scope_type == ScopeType.DEPT and scope_id:
            dept_id = scope_id
        if not dept_id:
            return False
        from apps.users.ticket_service import _get_dept_leader_id
        # 仅本部门经理可任命本部门团队组长(避免跨部门越权)
        return _get_dept_leader_id(dept_id) == user.id
    return False


def is_resource_owner(user, scope_type, scope_id):
    """协作角色(viewer/contributor)提单人身份校验 —— 资源所有者原则

    定稿规则:协作角色授权由"资源所有者"提单,即权限所在的组织管理者:
    - scope_type=TEAM:必须是目标团队(scope_id)的组长(team.leader_id)
    - scope_type=DEPT:必须是目标部门(scope_id)的部门经理(dept_manager 授权)
    - 超管可兜底发起

    提单人即工单发起人,不能同时是审批人(回避原则由审批链自动保证)。
    """
    if user.is_super_admin:
        return True
    if scope_type == ScopeType.TEAM and scope_id:
        # 资源团队组长:team.leader_id 直接匹配
        return Team.objects.filter(
            id=scope_id, is_deleted=False, leader_id=user.id,
        ).exists()
    if scope_type == ScopeType.DEPT and scope_id:
        from apps.users.ticket_service import _get_dept_leader_id
        # 资源部门经理:以 dept_manager 授权为准(兼容 leader_id 双来源)
        return _get_dept_leader_id(scope_id) == user.id
    return False


# 角色分类(前端按分类分组展示)
ROLE_CATEGORY_MAP = {
    'viewer': {'category': 'team', 'category_label': '团队角色', 'rank': 1},
    'contributor': {'category': 'team', 'category_label': '团队角色', 'rank': 2},
    'team_leader': {'category': 'team', 'category_label': '团队角色', 'rank': 3},
    'dept_manager': {'category': 'dept', 'category_label': '部门角色', 'rank': 10},
    'kb_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 20},
    'compliance_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 21},
    'user_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 22},
    'super_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 99},
}

# 审批链概要(前端展示用,不包含具体审批人)
APPROVAL_CHAIN_SUMMARY = {
    'viewer': {'steps': ['组长/部门经理提单'], 'desc': '本团队/本部门其他团队组长提单自动生效;部门级本部门部门经理提单自动生效;跨部门团队由资源部门经理批准,部门级跨部门由文档管理员审核'},
    'contributor': {'steps': ['组长/部门经理提单'], 'desc': '本团队/本部门其他团队组长提单自动生效;部门级本部门部门经理提单自动生效;跨部门团队由资源部门经理批准,部门级跨部门由文档管理员审核'},
    'team_leader': {'steps': ['人员管理'], 'desc': '部门经理发起,人员管理审批(超管兜底)'},
    'dept_manager': {'steps': ['用户管理员', '超级管理员'], 'desc': '人员管理发起,另一人员管理审批,超管复核'},
    'kb_admin': {'steps': ['用户管理员', '超级管理员'], 'desc': '人员管理发起,另一人员管理审批,超管复核'},
    'compliance_admin': {'steps': ['用户管理员', '超级管理员'], 'desc': '人员管理发起,另一人员管理审批,超管复核'},
    'user_admin': {'steps': ['超级管理员', '超级管理员'], 'desc': '人员管理发起,双超管审批+复核(双人独立)'},
    'super_admin': {'steps': ['超级管理员', '超级管理员'], 'desc': '人员管理发起,双超管审批+复核(双人独立)'},
}

# 自助申请角色(协作角色) — 管理岗一律走上级发起任命,不开放自助申请
SELF_APPLY_ROLE_KEYS = ('viewer', 'contributor')
# 管理岗/高权角色 — 仅允许上级(部门经理/用户管理员/超管)发起任命工单
MANAGEMENT_ROLE_KEYS = (
    'team_leader', 'dept_manager',
    'kb_admin', 'compliance_admin', 'user_admin',
)


def get_assignable_roles(purpose='self', scope_filter=''):
    """返回当前用户可申请的角色清单(按类别分组,含审批链提示)

    purpose=self 返回自助申请清单(协作角色);purpose=management 返回管理岗清单(管理端任命用)。
    scope_filter 可选:TEAM/DEPT/NONE,用于筛选指定范围的角色。
    返回 rows 列表(已按 rank 排序)。
    """
    if purpose == 'management':
        # 管理岗清单:供管理端发起任命工单使用(super_admin 不在此列,超管任命走用户编辑接口)
        roles = Role.objects.filter(
            is_deleted=False, role_key__in=MANAGEMENT_ROLE_KEYS,
        ).order_by('id')
    else:
        # 自助申请清单:仅协作角色(viewer/contributor)
        roles = Role.objects.filter(
            is_deleted=False, role_key__in=SELF_APPLY_ROLE_KEYS,
        ).order_by('id')

    rows = []
    for r in roles:
        meta = ROLE_CATEGORY_MAP.get(r.role_key)
        if not meta:
            # 未在分类表中登记的角色(如自定义角色)不返回
            continue
        # scope_type 筛选
        if scope_filter:
            category_scope_map = {
                'team': ScopeType.TEAM,
                'dept': ScopeType.DEPT,
                'global': ScopeType.NONE,
            }
            if category_scope_map.get(meta['category']) != scope_filter:
                continue

        chain_info = APPROVAL_CHAIN_SUMMARY.get(r.role_key, {'steps': [], 'desc': ''})
        # 需要绑定 scope 的角色类型
        need_scope = r.role_type in ('TEAM_SCOPE', 'DEPT_SCOPE')
        # viewer/contributor 团队级与部门级均可授权(定稿开放部门级);
        # 用 'TEAM|DEPT' 表达双范围,前端按 supported_scopes 渲染选择器。
        if r.role_key in ('viewer', 'contributor'):
            scope_type_required = 'TEAM|DEPT'
            supported_scopes = [ScopeType.TEAM, ScopeType.DEPT]
        elif r.role_key in ('team_leader',):
            scope_type_required = ScopeType.TEAM
            supported_scopes = [ScopeType.TEAM]
        elif r.role_key == 'dept_manager':
            scope_type_required = ScopeType.DEPT
            supported_scopes = [ScopeType.DEPT]
        else:
            scope_type_required = ScopeType.NONE
            supported_scopes = [ScopeType.NONE]

        rows.append({
            'id': r.id,
            'role_key': r.role_key,
            'name': r.name,
            'description': r.description or '',
            'role_type': r.role_type,
            'data_scope': r.data_scope,
            'category': meta['category'],
            'category_label': meta['category_label'],
            'rank': meta['rank'],
            'need_scope': need_scope,
            'scope_type_required': scope_type_required,
            'supported_scopes': supported_scopes,
            'approval_steps': chain_info['steps'],
            'approval_desc': chain_info['desc'],
            'is_builtin': r.is_builtin,
        })

    # 按 rank 排序(等级低的在前,便于前端按层级展示)
    rows.sort(key=lambda x: x['rank'])
    return rows


def preview_approval_chain(applicant, role_key, scope_type, scope_id, change_type):
    """预生成审批链供前端提交前预览，不创建工单

    返回 (nodes, target_scope_name)；参数非法或构造失败抛 ValueError。
    nodes 为链节点列表（含 approver_role 标签 + scope 名称解析）。
    """
    if scope_type in (ScopeType.TEAM, ScopeType.DEPT) and not scope_id:
        raise ValueError(f'scope_type={scope_type} 时 scope_id 必填')

    role = Role.objects.filter(role_key=role_key, is_deleted=False).first()
    if not role:
        raise ValueError(f'角色不存在: {role_key}')

    # scope_id 类型转换
    try:
        scope_id_int = int(scope_id) if scope_id else None
    except (TypeError, ValueError):
        raise ValueError('scope_id 应为整数')

    # 构造审批链(不创建工单,仅预览)
    # 延迟导入：保持测试可 patch apps.users.ticket_service.build_approval_chain，
    # 避免模块加载时提前绑定函数对象导致 mock 失效
    from apps.users.ticket_service import build_approval_chain
    try:
        chain = build_approval_chain(
            applicant=applicant,
            target_user=applicant,
            change_type=change_type,
            role=role,
            scope_type=scope_type,
            scope_id=scope_id_int,
        )
    except Exception as e:
        logger.exception(f'[ChainPreview] 构造审批链失败: {e}')
        raise ValueError('构造审批链失败,请检查参数')

    # 解析每个节点的 scope 名称 + 审批人角色标签
    approver_role_labels = {
        ApproverRole.TEAM_LEADER: '团队组长',
        ApproverRole.DEPT_LEADER: '部门经理',
        ApproverRole.USER_ADMIN: '用户管理员',
        ApproverRole.SUPER_ADMIN: '超级管理员',
    }
    nodes = []
    for idx, node in enumerate(chain):
        node_scope_type = node.get('approver_scope_type', ScopeType.NONE)
        node_scope_id = node.get('approver_scope_id')
        node_scope_name = _resolve_scope_name(node_scope_type, node_scope_id)

        nodes.append({
            'step': idx + 1,
            'approver_role': node.get('approver_role'),
            'approver_role_label': approver_role_labels.get(node.get('approver_role'), node.get('approver_role')),
            'approver_scope_type': node_scope_type,
            'approver_scope_id': node_scope_id,
            'approver_scope_name': node_scope_name,
            'status': node.get('status', 'PENDING'),
        })

    # 解析申请目标 scope 名称
    target_scope_name = _resolve_scope_name(scope_type, scope_id_int)

    return nodes, target_scope_name, role.name
