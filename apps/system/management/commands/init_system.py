"""
系统初始化命令（manage.py init_system）

首次部署时运行，创建必要的账号、角色、权限、部门、系统配置等基础数据。
迭代阶段也可运行：默认走"仅更新系统配置元数据"增量模式，--force 强制重置。

各数据类型的初始化逻辑按类型拆分到同级 init/ 子包：
- init/common.py           公共工具（DB 连接 / 迁移检查 / yaml 加载）
- init/roles.py            角色初始化
- init/permissions.py      权限点初始化
- init/role_permissions.py 角色-权限映射
- init/departments.py      部门初始化
- init/teams.py            团队初始化
- init/users.py            初始用户
- init/global_memories.py  全局记忆
- init/system_configs.py   系统配置（KV，从 .env 迁移而来）
- init/models.py           模型配置（LLM/Embedding/Rerank）

数据文件：
- init/initial_data.yaml        生产数据（超管/角色/权限/配置/模型/记忆）
- init/initial_data.dev.yaml    开发示例数据（部门/团队），--with-org 时合并

本命令仅做流程编排，不包含具体业务逻辑。

使用方法：
    python manage.py init_system
    python manage.py init_system --dry-run
    python manage.py init_system --force        # 强制覆盖已存在的配置项 value
    python manage.py init_system --with-org     # 同时创建示例部门/团队（开发期间使用）

参数：
    --config: 指定配置文件路径（默认: 包内 init/initial_data.yaml）
    --dry-run: 仅预览，不实际写入数据库
    --force: 强制覆盖已存在的数据（含 SystemConfig 的 value）
    --with-org: 同时创建示例部门/团队（默认不创建，生产环境无需示例组织数据）
"""
import os

from django.core.management.base import BaseCommand
from loguru import logger

# init/ 子包与本命令同级，存放各数据类型的初始化逻辑与 yaml 数据
from .init import (
    common,
    roles,
    permissions,
    role_permissions,
    departments,
    teams,
    users,
    global_memories,
    system_configs,
    models,
)

# 数据文件目录：本命令所在目录下的 init/ 子包
_INIT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'init')
_DEFAULT_CONFIG = os.path.join(_INIT_DIR, 'initial_data.yaml')
_DEV_CONFIG = os.path.join(_INIT_DIR, 'initial_data.dev.yaml')


