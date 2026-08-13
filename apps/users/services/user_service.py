"""用户管理业务逻辑：管理范围判定、角色分配校验、软删除、批量导入

从 UserViewSet 下沉的纯业务逻辑（仅依赖 user 对象与模型，不依赖 HTTP 细节），
视图层只保留参数校验、序列化与响应组装。
"""
import secrets
import string as _string

from django.contrib.auth import get_user_model
from django.utils import timezone
from loguru import logger

from apps.users.models import (
    Department, Team, Role,
    RolePermissionRel, UserRoleRel,
    has_permission, get_user_data_scope_level,
    get_user_managed_depts, get_user_managed_teams, DataScope,
    UserStatus, GrantStatus,
)
from rest_framework.exceptions import PermissionDenied

User = get_user_model()


def check_user_manage(user, target_user=None):
    """检查请求者是否有用户管理权限（及其范围）

    基于 permission_key + data_scope 判定：
    - user.manage_all：全局用户管理
    - user.manage + DEPT scope：部门属地范围内用户管理
    - user.manage + TEAM scope：团队属地范围内用户管理
    """
    u = user
    # 超级管理员永远放行（系统级快路径）
    if u.is_super_admin:
        return True
    # 拥有 user.manage_all 权限可管理全局用户
    if has_permission(u, 'user.manage_all'):
        return True
    # 拥有 user.manage 权限：按数据范围等级判定
    if has_permission(u, 'user.manage'):
        if target_user:
            u_scope = get_user_data_scope_level(u)
            # 部门级：可管理本部门（含属地授权部门）用户
            if u_scope == DataScope.DEPT:
                managed_depts = get_user_managed_depts(u)
                if target_user.department_id and target_user.department_id in managed_depts:
                    return True
            # 团队级：可管理本团队（含属地授权团队）用户
            if u_scope == DataScope.TEAM:
                managed_teams = get_user_managed_teams(u)
                if target_user.team_id and target_user.team_id in managed_teams:
                    return True
        return False
    return False


def get_user_manage_scope(user):
    """获取用户管理角色标识（用于创建/导入用户等需要判断管理层级的场景）

    返回 dict: {'is_super': bool, 'can_manage_all': bool, 'is_dept': bool, 'is_team': bool}
    四个维度互斥递进：超管 > 全局管理 > 部门管理 > 团队管理。
    供 views_users.py 的 create / batch_import 共用，避免重复计算。
    """
    u = user
    is_super = u.is_super_admin
    can_manage_all = has_permission(u, 'user.manage_all')
    is_dept = False
    is_team = False
    if not is_super and not can_manage_all and has_permission(u, 'user.manage'):
        scope = get_user_data_scope_level(u)
        if scope == DataScope.DEPT:
            is_dept = True
        elif scope == DataScope.TEAM:
            is_team = True
    return {
        'is_super': is_super,
        'can_manage_all': can_manage_all,
        'is_dept': is_dept,
        'is_team': is_team,
    }


