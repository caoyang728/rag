"""
notification app - 邮件订阅 & 发送日志 Model
对齐数据库设计 G2/G3
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField


class EmailSubscription(models.Model):
    """G2 email_subscription - 用户订阅（差评回复/报告日报/系统公告等）"""

    CATEGORY_CHOICES = [
        ('feedback_reply', 'feedback_reply'),
        ('daily_report', 'daily_report'),
        ('system_notice', 'system_notice'),
        ('node_update', 'node_update'),
        ('keyword_alert', 'keyword_alert'),
    ]

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey('users.SysUser', on_delete=models.CASCADE,
                             db_column='user_id', related_name='subscriptions')
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES)
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'email_subscription'
        unique_together = [('user', 'category')]


class EmailSendLog(models.Model):
    """G3 email_send_log - 邮件发送日志"""

    STATUS_CHOICES = [
        ('pending', 'pending'),
        ('sending', 'sending'),
        ('success', 'success'),
        ('failed', 'failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    to_email = models.EmailField()
    subject = models.CharField(max_length=256)
    body = models.TextField(default='')
    category = models.CharField(max_length=32, default='system_notice')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True, default='')
    retry_count = models.IntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'email_send_log'
        indexes = [
            models.Index(fields=['status', '-created_at'], name='idx_esl_status'),
        ]
