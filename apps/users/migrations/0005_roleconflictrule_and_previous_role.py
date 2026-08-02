# Generated migration: 角色互斥规则表 + 工单旧角色字段 + 初始互斥数据
# 业务背景:
# - 新增 RoleConflictRule 表实现 SoD(职责分离)约束
# - PermissionApprovalTicket 加 previous_role 字段支持 ROLE_CHANGE 工单
# - 写入 4 高权全局角色两两互斥的初始规则(6 条)

from django.db import migrations, models
import django.db.models.deletion


def _role_pk(apps, role_key):
    """通过 role_key 反查角色 PK,避免依赖 ID 顺序"""
    Role = apps.get_model('users', 'Role')
    role = Role.objects.filter(role_key=role_key).first()
    return role.id if role else None


def init_conflict_rules(apps, schema_editor):
    """写入 4 高权全局角色两两互斥初始规则

    组合: user_admin / kb_admin / compliance_admin / super_admin 两两互斥,共 C(4,2)=6 条。
    幂等:重复运行不会重复插入(unique_together 拦截)。
    """
    RoleConflictRule = apps.get_model('users', 'RoleConflictRule')
    Role = apps.get_model('users', 'Role')

    keys = ['user_admin', 'kb_admin', 'compliance_admin', 'super_admin']
    role_pks = {k: _role_pk(apps, k) for k in keys}
    # 任一角色缺失(尚未初始化)则跳过,等下次迁移或 init_system 再补
    if any(v is None for v in role_pks.values()):
        return

    # 两两组合(a < b 顺序写入,避免重复)
    for i, ka in enumerate(keys):
        for kb in keys[i + 1:]:
            RoleConflictRule.objects.get_or_create(
                role_a_id=role_pks[ka],
                role_b_id=role_pks[kb],
                defaults={'reason': f'{ka} × {kb} 互斥(4 高权全局角色 4 选 1)'},
            )


def remove_conflict_rules(apps, schema_editor):
    """回滚:清空初始互斥规则"""
    RoleConflictRule = apps.get_model('users', 'RoleConflictRule')
    Role = apps.get_model('users', 'Role')
    keys = ['user_admin', 'kb_admin', 'compliance_admin', 'super_admin']
    role_pks = [r.id for r in Role.objects.filter(role_key__in=keys)]
    if not role_pks:
        return
    RoleConflictRule.objects.filter(
        role_a_id__in=role_pks, role_b_id__in=role_pks,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0004_remove_user_unique_email_active_alter_user_email'),
    ]

    operations = [
        # 1. 新增 PermissionApprovalTicket.previous_role 字段
        migrations.AddField(
            model_name='permissionapprovalticket',
            name='previous_role',
            field=models.ForeignKey(
                blank=True,
                help_text='角色变更工单的旧角色（仅 ROLE_CHANGE，撤销目标',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='users.role',
            ),
        ),
        # 2. 调整 change_type choices(加 ROLE_CHANGE)
        migrations.AlterField(
            model_name='permissionapprovalticket',
            name='change_type',
            field=models.CharField(
                choices=[
                    ('GRANT', '授权'),
                    ('REVOKE', '撤销'),
                    ('SCOPE_CHANGE', '范围变更'),
                    ('EXPIRE_EXTEND', '延期'),
                    ('ROLE_CHANGE', '角色变更（同 scope 内升级/降级/平移，原子撤销旧角色+授予新角色）'),
                ],
                help_text='GRANT/REVOKE/SCOPE_CHANGE/EXPIRE_EXTEND/ROLE_CHANGE',
                max_length=16,
            ),
        ),
        # 3. 新增 RoleConflictRule 表
        migrations.CreateModel(
            name='RoleConflictRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reason', models.CharField(blank=True, default='', help_text='互斥原因（可选，便于审计回溯）', max_length=128)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('role_a', models.ForeignKey(help_text='互斥角色 A', on_delete=django.db.models.deletion.CASCADE, related_name='+', to='users.role')),
                ('role_b', models.ForeignKey(help_text='互斥角色 B', on_delete=django.db.models.deletion.CASCADE, related_name='+', to='users.role')),
            ],
            options={
                'verbose_name': '角色互斥规则',
                'db_table': 'role_conflict_rule',
                'unique_together': {('role_a', 'role_b')},
                'indexes': [
                    models.Index(fields=['role_a'], name='role_conf_role_a_idx'),
                    models.Index(fields=['role_b'], name='role_conf_role_b_idx'),
                ],
            },
        ),
        # 4. 写入初始互斥规则数据
        migrations.RunPython(init_conflict_rules, remove_conflict_rules),
    ]