def check_can_manage_user(user, target_user):
    """检查是否可以禁用/启用/删除用户（基于 permission_key 判定，避免角色硬编码）

    通过 permission_key + data_scope 判定：
    - super_admin 系统级快路径
    - user.manage_all（GLOBAL scope）：可禁用除超管和其他 manage_all 持有者外的用户
    - user.manage（DEPT scope）：只能禁用本部门非管理者
    - user.manage（TEAM scope）：只能禁用本团队非管理者
    返回 (can: bool, msg: str)。
    """
    u = user
    # 规则1：必须有用户管理权限（super_admin / user.manage_all / user.manage）
    if not (u.is_super_admin or has_permission(u, 'user.manage_all') or has_permission(u, 'user.manage')):
        return False, "没有禁用权限"

    # 规则2：不能操作自己
    if u.id == target_user.id:
        return False, "不能禁用自己"

    # 规则3：超级管理员不能被禁用（系统级快路径保护，防止锁死管理入口）
    if target_user.is_super_admin:
        return False, "超级管理员不能被禁用"

    # 规则4：超级管理员可以禁用除超级管理员以外的所有用户
    if u.is_super_admin:
        return True, ""

    # 以下按数据范围等级判定（GLOBAL > DEPT > TEAM）
    u_scope = get_user_data_scope_level(u)

    # 规则5：全局级（user.manage_all）——可禁用除超管和其他 manage_all 持有者外的用户
    if u_scope == DataScope.GLOBAL:
        if has_permission(target_user, 'user.manage_all'):
            return False, "不能禁用同级用户管理员"
        return True, ""

    # 规则6：部门级管理者——只能操作本部门（含属地授权部门）用户
    if u_scope == DataScope.DEPT:
        managed_depts = get_user_managed_depts(u)
        if not target_user.department_id or target_user.department_id not in managed_depts:
            return False, "只能禁用本部门员工"
        # 不能禁用同级管理者（拥有 user.manage / user.manage_all 的用户）
        if has_permission(target_user, 'user.manage') or has_permission(target_user, 'user.manage_all'):
            return False, "不能禁用同级部门经理"
        return True, ""

    # 规则7：团队级管理者——只能操作本团队（含属地授权团队）用户
    if u_scope == DataScope.TEAM:
        managed_teams = get_user_managed_teams(u)
        if not target_user.team_id or target_user.team_id not in managed_teams:
            return False, "只能禁用本组员工"
        # 不能禁用同级管理者
        if has_permission(target_user, 'user.manage') or has_permission(target_user, 'user.manage_all'):
            return False, "不能禁用同级团队组长"
        return True, ""

    return False, "无权限操作"


def get_manageable_user_ids(user):
    """获取当前用户可管理的用户ID集合

    返回 None 表示可管理所有用户（super_admin / user.manage_all）。
    """
    u = user
    # 拥有 user.manage_all 权限可管理所有用户（RBAC）
    if u.is_super_admin or has_permission(u, 'user.manage_all'):
        return None
    # 拥有 user.manage 权限：按 data_scope 判定管理范围
    if has_permission(u, 'user.manage'):
        u_scope = get_user_data_scope_level(u)
        # 部门级：可管理本部门（含属地授权部门）用户
        if u_scope == DataScope.DEPT:
            managed_depts = get_user_managed_depts(u)
            return set(User.objects.filter(department_id__in=managed_depts, is_deleted=False).values_list('id', flat=True))
        # 团队级：可管理本团队（含属地授权团队）用户
        if u_scope == DataScope.TEAM:
            managed_teams = get_user_managed_teams(u)
            return set(User.objects.filter(team_id__in=managed_teams, is_deleted=False).values_list('id', flat=True))
    # 普通用户只能管理自己
    return {u.id}


def filter_downward_roles(role_ids, is_dept):
    """组长只能分配 contributor/viewer；部门经理可额外分配 team_leader

    viewer 为默认准入只读角色；contributor 为申请通过后获得的读/写/下载角色。
    """
    allowed_keys = ['contributor', 'viewer']
    if is_dept:
        allowed_keys = ['team_leader', 'contributor', 'viewer']
    allowed_ids = set(Role.objects.filter(role_key__in=allowed_keys).values_list('id', flat=True))
    return [rid for rid in (role_ids or []) if rid in allowed_ids]