class Command(BaseCommand):
    help = '系统初始化：创建账号/角色/权限/系统配置等基础数据（首次部署或迭代刷新配置默认值）'

    def add_arguments(self, parser):
        parser.add_argument('--config', default=_DEFAULT_CONFIG, help='配置文件路径')
        parser.add_argument('--dry-run', action='store_true', help='仅预览，不实际写入数据库')
        parser.add_argument('--force', action='store_true', help='强制覆盖已存在的数据（含 SystemConfig value）')
        parser.add_argument('--with-org', action='store_true',
                            help='同时创建示例部门/团队（默认不创建，生产环境无需示例组织数据）')

    def handle(self, *args, **options):
        config_path = options['config']
        dry_run = options['dry_run']
        force = options['force']
        with_org = options['with_org']

        if not os.path.exists(config_path):
            logger.info(f'❌ 配置文件不存在: {config_path}')
            return

        logger.info('=' * 60)
        logger.info('RAG-Agent 系统初始化')
        logger.info('=' * 60)
        logger.info(f'配置文件: {config_path}')
        logger.info(f'模式: {"预览模式 (Dry Run)" if dry_run else "实际写入模式"}'
                    f'{" (Force)" if force else ""}')
        logger.info('=' * 60)

        # --- 步骤1: 测试数据库连接 ---
        # settings 未就绪前用 psycopg 直连，避免 ORM 依赖未初始化时报错
        logger.info('\n--- 步骤1: 测试数据库连接 ---')
        if not common.test_db_connection():
            return

        # --- 步骤2: 初始化 Django ---
        # management command 运行时 Django 已 setup，无需手动调用 django.setup()
        logger.info('\n--- 步骤2: 初始化 Django ---')
        logger.info('✅ Django 已由 management 框架初始化')

        # --- 步骤3: 检查迁移状态 ---
        logger.info('\n--- 步骤3: 检查迁移状态 ---')
        if not common.check_migrations():
            return

        # --- 步骤4: 检查角色表 ---
        logger.info('\n--- 步骤4: 检查角色表 ---')
        if not common.check_table_exists('user_role_list'):
            logger.info('❌ 角色表不存在，请先运行迁移命令')
            return
        logger.info('✅ 角色表存在')

        # --- 步骤5: 加载配置 ---
        logger.info('\n--- 步骤5: 加载配置 ---')
        config = common.load_config(config_path)
        if config is None:
            return

        # --with-org 时合并开发环境示例数据（部门/团队）
        # 生产主配置只含账号/角色/权限/配置等固定数据，示例组织数据单独放在 initial_data.dev.yaml
        if with_org:
            if os.path.exists(_DEV_CONFIG):
                dev_config = common.load_config(_DEV_CONFIG)
                if dev_config:
                    config['departments'] = dev_config.get('departments', [])
                    config['teams'] = dev_config.get('teams', [])
                    logger.info('✅ 已合并开发环境示例数据（部门/团队）')
            else:
                logger.info(f'⚠️  开发环境配置不存在: {_DEV_CONFIG}，跳过部门/团队创建')

        # --- 步骤6: 检查系统是否已初始化 ---
        # super_admin 角色存在视为已初始化，避免重复执行覆盖用户已调整数据
        logger.info('\n--- 步骤6: 检查系统是否已初始化 ---')
        from apps.users.models import Role
        if Role.objects.filter(role_key='super_admin').exists():
            if not force:
                # 已初始化 + 非 force：仅增量更新系统配置（新增项创建 + 已有项更新元数据）
                # 不覆盖用户已调整的 value，但同步 label/description/category 等开发者维护的元数据
                logger.info('⚠️ 检测到系统已初始化，仅更新系统配置元数据')
                logger.info('   如需完全重新初始化，请使用 --force 参数')
                system_configs.create_system_configs(config, dry_run, False)
                # 定时任务调度配置：增量补齐缺失项，已存在的保留用户在管理端调整的值
                system_configs.create_schedule_configs(dry_run, False)
                # 模型配置同样走增量：补齐缺失条目，已存在的保留用户在前端调整的值
                models.create_llm_models(config, dry_run, False)
                logger.info('\n' + '=' * 60)
                if dry_run:
                    logger.info('预览完成！运行时不带 --dry-run 参数以实际更新')
                else:
                    logger.info('系统配置更新完成！')
                logger.info('=' * 60)
                return
            else:
                logger.info('⚠️ 检测到系统已初始化，使用 --force 强制重新初始化所有数据')

        # --- 步骤7: 创建数据 ---
        # 顺序：角色 → 权限 → 角色权限映射 → [部门 → 团队] → 用户 → 全局记忆 → 系统配置
        # 部门/团队默认不创建（生产环境无需示例组织数据），开发期间可用 --with-org 开启
        # 系统配置放最后，因为部分配置项可能依赖角色/权限已就绪（如 system_maintainer）
        logger.info('\n--- 步骤7: 创建数据 ---')
        roles.create_roles(config, dry_run)
        permissions.create_permissions(config, dry_run)
        role_permissions.create_role_permissions(config, dry_run)
        if with_org:
            # 仅开发期间 --with-org 时创建示例部门/团队
            departments.create_departments(config, dry_run)
            teams.create_teams(config, dry_run)
        else:
            logger.info('\n⏭️  跳过部门/团队创建（生产默认行为，开发期间可用 --with-org 开启）')
        users.create_users(config, dry_run)
        global_memories.create_global_memories(config, dry_run)
        # 系统配置：--force 时覆盖 value，否则仅创建缺失项 + 更新元数据
        system_configs.create_system_configs(config, dry_run, force)
        # 定时任务调度配置：--force 时重置默认调度时间，否则仅创建缺失项 + 更新元数据
        system_configs.create_schedule_configs(dry_run, force)
        # 模型配置：首次部署创建预置模型，--force 时覆盖已存在项字段
        models.create_llm_models(config, dry_run, force)

        logger.info('\n' + '=' * 60)
        if dry_run:
            logger.info('预览完成！运行时不带 --dry-run 参数以实际创建数据')
        else:
            logger.info('系统初始化完成！')
        logger.info('=' * 60)
