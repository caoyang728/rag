#!/usr/bin/env python3
"""
系统初始化脚本（总入口）
首次部署时运行，创建必要的账号、角色、权限、部门、系统配置等基础数据

各数据类型的初始化逻辑已按类型拆分到 scripts/init/ 目录下：
- init/common.py           公共工具（DB 连接 / Django 启动 / 迁移检查 / yaml 加载）
- init/roles.py            角色初始化
- init/permissions.py      权限点初始化
- init/role_permissions.py 角色-权限映射
- init/departments.py      部门初始化
- init/teams.py            团队初始化
- init/users.py            初始用户
- init/global_memories.py  全局记忆
- init/system_configs.py   系统配置（KV，从 .env 迁移而来）
- init/models.py           模型配置（LLM/Embedding/Rerank）

本文件仅做流程编排，不包含具体业务逻辑。

使用方法：
    python scripts/init_system.py
    python scripts/init_system.py --config scripts/initial_data.yaml
    python scripts/init_system.py --dry-run
    python scripts/init_system.py --force   # 强制覆盖已存在的配置项 value
    python scripts/init_system.py --with-org  # 同时创建示例部门/团队（开发期间使用）

参数：
    --config: 指定配置文件路径（默认: scripts/initial_data.yaml）
    --dry-run: 仅预览，不实际写入数据库
    --force: 强制覆盖已存在的数据（含 SystemConfig 的 value）
    --with-org: 同时创建示例部门/团队（默认不创建，生产环境无需示例组织数据）
"""

import os
import sys
import argparse
import traceback

from loguru import logger

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 将 scripts/ 目录加入 path，使其下的 init/ 子包可被导入
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(PROJECT_ROOT, '.env')
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path)
        logger.info(f'✅ 已加载环境变量文件: {dotenv_path}')
    else:
        logger.info(f'⚠️  环境变量文件不存在: {dotenv_path}')
except ImportError:
    logger.info('⚠️  python-dotenv 未安装，环境变量将从系统读取')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')

# 导入各初始化模块（此时 django 尚未 setup，模块内函数体不会触发 ORM 调用）
from init import (
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


def main():
    parser = argparse.ArgumentParser(description='系统初始化脚本')
    parser.add_argument('--config', default='scripts/initial_data.yaml', help='配置文件路径')
    parser.add_argument('--dry-run', action='store_true', help='仅预览，不实际写入数据库')
    parser.add_argument('--force', action='store_true', help='强制覆盖已存在的数据（含 SystemConfig value）')
    parser.add_argument('--with-org', action='store_true',
                        help='同时创建示例部门/团队（默认不创建，生产环境无需示例组织数据）')
    args = parser.parse_args()

    config_path = os.path.join(PROJECT_ROOT, args.config)
    if not os.path.exists(config_path):
        logger.info(f'❌ 配置文件不存在: {config_path}')
        sys.exit(1)

    logger.info('=' * 60)
    logger.info('RAG-Agent 系统初始化')
    logger.info('=' * 60)
    logger.info(f'配置文件: {config_path}')
    logger.info(f'模式: {"预览模式 (Dry Run)" if args.dry_run else "实际写入模式"}'
                f'{" (Force)" if args.force else ""}')
    logger.info('=' * 60)

    # --- 步骤1: 测试数据库连接 ---
    # settings 未就绪前用 psycopg 直连，避免 ORM 依赖未初始化时报错
    logger.info('\n--- 步骤1: 测试数据库连接 ---')
    if not common.test_db_connection():
        sys.exit(1)

    # --- 步骤2: 初始化 Django ---
    logger.info('\n--- 步骤2: 初始化 Django ---')
    if not common.setup_django():
        sys.exit(1)

    # --- 步骤3: 检查迁移状态 ---
    logger.info('\n--- 步骤3: 检查迁移状态 ---')
    if not common.check_migrations():
        sys.exit(1)

    # --- 步骤4: 检查角色表 ---
    logger.info('\n--- 步骤4: 检查角色表 ---')
    if not common.check_table_exists('user_role_list'):
        logger.info('❌ 角色表不存在，请先运行迁移命令')
        sys.exit(1)
    logger.info('✅ 角色表存在')

    # --- 步骤5: 加载配置 ---
    logger.info('\n--- 步骤5: 加载配置 ---')
    config = common.load_config(config_path)
    if config is None:
        sys.exit(1)

    # --- 步骤6: 检查系统是否已初始化 ---
    # super_admin 角色存在视为已初始化，避免重复执行覆盖用户已调整数据
    logger.info('\n--- 步骤6: 检查系统是否已初始化 ---')
    from apps.users.models import Role
    if Role.objects.filter(role_key='super_admin').exists():
        if not args.force:
            # 已初始化 + 非 force：仅增量更新系统配置（新增项创建 + 已有项更新元数据）
            # 不覆盖用户已调整的 value，但同步 label/description/category 等开发者维护的元数据
            logger.info('⚠️ 检测到系统已初始化，仅更新系统配置元数据')
            logger.info('   如需完全重新初始化，请使用 --force 参数')
            system_configs.create_system_configs(config, args.dry_run, False)
            # 模型配置同样走增量：补齐缺失条目，已存在的保留用户在前端调整的值
            models.create_llm_models(config, args.dry_run, False)
            logger.info('\n' + '=' * 60)
            if args.dry_run:
                logger.info('预览完成！运行时不带 --dry-run 参数以实际更新')
            else:
                logger.info('系统配置更新完成！')
            logger.info('=' * 60)
            sys.exit(0)
        else:
            logger.info('⚠️ 检测到系统已初始化，使用 --force 强制重新初始化所有数据')

    # --- 步骤7: 创建数据 ---
    # 顺序：角色 → 权限 → 角色权限映射 → [部门 → 团队] → 用户 → 全局记忆 → 系统配置
    # 部门/团队默认不创建（生产环境无需示例组织数据），开发期间可用 --with-org 开启
    # 系统配置放最后，因为部分配置项可能依赖角色/权限已就绪（如 system_maintainer）
    logger.info('\n--- 步骤7: 创建数据 ---')
    roles.create_roles(config, args.dry_run)
    permissions.create_permissions(config, args.dry_run)
    role_permissions.create_role_permissions(config, args.dry_run)
    if args.with_org:
        # 仅开发期间 --with-org 时创建示例部门/团队
        departments.create_departments(config, args.dry_run)
        teams.create_teams(config, args.dry_run)
    else:
        logger.info('\n⏭️  跳过部门/团队创建（生产默认行为，开发期间可用 --with-org 开启）')
    users.create_users(config, args.dry_run)
    global_memories.create_global_memories(config, args.dry_run)
    # 系统配置：--force 时覆盖 value，否则仅创建缺失项 + 更新元数据
    system_configs.create_system_configs(config, args.dry_run, args.force)
    # 模型配置：首次部署创建预置模型，--force 时覆盖已存在项字段
    models.create_llm_models(config, args.dry_run, args.force)

    logger.info('\n' + '=' * 60)
    if args.dry_run:
        logger.info('预览完成！运行时不带 --dry-run 参数以实际创建数据')
    else:
        logger.info('系统初始化完成！')
    logger.info('=' * 60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info('\n\n⏹️  用户中断操作')
        sys.exit(0)
    except Exception as e:
        logger.info(f'\n\n❌ 初始化脚本异常终止: {e}')
        traceback.print_exc()
        sys.exit(1)
