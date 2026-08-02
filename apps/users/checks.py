"""apps.users.checks —— Django 系统检查(部署期硬约束)

启动时检查关键约束,不达标抛 Warning(不阻断启动,但日志/控制台可见)。

当前检查:
- super_admin_count:活跃超管数量 < 2 时抛 Warning
  业务背景:超管双审要求两个不同的超管顺序审批,不足 2 人会导致超管工单无法流转。
  生产环境必须 ≥2 超管,部署期通过此 check 提示运维补人。
"""
from django.core.checks import Warning, register

from apps.users.models import UserRoleRel, GrantStatus


@register()
def check_super_admin_count(app_configs, **kwargs):
    """检查活跃超管数量 ≥ 2(超管双审硬约束)

    阈值说明:
    - 0 人:系统无法运作,任何超管操作都进不去 → 抛 Warning(严重)
    - 1 人:超管工单无法双审(自审被禁) → 抛 Warning(高)
    - ≥2 人:正常
    """
    errors = []
    try:
        count = UserRoleRel.objects.filter(
            role__role_key='super_admin',
            status=GrantStatus.ACTIVE,
        ).values_list('user_id', flat=True).distinct().count()
    except Exception as e:
        # 表未迁移/数据库未就绪时跳过(开发期首次 migrate 会触发)
        return errors

    if count == 0:
        errors.append(Warning(
            '系统当前没有活跃的超级管理员,所有超管操作将无法执行。'
            '请立即通过 create_superuser 创建至少 2 个超管账号。',
            id='users.E001_no_super_admin',
        ))
    elif count == 1:
        errors.append(Warning(
            f'系统当前只有 {count} 个活跃超级管理员,超管工单双审将无法流转'
            f'(申请人不能审自己工单 + 双人独立性约束)。'
            f'请立即指派第二个超级管理员。',
            id='users.E002_insufficient_super_admin',
        ))
    return errors
