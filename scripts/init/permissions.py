"""权限点初始化模块"""
import traceback

from loguru import logger


def create_permissions(config, dry_run=False):
    """创建内置权限点（permission_key 三段式：module.resource.action）"""
    logger.info('\n=== 创建权限 ===')
    from apps.users.models import Permission
    perms_config = config.get('permissions', [])
    created = 0
    skipped = 0

    for perm_data in perms_config:
        perm_key = perm_data['code']
        perm_name = perm_data['name']
        module = perm_data.get('module', '')
        description = perm_data.get('description', '')
        is_builtin = perm_data.get('is_builtin', False)

        try:
            if Permission.objects.filter(permission_key=perm_key).exists():
                logger.info(f'  ⏭️  权限 "{perm_key}" 已存在，跳过')
                skipped += 1
            else:
                if not dry_run:
                    Permission.objects.create(
                        permission_key=perm_key,
                        permission_name=perm_name,
                        module=module,
                        description=description,
                        is_builtin=is_builtin,
                    )
                logger.info(f'  ✅ 创建权限: {perm_key}')
                created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建权限 "{perm_key}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created
