"""RBAC（角色-权限）业务逻辑：角色权限全量覆盖分配"""
from django.db import transaction
from django.utils import timezone
from loguru import logger

from apps.users.models import Permission, RolePermissionRel


def assign_permissions_to_role(role, permission_ids, actor):
    """全量覆盖角色权限：事务内撤销未保留项 + 批量写入新项

    permission_ids 为调用方已清洗的合法权限ID列表。
    返回 (valid_ids, invalid_count)：valid_ids 为实际写入的权限ID，
    invalid_count 为参数中存在但库中不存在的权限数（用于响应提示）。
    """
    # 批量校验权限是否存在，只保留有效的
    existing_ids = set(Permission.objects.filter(id__in=permission_ids).values_list("id", flat=True))
    valid_ids = [pid for pid in permission_ids if pid in existing_ids]
    invalid_count = len(permission_ids) - len(valid_ids)

    with transaction.atomic():
        RolePermissionRel.objects.filter(
            role=role,
            is_active=True,
        ).exclude(permission_id__in=valid_ids).update(
            is_active=False,
            revoked_at=timezone.now(),
            revoked_by=actor
        )

        if valid_ids:
            objs = [RolePermissionRel(
                role=role,
                permission_id=pid,
                granted_by=actor
            ) for pid in valid_ids]
            RolePermissionRel.objects.bulk_create(
                objs,
                update_conflicts=True,
                update_fields=['is_active', 'revoked_at', 'revoked_by'],
                unique_fields=['role_id', 'permission_id']
            )
    logger.info(f"Role.assign_permissions - user: {actor.username}, role: {role.role_key}, count: {len(valid_ids)}")
    return valid_ids, invalid_count
