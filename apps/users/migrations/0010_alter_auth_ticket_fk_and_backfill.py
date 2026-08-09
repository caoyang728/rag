"""数据迁移（第二阶段）：三张授权表 ticket FK 改指 unified_ticket + 回填

背景：
- 0008 建统一主表，0009 清空授权表 ticket_id 并迁移存量工单（旧关联暂存
  在 unified.detail['legacy_rel']），本迁移承接收尾：
1. AlterField：user_role_global_rel / dept_scope / team_scope 的 ticket FK
   从 permission_approval_ticket 改指 unified_ticket（此时 ticket_id 全为 NULL，
   FK 重建不会触发引用校验失败）
2. RunPython 回填：按 detail['legacy_rel'] 中记录的 rel pk，把授权表
   ticket_id 重新指向对应 unified_ticket

回滚说明：AlterField 反向自动还原 FK；回填反向为空（授权关联由 0009
数据回滚时一并失效，不额外处理）。
"""
import django.db.models.deletion
from django.db import migrations, models


def backfill_auth_rel_tickets(apps, schema_editor):
    """按 unified.detail['legacy_rel'] 回填三张授权表的 ticket_id

    legacy_rel 结构：{rel_model_key: [rel_pk, ...]}（global/dept/team）。
    仅处理 permission 工单；无 legacy_rel 的工单（存量无授权关联）直接跳过。
    """
    UnifiedTicket = apps.get_model('users', 'UnifiedTicket')
    rel_models = {
        'global': apps.get_model('users', 'UserRoleRel'),
        'dept': apps.get_model('users', 'UserDeptScopeRel'),
        'team': apps.get_model('users', 'UserTeamScopeRel'),
    }
    for t in UnifiedTicket.objects.filter(biz_type='permission').iterator():
        detail = t.detail if isinstance(t.detail, dict) else {}
        rel_map = detail.get('legacy_rel') or {}
        for key, pks in rel_map.items():
            model = rel_models.get(key)
            if not model or not pks:
                continue
            model.objects.filter(pk__in=pks).update(ticket_id=t.pk)


def noop_reverse(apps, schema_editor):
    """反向无操作：授权关联在回滚 0009 时随数据清理，无需特殊处理"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0009_migrate_tickets_to_unified'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userrolerel',
            name='ticket',
            field=models.ForeignKey('UnifiedTicket', blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='user_role_rels',
                                    help_text='关联审批工单（有则填）'),
        ),
        migrations.AlterField(
            model_name='userdeptscoperel',
            name='ticket',
            field=models.ForeignKey('UnifiedTicket', blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='dept_scope_rels'),
        ),
        migrations.AlterField(
            model_name='userteamscoperel',
            name='ticket',
            field=models.ForeignKey('UnifiedTicket', blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='team_scope_rels'),
        ),
        migrations.RunPython(backfill_auth_rel_tickets, noop_reverse),
    ]
