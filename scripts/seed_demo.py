"""
种子数据：部门、团队、权限点、角色、角色-权限映射、演示用户
运行方式: python scripts/seed_demo.py   (需在项目根目录)

RBAC 角色体系 (6 种 × 4 级 scope):
  超级管理员  super_admin   - 人员管理 + 文档只读/恢复
  知识库运维    kb_ops        - 知识库全部 CRUD，无人员管理
  部门经理      dept_manager   - 本部门人员+文档全部权限
  Team Leader   team_leader    - 本组人员+文档全部权限
  普通员工      employee      - 编辑个人文档
  只读员工      readonly      - 只读

权限动作: read / upload / edit / delete / export / share / manage / restore / config
权限范围: all / department / team / personal
"""
import os
import sys

from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rag_project.settings")

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

import django
django.setup()

from django.db import transaction
from apps.users.models import (
    Department, Team, Role, Permission, RolePermission,
    SysUser,
)


# ============================================================
# 1. 权限点定义
# ============================================================
PERMISSION_DEFS = [
    # ---- 知识库权限 (knowledge) ----
    # read（所有人默认拥有 all/department/team 的 read）
    ("knowledge:read:all",       "检索所有文档",        "knowledge", "read", "all"),
    ("knowledge:read:department","检索本部门文档",       "knowledge", "read", "department"),
    ("knowledge:read:team",      "检索本组文档",        "knowledge", "read", "team"),
    ("knowledge:read:personal",  "检索个人文档",        "knowledge", "read", "personal"),
    # upload
    ("knowledge:upload:all",     "上传到任意节点",       "knowledge", "upload", "all"),
    ("knowledge:upload:department","上传到本部门节点",   "knowledge", "upload", "department"),
    ("knowledge:upload:team",    "上传到本组节点",       "knowledge", "upload", "team"),
    # edit
    ("knowledge:edit:all",       "编辑所有文档",         "knowledge", "edit", "all"),
    ("knowledge:edit:department","编辑本部门文档",       "knowledge", "edit", "department"),
    ("knowledge:edit:team",      "编辑本组文档",         "knowledge", "edit", "team"),
    ("knowledge:edit:personal",  "编辑个人文档",         "knowledge", "edit", "personal"),
    # delete
    ("knowledge:delete:all",     "删除所有文档",         "knowledge", "delete", "all"),
    ("knowledge:delete:department","删除本部门文档",     "knowledge", "delete", "department"),
    ("knowledge:delete:team",    "删除本组文档",         "knowledge", "delete", "team"),
    ("knowledge:delete:personal","删除个人文档",         "knowledge", "delete", "personal"),
    # export
    ("knowledge:export:all",     "导出所有文档",         "knowledge", "export", "all"),
    ("knowledge:export:department","导出本部门文档",     "knowledge", "export", "department"),
    ("knowledge:export:team",    "导出本组文档",         "knowledge", "export", "team"),
    ("knowledge:export:personal","导出个人文档",         "knowledge", "export", "personal"),
    # share
    ("knowledge:share:all",      "授权所有文档",         "knowledge", "share", "all"),
    ("knowledge:share:department","授权本部门文档",      "knowledge", "share", "department"),
    ("knowledge:share:team",     "授权本组文档",         "knowledge", "share", "team"),
    # restore (特殊动作，无 scope)
    ("knowledge:restore",        "恢复已删除文档",       "knowledge", "restore", ""),

    # ---- 用户管理权限 (user) ----
    ("user:manage:all",          "管理所有用户",         "user", "manage", "all"),
    ("user:manage:department",   "管理本部门用户",       "user", "manage", "department"),
    ("user:manage:team",         "管理本组用户",         "user", "manage", "team"),

    # ---- 审计日志权限 (audit) ----
    ("audit:read:all",           "查看所有审计日志",     "audit", "read", "all"),
    ("audit:read:department",    "查看本部门审计日志",   "audit", "read", "department"),
    ("audit:read:team",          "查看本组审计日志",     "audit", "read", "team"),
    ("audit:read:personal",      "查看个人审计日志",     "audit", "read", "personal"),

    # ---- 系统管理权限 (system) ----
    ("system:config",            "系统配置管理",         "system", "config", ""),
]

