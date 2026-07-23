#!/usr/bin/env python3
"""
系统初始化脚本
首次部署时运行，创建必要的账号、角色、权限、部门等基础数据

使用方法：
    python scripts/init_system.py
    python scripts/init_system.py --config scripts/initial_data.yaml
    python scripts/init_system.py --dry-run

参数：
    --config: 指定配置文件路径（默认: scripts/initial_data.yaml）
    --dry-run: 仅预览，不实际写入数据库
    --force: 强制覆盖已存在的数据
"""

import os
import sys
import argparse
import yaml
import traceback
import socket
import time

from loguru import logger

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

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


def test_db_connection():
    import psycopg
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        host = os.getenv('PG_DB_HOST', 'localhost')
        port = int(os.getenv('PG_DB_PORT', '5432'))
        db = os.getenv('PG_DB_DATABASE', 'rag_agent')
        user = os.getenv('PG_DB_USER', 'rag_user')
        password = os.getenv('PG_DB_PASSWORD', 'rag_pass_2026')
        ssl_mode = os.getenv('PG_SSL_MODE', 'prefer')
    else:
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        host = parsed.hostname
        port = parsed.port or 5432
        db = parsed.path.lstrip('/')
        user = parsed.username
        password = parsed.password
        ssl_mode = 'prefer'

    logger.info(f'🔍 测试数据库连接: {user}@{host}:{port}/{db}')
    try:
        conn = psycopg.connect(
            host=host,
            port=port,
            dbname=db,
            user=user,
            password=password,
            sslmode=ssl_mode,
            connect_timeout=10
        )
        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        result = cursor.fetchone()
        logger.info(f'✅ 数据库连接成功: {result[0].split()[0]}')
        conn.close()
        return True
    except psycopg.Error as e:
        logger.info(f'❌ 数据库连接失败: {e}')
        return False
    except Exception as e:
        logger.info(f'❌ 连接测试异常: {e}')
        return False


def setup_django():
    try:
        import django
        django.setup()
        logger.info('✅ Django 初始化成功')
        return True
    except Exception as e:
        logger.info(f'❌ Django 初始化失败: {e}')
        traceback.print_exc()
        return False


def check_migrations():
    from django.core.management import call_command
    try:
        from django.db import connection
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app='users';")
        count = cursor.fetchone()[0]
        if count == 0:
            logger.info('❌ 用户模块迁移未执行，请先运行:')
            logger.info('   python manage.py makemigrations')
            logger.info('   python manage.py migrate')
            return False
        logger.info(f'✅ 用户模块迁移已执行（{count} 条记录）')
        return True
    except Exception as e:
        logger.info(f'⚠️  检查迁移状态失败: {e}')
        return False


def check_table_exists(table_name):
    from django.db import connection
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = %s);",
            [table_name]
        )
        exists = cursor.fetchone()[0]
        return exists
    except Exception as e:
        logger.info(f'❌ 检查表 "{table_name}" 失败: {e}')
        return False


def import_models():
    try:
        from apps.users.models import SysUser, Role, Permission, RolePermission, UserRole, Department, Team
        logger.info('✅ 模型导入成功')
        return SysUser, Role, Permission, RolePermission, UserRole, Department, Team
    except Exception as e:
        logger.info(f'❌ 模型导入失败: {e}')
        traceback.print_exc()
        return None


def load_config(config_path):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.info(f'❌ 配置文件加载失败: {e}')
        traceback.print_exc()
        return None


def create_roles(config, dry_run=False):
    logger.info('\n=== 创建角色 ===')
    from apps.users.models import Role
    roles_config = config.get('roles', [])
    created = 0
    skipped = 0

    for role_data in roles_config:
        code = role_data['code']
        name = role_data['name']
        description = role_data.get('description', '')
        is_builtin = role_data.get('is_builtin', False)

        try:
            if Role.objects.filter(code=code).exists():
                logger.info(f'  ⏭️  角色 "{code}" 已存在，跳过')
                skipped += 1
            else:
                if not dry_run:
                    Role.objects.create(
                        code=code,
                        name=name,
                        description=description,
                        is_builtin=is_builtin
                    )
                logger.info(f'  ✅ 创建角色: {code} - {name}')
                created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建角色 "{code}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created


