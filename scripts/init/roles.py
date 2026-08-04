"""角色初始化模块"""
import traceback

from loguru import logger


def create_roles(config, dry_run=False):
    """创建内置角色

    YAML 中的 code 作为 role_key；role_type/data_scope 决定授权时是否需绑定 Scope
    """
    logger.info('\n=== 创建角色 ===')
    from apps.users.models import Role
    roles_config = config.get('roles', [])
    created = 0
    skipped = 0

    for role_data in roles_config:
        role_key = role_data['code']
        name = role_data['name']
        description = role_data.get('description', '')
        is_builtin = role_data.get('is_builtin', False)
        role_type = role_data.get('role_type', 'NORMAL_USER')
        data_scope = role_data.get('data_scope', 'TEAM')

        try:
            if Role.objects.filter(role_key=role_key).exists():
                logger.info(f'  ⏭️  角色 "{role_key}" 已存在，跳过')
                skipped += 1
            else:
                if not dry_run:
                    Role.objects.create(
                        role_key=role_key,
                        name=name,
                        description=description,
                        is_builtin=is_builtin,
                        role_type=role_type,
                        data_scope=data_scope,
                    )
                logger.info(f'  ✅ 创建角色: {role_key} - {name}')
                created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建角色 "{role_key}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created