# ============================================================
# 2. 角色定义 & 权限分配
# ============================================================
ROLE_DEFS = {
    "super_admin": {
        "name": "超级管理员",
        "desc": "人员管理 + 文档只读/恢复权限",
        "perms": [
            "knowledge:read:all", "knowledge:restore",
            "user:manage:all",
            "audit:read:all",
            "system:config",
        ],
    },
    "kb_ops": {
        "name": "知识库运维",
        "desc": "知识库全部 CRUD 权限，无人员管理",
        "perms": [
            "knowledge:read:all", "knowledge:upload:all",
            "knowledge:edit:all", "knowledge:delete:all",
            "knowledge:export:all", "knowledge:share:all",
            "knowledge:restore",
            "audit:read:all",
        ],
    },
    "dept_manager": {
        "name": "部门经理",
        "desc": "本部门人员 + 文档全部权限",
        "perms": [
            "knowledge:read:all",
            "knowledge:upload:department",
            "knowledge:edit:department",
            "knowledge:delete:department",
            "knowledge:export:department",
            "knowledge:share:department",
            "user:manage:department",
            "audit:read:department",
        ],
    },
    "team_leader": {
        "name": "Team Leader",
        "desc": "本组人员 + 文档全部权限",
        "perms": [
            "knowledge:read:all",
            "knowledge:upload:team",
            "knowledge:edit:team",
            "knowledge:delete:team",
            "knowledge:export:team",
            "knowledge:share:team",
            "user:manage:team",
            "audit:read:team",
        ],
    },
    "employee": {
        "name": "普通员工",
        "desc": "可编辑个人上传的文档",
        "perms": [
            "knowledge:read:all",
            "knowledge:read:department",
            "knowledge:read:team",
            "knowledge:upload:team",
            "knowledge:edit:personal",
            "knowledge:delete:personal",
            "knowledge:export:personal",
            "audit:read:personal",
        ],
    },
    "readonly": {
        "name": "只读员工",
        "desc": "仅检索和在线预览",
        "perms": [
            "knowledge:read:all",
            "audit:read:personal",
        ],
    },
}


def seed():
    logger.info("=== 开始种子数据初始化 ===")

    # --- 1. 部门 & 团队 ---
    dept, _ = Department.objects.get_or_create(
        name="研发中心", defaults={"code": "rd"}
    )
    logger.info(f"  部门: {dept.name}")

    teams_data = [
        ("ai_platform", "AI 平台组"),
        ("backend", "后端组"),
        ("frontend", "前端组"),
        ("devops", "运维组"),
    ]
    teams = {}
    for code, name in teams_data:
        t, _ = Team.objects.get_or_create(code=code, defaults={"name": name, "department": dept})
        teams[code] = t
        logger.info(f"  团队: {t.name}")

    # --- 2. 权限点 ---
    perm_map = {}
    for code, name, module, action, scope in PERMISSION_DEFS:
        p, created = Permission.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "action": action, "scope": scope}
        )
        perm_map[code] = p
    logger.info(f"  权限点: {len(perm_map)} 条 {'(新建)' if any(1 for _ in []) else ''}")

    # --- 3. 角色 & 角色权限 ---
    for role_code, info in ROLE_DEFS.items():
        role, _ = Role.objects.update_or_create(
            code=role_code,
            defaults={"name": info["name"], "description": info["desc"], "is_builtin": True}
        )
        # 清空旧的权限映射，重新分配
        RolePermission.objects.filter(role=role).delete()
        for perm_code in info["perms"]:
            p = perm_map.get(perm_code)
            if p:
                RolePermission.objects.get_or_create(role=role, permission=p)
        logger.info(f"  角色: {role.name} ({len(info['perms'])} 权限)")

    # --- 4. 演示用户 ---
    from apps.users.models import UserRole

    demo_users = [
        {
            "username": "admin",
            "email": "admin@example.com",
            "password": "admin12345",
            "real_name": "系统管理员",
            "department": dept,
            "roles": ["super_admin"],
        },
        {
            "username": "kbops",
            "email": "kbops@example.com",
            "password": "demo12345",
            "real_name": "知识库运维",
            "department": dept,
            "roles": ["kb_ops"],
        },
        {
            "username": "deptmgr",
            "email": "deptmgr@example.com",
            "password": "demo12345",
            "real_name": "张经理",
            "department": dept,
            "roles": ["dept_manager"],
        },
        {
            "username": "tleader",
            "email": "tleader@example.com",
            "password": "demo12345",
            "real_name": "王组长",
            "department": dept,
            "roles": ["team_leader"],
        },
        {
            "username": "employee1",
            "email": "emp1@example.com",
            "password": "demo12345",
            "real_name": "李员工",
            "department": dept,
            "roles": ["employee"],
        },
        {
            "username": "readonly1",
            "email": "ro1@example.com",
            "password": "demo12345",
            "real_name": "赵只读",
            "department": dept,
            "roles": ["readonly"],
        },
    ]

    for ud in demo_users:
        username = ud["username"]
        user, created = SysUser.objects.update_or_create(
            username=username,
            defaults={
                "email": ud["email"],
                "real_name": ud["real_name"],
                "department": ud["department"],
                "status": "active",
            }
        )
        if created:
            user.set_password(ud["password"])
            user.save()

        # 角色分配
        for role_code in ud["roles"]:
            role = Role.objects.get(code=role_code)
            UserRole.objects.get_or_create(user=user, role=role)

        action = "新建" if created else "更新"
        logger.info(f"  用户: {user.real_name} (@{username}) [{', '.join(ud['roles'])}] {action}")

    logger.info(f"\n=== 种子数据完成 ===")
    logger.info(f"登录账号（密码均为 demo12345，admin 为 admin12345）:")
    for ud in demo_users:
        logger.info(f"  {ud['username']:12s}  {ud['real_name']}  [{', '.join(ud['roles'])}]")


if __name__ == "__main__":
    seed()
