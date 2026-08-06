"""角色-权限映射初始化模块"""
import traceback

from loguru import logger


def create_role_permissions(config, dry_run=False):
    """建立角色与权限的映射关系"""
    logger.info('\n=== 创建角色-权限映射 ===')
    from apps.users.models import Role, Permission, RolePermissionRel
    rp_config = config.get('role_permissions', {})
    created = 0
    skipped = 0

    for role_code, perm_codes in rp_config.items():
        try:
            role = Role.objects.get(role_key=role_code)
        except Role.DoesNotExist:
            logger.info(f'  ❌ 角色 "{role_code}" 不存在，跳过')
            continue

        for perm_code in perm_codes:
            try:
                perm = Permission.objects.get(permission_key=perm_code)
            except Permission.DoesNotExist:
                logger.info(f'  ❌ 权限 "{perm_code}" 不存在，跳过')
                continue

            try:
                if RolePermissionRel.objects.filter(role=role, permission=perm).exists():
                    skipped += 1
                else:
                    if not dry_run:
                        RolePermissionRel.objects.create(role=role, permission=perm)
                    logger.info(f'  ✅ {role_code} -> {perm_code}')
                    created += 1
            except Exception as e:
                logger.info(f'  ❌ 创建角色权限映射失败: {role_code} -> {perm_code}: {e}')
                traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created