def create_permissions(config, dry_run=False):
    logger.info('\n=== 创建权限 ===')
    from apps.users.models import Permission
    perms_config = config.get('permissions', [])
    created = 0
    skipped = 0

    for perm_data in perms_config:
        code = perm_data['code']
        name = perm_data['name']
        module = perm_data.get('module', '')
        action = perm_data.get('action', 'read')
        scope = perm_data.get('scope', 'personal')
        description = perm_data.get('description', '')

        try:
            if Permission.objects.filter(code=code).exists():
                logger.info(f'  ⏭️  权限 "{code}" 已存在，跳过')
                skipped += 1
            else:
                if not dry_run:
                    Permission.objects.create(
                        code=code,
                        name=name,
                        module=module,
                        action=action,
                        scope=scope,
                        description=description
                    )
                logger.info(f'  ✅ 创建权限: {code}')
                created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建权限 "{code}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created


def create_role_permissions(config, dry_run=False):
    logger.info('\n=== 创建角色-权限映射 ===')
    from apps.users.models import Role, Permission, RolePermission
    rp_config = config.get('role_permissions', {})
    created = 0
    skipped = 0

    for role_code, perm_codes in rp_config.items():
        try:
            role = Role.objects.get(code=role_code)
        except Role.DoesNotExist:
            logger.info(f'  ❌ 角色 "{role_code}" 不存在，跳过')
            continue

        for perm_code in perm_codes:
            try:
                perm = Permission.objects.get(code=perm_code)
            except Permission.DoesNotExist:
                logger.info(f'  ❌ 权限 "{perm_code}" 不存在，跳过')
                continue

            try:
                if RolePermission.objects.filter(role=role, permission=perm).exists():
                    skipped += 1
                else:
                    if not dry_run:
                        RolePermission.objects.create(role=role, permission=perm)
                    logger.info(f'  ✅ {role_code} -> {perm_code}')
                    created += 1
            except Exception as e:
                logger.info(f'  ❌ 创建角色权限映射失败: {role_code} -> {perm_code}: {e}')
                traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created


def create_departments(config, dry_run=False):
    logger.info('\n=== 创建部门 ===')
    from apps.users.models import Department
    depts_config = config.get('departments', [])
    created = 0
    skipped = 0

    def create_dept_tree(depts, parent=None):
        nonlocal created, skipped
        for dept_data in depts:
            code = dept_data['code']
            name = dept_data['name']
            sort_order = dept_data.get('sort_order', 0)
            children = dept_data.get('children', [])

            try:
                if Department.objects.filter(code=code).exists():
                    logger.info(f'  ⏭️  部门 "{code}" 已存在，跳过')
                    skipped += 1
                    dept = Department.objects.get(code=code)
                else:
                    if not dry_run:
                        dept = Department.objects.create(
                            name=name,
                            code=code,
                            parent=parent,
                            sort_order=sort_order
                        )
                    logger.info(f'  ✅ 创建部门: {code} - {name}')
                    created += 1

                if children:
                    create_dept_tree(children, parent=dept)
            except Exception as e:
                logger.info(f'  ❌ 创建部门 "{code}" 失败: {e}')
                traceback.print_exc()

    create_dept_tree(depts_config)
    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created


def create_teams(config, dry_run=False):
    logger.info('\n=== 创建团队 ===')
    from apps.users.models import Team, Department
    teams_config = config.get('teams', [])
    created = 0
    skipped = 0

    for team_data in teams_config:
        code = team_data['code']
        name = team_data['name']
        description = team_data.get('description', '')
        dept_name = team_data.get('department')

        try:
            if Team.objects.filter(code=code).exists():
                logger.info(f'  ⏭️  团队 "{code}" 已存在，跳过')
                skipped += 1
                continue

            department = None
            if dept_name:
                try:
                    department = Department.objects.get(name=dept_name)
                except Department.DoesNotExist:
                    logger.info(f'  ⚠️  团队 "{code}" 的部门 "{dept_name}" 不存在，将不设置部门')

            if not dry_run:
                Team.objects.create(
                    name=name,
                    code=code,
                    description=description,
                    department=department
                )
            logger.info(f'  ✅ 创建团队: {code} - {name}')
            created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建团队 "{code}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created


