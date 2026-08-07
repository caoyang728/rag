"""数据迁移：将 ConfigChangeTicket + ModelChangeTicket 数据迁移到统一 Ticket 表"""
from django.db import migrations


def migrate_forward(apps, schema_editor):
    ConfigChangeTicket = apps.get_model('system', 'ConfigChangeTicket')
    ModelChangeTicket = apps.get_model('system', 'ModelChangeTicket')
    Ticket = apps.get_model('system', 'Ticket')

    # 状态映射：first_approved → pending_review
    STATUS_MAP = {
        'pending': 'pending',
        'first_approved': 'pending_review',
        'approved': 'approved',
        'rejected': 'rejected',
        'withdrawn': 'withdrawn',
    }

    SCHEDULE_PREFIX = 'llm_scheduler:'

    # 迁移配置工单（含定时任务）
    for t in ConfigChangeTicket.objects.all().iterator():
        is_schedule = (t.config_key or '').startswith(SCHEDULE_PREFIX)
        Ticket.objects.create(
            ticket_type='schedule' if is_schedule else 'config',
            operation='modify',
            status=STATUS_MAP.get(t.status, t.status),
            risk_level=t.risk_level,
            reason=t.reason or '',
            creator_id=t.creator_id,
            auditor_id=t.reviewer_id,          # reviewer → auditor
            reviewer_id=t.super_admin_reviewer_id,  # super_admin_reviewer → reviewer
            audit_comment=t.review_comment or '',
            review_comment=t.super_admin_comment or '',
            created_at=t.created_at,
            audited_at=t.reviewed_at,          # reviewed_at → audited_at
            reviewed_at=t.super_admin_reviewed_at,  # super_admin_reviewed_at → reviewed_at
            applied_at=t.applied_at,
            config_key=t.config_key,
            target_model_id=None,
            detail={
                'config_label': t.config_label or '',
                'old_value': t.old_value or '',
                'new_value': t.new_value or '',
                'change_summary': t.change_summary or '',
                '_old_table': 'system_config_ticket',
                '_old_id': t.id,
            },
        )

    # 迁移模型工单
    for t in ModelChangeTicket.objects.all().iterator():
        # target_model_snapshot 中提取 model_id 备用
        snapshot = t.target_model_snapshot or {}
        target_id = t.target_model_id or snapshot.get('id')
        Ticket.objects.create(
            ticket_type='model',
            operation=t.operation,
            status=STATUS_MAP.get(t.status, t.status),
            risk_level=t.risk_level,
            reason=t.reason or '',
            creator_id=t.creator_id,
            auditor_id=t.reviewer_id,
            reviewer_id=t.super_admin_reviewer_id,
            audit_comment=t.review_comment or '',
            review_comment=t.super_admin_comment or '',
            created_at=t.created_at,
            audited_at=t.reviewed_at,
            reviewed_at=t.super_admin_reviewed_at,
            applied_at=t.applied_at,
            config_key=None,
            target_model_id=target_id,
            detail={
                'target_model_snapshot': snapshot,
                'changed_fields': t.changed_fields or {},
                'dependency_refs': t.dependency_refs or [],
                '_old_table': 'system_model_ticket',
                '_old_id': t.id,
            },
        )


def migrate_backward(apps, schema_editor):
    """反向迁移：清空 Ticket 表（旧表数据未动，可随时回滚）"""
    Ticket = apps.get_model('system', 'Ticket')
    Ticket.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ('system', '0012_ticket_unified'),
    ]

    operations = [
        migrations.RunPython(migrate_forward, migrate_backward),
    ]