def filter_role_ids(user, role_ids):
    """检查角色ID，非超管不能分配高级角色，检测到受限角色时抛出403错误

    通过 permission_key 判定受限角色：拥有 kb.manage_all / user.manage_all
    权限的角色视为高级角色；super_admin 角色也受限（系统级快路径角色）。
    """
    u = user
    # 超级管理员可以分配任意角色
    if u.is_super_admin:
        return role_ids
    # 非超管不能分配高级角色：通过 RolePermissionRel 反查拥有 *_manage_all 权限的角色
    restricted_ids = set(RolePermissionRel.objects.filter(
        permission__permission_key__in=['kb.manage_all', 'user.manage_all'],
        is_active=True,
    ).values_list('role_id', flat=True))
    # super_admin 角色也受限（系统级快路径角色，不可委派）
    sa_role_id = Role.objects.filter(role_key='super_admin').values_list('id', flat=True).first()
    if sa_role_id:
        restricted_ids.add(sa_role_id)
    has_restricted = role_ids and restricted_ids & set(role_ids)
    if has_restricted:
        raise PermissionDenied("无权分配高级角色")
    return role_ids


def validate_role_uniqueness(user, role_ids, department_id=None, team_ids=None):
    """校验 dept_manager 和 team_leader 的唯一性约束，冲突返回错误信息（str），无冲突返回 None"""
    if not role_ids:
        return None
    # 批量查询所有 role_id 对应的 role_key，避免 N+1
    role_map = dict(Role.objects.filter(id__in=role_ids).values_list('id', 'role_key'))
    dept_manager_role_id = None
    team_leader_role_id = None
    for rid, rkey in role_map.items():
        if rkey == 'dept_manager':
            dept_manager_role_id = rid
        if rkey == 'team_leader':
            team_leader_role_id = rid

    # 部门经理唯一性：同一部门只能有一个部门经理
    if dept_manager_role_id and department_id:
        existing = UserRoleRel.objects.filter(
            role__role_key='dept_manager',
            status=GrantStatus.ACTIVE,
        ).exclude(user=user).filter(
            user__department_id=department_id,
            user__is_deleted=False
        ).first()
        if existing:
            return f"该部门已有部门经理：{existing.user.real_name or existing.user.username}"

    # 团队 leader 唯一性：同一团队只能有一个 team_leader
    # 单团队 FK：user__team_id 直接匹配
    if team_leader_role_id and team_ids:
        for tid in team_ids:
            existing = UserRoleRel.objects.filter(
                role__role_key='team_leader',
                status=GrantStatus.ACTIVE,
            ).exclude(user=user).filter(
                user__team_id=tid,
                user__is_deleted=False
            ).first()
            if existing:
                team_name = Team.objects.filter(id=tid).values_list('name', flat=True).first() or f'团队#{tid}'
                return f"团队“{team_name}”已有团队组长：{existing.user.real_name or existing.user.username}"
    return None


def soft_delete(user):
    """软删除用户：仅标记 is_deleted，保留原 username/email 供恢复识别

    username 全局唯一阻止同名新建；email partial unique 允许同邮箱命中软删除记录 → 询问恢复
    """
    user.is_deleted = True
    user.deleted_at = timezone.now()
    user.status = UserStatus.DISABLED
    user.save(update_fields=['is_deleted', 'deleted_at', 'status', 'updated_at'])


def sync_role_leader(user, role_ids_set):
    """分配/移除 team_leader / dept_manager 角色时，同步更新 Team.leader_id / Department.leader_id"""
    role_key_map = dict(Role.objects.filter(
        role_key__in=['team_leader', 'dept_manager']
    ).values_list('id', 'role_key'))

    has_tl = any(role_key_map.get(rid) == 'team_leader' for rid in role_ids_set)
    has_dm = any(role_key_map.get(rid) == 'dept_manager' for rid in role_ids_set)

    if has_tl:
        # 单团队 FK：用户只有一个 team
        if user.team_id:
            Team.objects.filter(id=user.team_id, leader__isnull=True).update(leader=user)
    else:
        Team.objects.filter(leader=user).update(leader=None)

    if has_dm:
        if user.department_id:
            Department.objects.filter(id=user.department_id, leader__isnull=True).update(leader=user)
    else:
        Department.objects.filter(leader=user).update(leader=None)