def create_users(config, dry_run=False):
    logger.info('\n=== 创建用户 ===')
    from apps.users.models import SysUser, Role, UserRole
    users_config = config.get('users', [])
    created = 0
    skipped = 0

    for user_data in users_config:
        username = user_data['username']
        email = user_data['email']
        real_name = user_data.get('real_name', '')
        password = user_data['password']

        try:
            if SysUser.objects.filter(username=username).exists():
                logger.info(f'  ⏭️  用户 "{username}" 已存在，跳过')
                skipped += 1
                continue

            if not dry_run:
                user = SysUser.objects.create_user(
                    username=username,
                    email=email,
                    password=password
                )
                user.real_name = real_name
                user.save()

                # 处理角色分配
                role_code = user_data.get('role')
                if role_code:
                    try:
                        role = Role.objects.get(code=role_code)
                        UserRole.objects.get_or_create(user=user, role=role)
                    except Role.DoesNotExist:
                        logger.info(f'  ⚠️  角色 "{role_code}" 不存在，用户 "{username}" 将没有角色')

            logger.info(f'  ✅ 创建用户: {username} ({real_name})')
            created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建用户 "{username}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created


def main():
    parser = argparse.ArgumentParser(description='系统初始化脚本')
    parser.add_argument('--config', default='scripts/initial_data.yaml', help='配置文件路径')
    parser.add_argument('--dry-run', action='store_true', help='仅预览，不实际写入数据库')
    parser.add_argument('--force', action='store_true', help='强制覆盖已存在的数据')
    args = parser.parse_args()

    config_path = os.path.join(PROJECT_ROOT, args.config)
    if not os.path.exists(config_path):
        logger.info(f'❌ 配置文件不存在: {config_path}')
        sys.exit(1)

    logger.info('=' * 60)
    logger.info('RAG-Agent 系统初始化')
    logger.info('=' * 60)
    logger.info(f'配置文件: {config_path}')
    logger.info(f'模式: {"预览模式 (Dry Run)" if args.dry_run else "实际写入模式"}')
    logger.info('=' * 60)

    logger.info('\n--- 步骤1: 测试数据库连接 ---')
    if not test_db_connection():
        sys.exit(1)

    logger.info('\n--- 步骤2: 初始化 Django ---')
    if not setup_django():
        sys.exit(1)

    logger.info('\n--- 步骤3: 检查迁移状态 ---')
    if not check_migrations():
        sys.exit(1)

    logger.info('\n--- 步骤4: 检查角色表 ---')
    if not check_table_exists('system_role_list'):
        logger.info('❌ 角色表不存在，请先运行迁移命令')
        sys.exit(1)
    logger.info('✅ 角色表存在')

    logger.info('\n--- 步骤5: 加载配置 ---')
    config = load_config(config_path)
    if config is None:
        sys.exit(1)

    logger.info('\n--- 步骤6: 检查系统是否已初始化 ---')
    from apps.users.models import Role
    if Role.objects.filter(code='super_admin').exists():
        logger.info('❌ 检测到 super_admin 角色已存在，系统可能已初始化')
        logger.info('   如果需要重新初始化，请先清空数据库或使用 --force 参数')
        if not args.force:
            logger.info('   终止执行')
            sys.exit(1)
        else:
            logger.info('   使用 --force 参数强制继续')

    logger.info('\n--- 步骤7: 创建数据 ---')
    create_roles(config, args.dry_run)
    create_permissions(config, args.dry_run)
    create_role_permissions(config, args.dry_run)
    create_departments(config, args.dry_run)
    create_teams(config, args.dry_run)
    create_users(config, args.dry_run)

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
