"""初始用户初始化模块"""
import traceback

from loguru import logger


def create_users(config, dry_run=False):
    """创建初始超级管理员账号

    通过 UserRoleRel 绑定角色，需显式指定 status=ACTIVE
    """
    logger.info('\n=== 创建用户 ===')
    from apps.users.models import User, Role, UserRoleRel, GrantStatus
    users_config = config.get('users', [])
    created = 0
    skipped = 0

    for user_data in users_config:
        username = user_data['username']
        email = user_data['email']
        real_name = user_data.get('real_name', '')
        password = user_data['password']

        try:
            if User.objects.filter(username=username).exists():
                logger.info(f'用户 "{username}" 已存在，跳过')
                skipped += 1
                continue

            if not dry_run:
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password
                )
                user.real_name = real_name
                user.save()

                role_code = user_data.get('role')
                if role_code:
                    try:
                        role = Role.objects.get(role_key=role_code)
                        UserRoleRel.objects.get_or_create(
                            user=user, role=role,
                            defaults={'status': GrantStatus.ACTIVE},
                        )
                    except Role.DoesNotExist:
                        logger.info(f'  ⚠️  角色 "{role_code}" 不存在，用户 "{username}" 将没有角色')

            logger.info(f'  ✅ 创建用户: {username} ({real_name})')
            created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建用户 "{username}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created