def import_users_batch(rows, col_map, actor, is_dept, is_team,
                       dept_map, team_map, viewer_role, my_team_ids):
    """批量导入员工核心逻辑：逐行校验并创建，返回 (success_count, fail_count, out_rows)

    rows 为去除表头后的原始数据行；col_map 为列名→索引映射。
    out_rows 为原行 + [结果, 原因]，由视图层包装成 CSV 返回。
    权限判定（is_team/is_dept）由调用方提前算好，此处只做数据落地。
    """
    from django.db import transaction

    out_buf_rows = []
    success_count = 0
    fail_count = 0

    for line_no, row in enumerate(rows, start=2):
        def get_col(name):
            idx = col_map.get(name)
            if idx is None or idx >= len(row):
                return ""
            return (row[idx] or "").strip()

        username = get_col("用户名")
        real_name = get_col("姓名")
        email = get_col("邮箱")
        dept_name = get_col("部门")
        team_name = get_col("团队")
        status_str = get_col("状态")

        result = "失败"
        reason = ""

        # 行级校验
        if not username:
            reason = "用户名不能为空"
        elif not real_name:
            reason = "姓名不能为空"
        elif not email:
            reason = "邮箱不能为空"
        elif User.objects.filter(username=username, is_deleted=False).exists():
            reason = f"用户名「{username}」已存在"
        elif User.objects.filter(email=email, is_deleted=False).exists():
            reason = f"邮箱「{email}」已被使用"
        else:
            # 解析部门
            dept_id = None
            department = None
            if dept_name:
                department = dept_map.get(dept_name)
                if not department:
                    reason = f"部门「{dept_name}」不存在"
                else:
                    dept_id = department.id

            # 非超管权限范围校验
            if not reason:
                if is_team and dept_id and dept_id != actor.department_id:
                    reason = "组长只能导入本部门员工"
                elif is_dept and dept_id and dept_id != actor.department_id:
                    reason = "部门经理只能导入本部门员工"

            # 解析团队
            team_id = None
            if not reason and team_name and dept_id:
                team = team_map.get((team_name, dept_id))
                if not team:
                    reason = f"团队「{team_name}」在部门「{dept_name}」下不存在"
                else:
                    team_id = team.id
                    # 组长只能导入本团队
                    if is_team and team_id not in my_team_ids:
                        reason = "组长只能导入本团队员工"

            # 解析状态
            status_val = UserStatus.ACTIVE
            if status_str:
                if status_str in ("启用", "active"):
                    status_val = UserStatus.ACTIVE
                elif status_str in ("禁用", "disabled"):
                    status_val = UserStatus.DISABLED
                else:
                    reason = f"状态「{status_str}」无效，应为 启用/禁用"

            # 创建用户
            if not reason:
                try:
                    with transaction.atomic():
                        # 使用加密安全随机数生成默认密码，避免可预测性攻击
                        alphabet = _string.ascii_letters + _string.digits + "!@#$"
                        pwd = ''.join(secrets.choice(alphabet) for _ in range(12))
                        user = User.objects.create(
                            username=username,
                            email=email,
                            real_name=real_name,
                            department_id=dept_id,
                            status=status_val,
                        )
                        user.set_password(pwd)
                        user.save(update_fields=['password'])
                        # 导入用户默认 viewer 角色
                        if viewer_role:
                            UserRoleRel.objects.create(
                                user=user, role_id=viewer_role.id, status='ACTIVE', granted_by=actor
                            )
                        # 单团队 FK：直接设置 user.team_id
                        if team_id:
                            user.team_id = team_id
                            user.save(update_fields=['team', 'updated_at'])
                    result = "成功"
                    success_count += 1
                except Exception as e:
                    logger.error(f"User.batch_import - row {line_no} create failed: {e}")
                    reason = "创建失败，请联系管理员"

        if result == "失败":
            fail_count += 1
        out_buf_rows.append(row + [result, reason])

    logger.info(f"User.batch_import - user: {actor.username}, success: {success_count}, fail: {fail_count}")
    return success_count, fail_count, out_buf_rows
