"""
RBAC 权限种子数据脚本 —— 写入 8 个内置角色、权限点清单、角色-权限绑定关系。

8 个内置角色：
- super_admin: 超级管理员（系统级快路径，鉴权绕过 permission_key）
- user_admin: 人员管理员
- kb_admin: 文档管理员
- compliance_admin: 合规审计员
- dept_manager: 部门经理
- team_leader: 团队组长
- viewer: 查看者（兜底角色，随人事归属生效自带只读）
- contributor: 参与者（显式授权角色，需申请获得，读/写/下载本人团队文档）

幂等设计：重复执行不会报错、不会产生重复记录，已存在的记录跳过不覆盖人工改动。
适用场景：系统初始化部署、权限点清单/绑定关系升级后再次对齐。
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.users.models import (
    DataScope,
    Permission,
    Role,
    RolePermissionRel,
    RoleType,
)


# ============================================================================
# 权限点清单：(permission_key, permission_name, module)
# permission_key 三段式 module.resource.action；个别四级 key（如 kb.document.access.approve）
# 用于在 resource 内进一步细分动作，仍归入所属 module。
# 所有权限点 is_builtin=True，由本脚本维护，不可手工删除。
# ============================================================================
PERMISSIONS = [
    # org 模块 —— 组织架构管理（部门 / 团队）
    ('org.dept.create', '创建部门', 'org'),
    ('org.dept.update', '更新部门', 'org'),
    ('org.dept.delete', '删除部门', 'org'),
    ('org.team.create', '创建团队', 'org'),
    ('org.team.update', '更新团队', 'org'),
    ('org.team.delete', '删除团队', 'org'),

    # user 模块 —— 人员管理（邀请 / 部门团队内管理 / 全局管理 / 调岗 / 禁用）
    ('user.invite', '邀请员工', 'user'),
    ('user.manage', '管理部门/团队内用户', 'user'),
    ('user.manage_all', '全局用户管理', 'user'),
    ('user.transfer', '员工调岗', 'user'),
    ('user.disable', '禁用用户', 'user'),

    # role 模块 —— 角色授权与撤销（按授权 Scope 区分审批链路）
    ('role.grant.global', '授予全局角色', 'role'),
    ('role.grant.dept', '授予部门角色', 'role'),
    ('role.grant.team', '授予团队角色', 'role'),
    ('role.revoke', '撤销角色', 'role'),

    # kb 模块 —— 知识库全局管理 / 节点管理 / 文档管理
    ('kb.manage_all', '知识库全局管理', 'kb'),
    ('kb.node.manage', '知识节点管理', 'kb'),
    ('kb.node.create', '创建节点', 'kb'),
    ('kb.node.move', '移动节点', 'kb'),
    ('kb.node.delete', '删除节点', 'kb'),
    ('kb.document.read', '读取文档', 'kb'),
    ('kb.document.upload', '上传文档', 'kb'),
    ('kb.document.delete', '删除文档', 'kb'),
    ('kb.document.update', '更新文档', 'kb'),
    ('kb.document.download', '下载文档', 'kb'),
    ('kb.document.share', '分享文档', 'kb'),
    ('kb.document.promote_to_dept', '文档上推至部门级', 'kb'),
    ('kb.document.access.approve', '批准文档访问申请', 'kb'),
    ('kb.document.block.manage', '管理文档黑名单', 'kb'),

    # audit 模块 —— 审计日志与完整性校验（合规要求只读不可删）
    ('audit.log.read', '查看审计日志', 'audit'),
    ('audit.log.export', '导出审计日志', 'audit'),
    ('audit.integrity.check', '完整性校验', 'audit'),

    # analytics 模块 —— 运营分析报表
    ('analytics.system.read', '系统级指标', 'analytics'),
    ('analytics.system.write', '系统级写（关键词权重/反馈状态/报告删除）', 'analytics'),
    ('analytics.org.read', '组织使用报表', 'analytics'),

    # compliance 模块 —— 合规审计
    ('compliance.audit', '合规审计', 'compliance'),
]


# ============================================================================
# 8 个内置角色定义：(role_key, name, role_type, data_scope, description)
# 内置角色 is_builtin=True，全局角色不绑定具体组织。
# role_type 决定授权时是否需绑定管辖 Scope（GLOBAL 无需 / DEPT_SCOPE 绑部门 / TEAM_SCOPE 绑团队）。
# ============================================================================
BUILTIN_ROLES = [
    # 超级管理员 —— 系统级快路径，鉴权绕过 permission_key
    (
        'super_admin', '超级管理员',
        RoleType.GLOBAL, DataScope.GLOBAL,
        '最高权限（系统级快路径，鉴权绕过 permission_key）',
    ),
    (
        'user_admin', '人员管理员',
        RoleType.GLOBAL, DataScope.GLOBAL,
        '管理组织/人员/部门/团队，不可操作文档',
    ),
    (
        'kb_admin', '文档管理员',
        RoleType.GLOBAL, DataScope.GLOBAL,
        '管理全部知识库/文档，不可管理人',
    ),
    (
        'compliance_admin', '合规审计员',
        RoleType.GLOBAL, DataScope.GLOBAL,
        '查看审计日志/合规校验，只读',
    ),
    (
        'dept_manager', '部门经理',
        RoleType.DEPT_SCOPE, DataScope.DEPT,
        '管理指定部门人员/部门级知识库',
    ),
    (
        'team_leader', '团队组长',
        RoleType.TEAM_SCOPE, DataScope.TEAM,
        '管理指定团队人员/团队级知识库',
    ),
    (
        'viewer', '查看者',
        RoleType.NORMAL_USER, DataScope.TEAM,
        '兜底角色，随人事归属生效自带只读，未显式授权 contributor 时自动叠加',
    ),
    (
        'contributor', '参与者',
        RoleType.NORMAL_USER, DataScope.TEAM,
        '显式授权角色，需申请获得，获得后覆盖 viewer 兜底，可查看/上传/下载文档',
    ),
]


# ============================================================================
# 角色-权限绑定关系：role_key -> [permission_key 或通配符, ...]
# 通配符语义（统一为前缀匹配，便于覆盖同 resource 下的多级 key）：
#   '*'          → 全部权限点（仅 super_admin 使用）
#   'xxx.*'      → 所有以 'xxx.' 开头的 permission_key
#                  例如 'kb.document.*' 会覆盖 'kb.document.access.approve'
#                  这类四级 key（同属 document resource 组，前缀一致即归属）
# ============================================================================
ROLE_PERMISSIONS = {
    # 超级管理员：系统级快路径角色，权限全部放开
    'super_admin': ['*'],

    # 人员管理员：组织+人员全域管理，不持有任何 kb.* 文档权限
    'user_admin': [
        'org.*',
        'user.invite', 'user.manage', 'user.manage_all', 'user.transfer', 'user.disable',
        'role.grant.dept', 'role.grant.team', 'role.revoke',
        'audit.log.read',
    ],

    # 文档管理员：知识库+文档全域管理，不持有任何 org.*/user.* 人员权限
    'kb_admin': [
        'kb.manage_all', 'kb.node.*', 'kb.document.*',
        'audit.log.read',
    ],

    # 合规审计员：只读审计/合规/分析，不持有任何写操作权限
    'compliance_admin': [
        'audit.log.read', 'audit.log.export', 'audit.integrity.check',
        'compliance.audit',
        'analytics.system.read', 'analytics.org.read',
    ],

    # 部门经理：本部门人员/团队/节点/文档管理
    'dept_manager': [
        'org.team.create', 'org.team.update',
        'user.invite', 'user.manage',
        'role.grant.team', 'role.revoke',
        'kb.node.manage', 'kb.node.create', 'kb.node.move',
        'kb.document.read', 'kb.document.upload', 'kb.document.delete',
        'kb.document.update', 'kb.document.download', 'kb.document.share',
        'kb.document.access.approve', 'kb.document.block.manage',
        'analytics.org.read',
    ],

    # 团队组长：本团队人员/节点/文档管理，可上推文档至部门级
    'team_leader': [
        'user.invite',
        'role.revoke',
        'kb.node.manage', 'kb.node.create',
        'kb.document.read', 'kb.document.upload', 'kb.document.delete',
        'kb.document.update', 'kb.document.download', 'kb.document.share',
        'kb.document.promote_to_dept',
        'kb.document.access.approve', 'kb.document.block.manage',
        'analytics.org.read',
    ],

    # 参与者：随人事归属生效的兜底角色，仅本团队文档读/上传/下载
    'contributor': [
        'kb.document.read', 'kb.document.upload', 'kb.document.download',
    ],

    # 查看者：默认准入角色，仅可读取文档，无下载/写操作权限
    # 需通过申请流程升级为 contributor 获得写权限
    'viewer': [
        'kb.document.read',
    ],
}


def expand_wildcards(spec_list, all_keys):
    """将绑定规则中的通配符展开为具体 permission_key 列表。

    输入：
      - spec_list: 绑定规则列表，元素可为 '*'、'xxx.*' 前缀通配符或具体 permission_key
      - all_keys: 全量 permission_key 列表（用于通配符展开，保持 PERMISSIONS 顺序）
    输出：去重后的具体 permission_key 列表（保持首次出现顺序）

    何时走到：在写入角色-权限绑定前，对每个角色的 spec_list 做一次展开，
    把通配符翻译成 Permission 表中真实存在的 key，再逐条 update_or_create。
    """
    expanded = []
    seen = set()
    for spec in spec_list:
        if spec == '*':
            # 超级管理员专用：取全部权限点
            candidates = all_keys
        elif spec.endswith('.*'):
            # 前缀通配：'xxx.*' -> 以 'xxx.' 开头的所有 key
            # 保留末尾的 '.' 作为前缀，避免 'kb.document.*' 误匹配 'kb.documentX' 类 key
            prefix = spec[:-1]
            candidates = [k for k in all_keys if k.startswith(prefix)]
        else:
            # 具体权限点，直接使用
            candidates = [spec]
        for key in candidates:
            if key not in seen:
                seen.add(key)
                expanded.append(key)
    return expanded


class Command(BaseCommand):
    help = '写入 RBAC 权限种子数据：8 个内置角色、权限点清单、角色-权限绑定关系（幂等可重复执行）'

    def add_arguments(self, parser):
        # --reset 仅开发环境使用：绑定规则变更后强制对齐线上数据，
        # 会先清除 8 个内置角色的全部绑定再重建；生产环境慎用
        parser.add_argument(
            '--reset',
            action='store_true',
            help='清除 8 个内置角色的所有权限绑定后重建（开发调试用，谨慎使用）',
        )

    def handle(self, *args, **options):
        """执行种子数据写入。

        幂等保证：
          - 角色/权限点用 get_or_create：已存在则跳过，不覆盖人工改动
          - 角色绑定用 update_or_create：已存在则强制 is_active=True（恢复曾软删的绑定）
        全程包裹在 transaction.atomic 中，任一步失败整体回滚，避免半成品数据。
        """
        reset = options.get('reset', False)

        # 统计计数器：created=本次新建，skipped=已存在跳过
        stats = {
            'role_created': 0, 'role_skipped': 0,
            'perm_created': 0, 'perm_skipped': 0,
            'binding_created': 0, 'binding_skipped': 0,
        }

        with transaction.atomic():
            # 1) 内置角色：role_key 全局唯一，is_builtin=True
            #    用 get_or_create 而非 update_or_create：避免覆盖运维对 name/description 的手工调整
            role_map = {}
            for role_key, name, role_type, data_scope, description in BUILTIN_ROLES:
                role, created = Role.objects.get_or_create(
                    role_key=role_key,
                    defaults={
                        'name': name,
                        'role_type': role_type,
                        'data_scope': data_scope,
                        'description': description,
                        'is_builtin': True,
                    },
                )
                role_map[role_key] = role
                if created:
                    stats['role_created'] += 1
                else:
                    stats['role_skipped'] += 1

            # 2) 权限点清单：permission_key 全局唯一，is_builtin=True
            perm_map = {}
            for permission_key, permission_name, module in PERMISSIONS:
                perm, created = Permission.objects.get_or_create(
                    permission_key=permission_key,
                    defaults={
                        'permission_name': permission_name,
                        'module': module,
                        'is_builtin': True,
                    },
                )
                perm_map[permission_key] = perm
                if created:
                    stats['perm_created'] += 1
                else:
                    stats['perm_skipped'] += 1

            # --reset：清除内置角色的全部绑定后重建。
            # 普通幂等模式只新增/激活绑定，不会删除已被移除的冗余绑定；
            # --reset 用于绑定规则删减后强制对齐线上数据，仅开发环境使用。
            if reset:
                deleted_count, _ = RolePermissionRel.objects.filter(
                    role__role_key__in=ROLE_PERMISSIONS.keys(),
                ).delete()
                self.stdout.write(self.style.WARNING(
                    f'--reset 已清除内置角色绑定 {deleted_count} 条，开始重建'
                ))

            # 3) 角色-权限绑定：按 role + permission update_or_create，
            #    is_active=True 顺便恢复曾软删（is_active=False）的绑定；
            #    revoked_at 置空避免审计字段残留误导
            all_keys = [pk for pk, _, _ in PERMISSIONS]
            for role_key, spec_list in ROLE_PERMISSIONS.items():
                role = role_map[role_key]
                for permission_key in expand_wildcards(spec_list, all_keys):
                    permission = perm_map[permission_key]
                    _, created = RolePermissionRel.objects.update_or_create(
                        role=role,
                        permission=permission,
                        defaults={
                            'is_active': True,
                            'revoked_at': None,
                        },
                    )
                    if created:
                        stats['binding_created'] += 1
                    else:
                        stats['binding_skipped'] += 1

        # 输出统计：created/skipped 数量
        self.stdout.write(self.style.SUCCESS('权限种子数据写入完成：'))
        self.stdout.write(self.style.SUCCESS(
            f"  角色：新建 {stats['role_created']} / 跳过 {stats['role_skipped']}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  权限点：新建 {stats['perm_created']} / 跳过 {stats['perm_skipped']}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  绑定关系：新建 {stats['binding_created']} / 跳过 {stats['binding_skipped']}"
        ))
