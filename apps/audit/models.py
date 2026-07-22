"""
audit app - 审计日志 Model（含 sha256 哈希链）
对齐数据库设计 F1
sha256 哈希链（prev_hash + row_hash）保证审计日志不可篡改
    - 每条日志的 row_hash = sha256(prev_hash + 关键字段 canonical json)
    - 后续验证：读全表按 id 排序，逐行重算，比对即可发现被篡改
"""
import hashlib
import json

from django.db import models
from django.contrib.postgres.fields import ArrayField


class AuditLog(models.Model):
    """F1 audit_log - 审计日志（哈希链）"""

    ACTION_CATEGORY_CHOICES = [
        ('auth', 'auth'),
        ('user', 'user'),
        ('rbac', 'rbac'),
        ('node', 'node'),
        ('document', 'document'),
        ('chat', 'chat'),
        ('config', 'config'),
        ('export', 'export'),
        ('security', 'security'),
        ('system', 'system'),
    ]
    RESULT_CHOICES = [
        ('success', 'success'),
        ('failed', 'failed'),
        ('denied', 'denied'),
    ]

    id = models.BigAutoField(primary_key=True)
    actor = models.ForeignKey('users.SysUser', on_delete=models.SET_NULL, null=True, blank=True,
                              db_column='actor_id', related_name='audit_logs')
    actor_username = models.CharField(max_length=64, blank=True, default='',
                                       help_text='冗余：账号变名/删除时保留')
    action = models.CharField(max_length=64, help_text='动作：login/upload_doc/delete_user...')
    action_category = models.CharField(max_length=16, choices=ACTION_CATEGORY_CHOICES,
                                        default='system')
    target_type = models.CharField(max_length=32, blank=True, default='',
                                    help_text='被操作对象类型 document/user/...')
    target_id = models.CharField(max_length=64, blank=True, default='')
    result = models.CharField(max_length=16, choices=RESULT_CHOICES, default='success')

    ip_address = models.CharField(max_length=64, blank=True, default='')
    user_agent = models.CharField(max_length=256, blank=True, default='')
    method = models.CharField(max_length=8, blank=True, default='')
    path = models.CharField(max_length=256, blank=True, default='')

    detail = models.JSONField(default=dict, blank=True,
                              help_text='详情快照，如 diff、error_message')

    # ⭐ 哈希链核心字段
    prev_hash = models.CharField(max_length=64, blank=True, default='',
                                  help_text='上一条 row_hash；首条为空串')
    row_hash = models.CharField(max_length=64, blank=True, default='',
                                 help_text='本条 sha256')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'audit_log'
        indexes = [
            models.Index(fields=['-created_at'], name='idx_al_time'),
            models.Index(fields=['actor', '-created_at'], name='idx_al_actor_time'),
            models.Index(fields=['action_category'], name='idx_al_category'),
            models.Index(fields=['target_type', 'target_id'], name='idx_al_target'),
            models.Index(fields=['action'], name='idx_al_action'),
        ]

    def _build_payload(self) -> str:
        """构造参与 hash 的规范化 payload（不含 id，因 save 时尚未生成）"""
        data = {
            'actor_id': self.actor_id,
            'actor_username': self.actor_username,
            'action': self.action,
            'action_category': self.action_category,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'result': self.result,
            'ip': self.ip_address,
            'detail': self.detail,
            'ts': self.created_at.isoformat() if self.created_at else '',
        }
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    def save(self, *args, **kwargs):
        """保存前：加行锁查 prev，防止并发分叉"""
        if not self.row_hash:
            from django.db import transaction
            with transaction.atomic():
                last = (
                    AuditLog.objects
                    .select_for_update()
                    .order_by('-id')
                    .first()
                )
                self.prev_hash = last.row_hash if last else ''
                payload = (self.prev_hash + '|' + self._build_payload()).encode('utf-8')
                self.row_hash = hashlib.sha256(payload).hexdigest()
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @classmethod
    def verify_chain(cls, limit=None):
        """审计校验：逐行重算 row_hash，返回 (ok, first_bad_id)"""
        qs = cls.objects.order_by('id')
        if limit:
            qs = qs[:limit]
        prev = ''
        for row in qs:
            payload = (prev + '|' + row._build_payload()).encode('utf-8')
            expected = hashlib.sha256(payload).hexdigest()
            if expected != row.row_hash:
                return False, row.id
            prev = row.row_hash
        return True, None

    def __str__(self):
        return f'Audit<{self.id}>{self.action}'
