"""
security app - IP 白/黑名单 & 登录尝试 & 敏感词
对齐数据库设计 F2/F3/F4/F5
五重安全防线之 IP 风控（白名单+连续失败自动黑名单+失败原因分类）
"""
from django.db import models


class IpWhitelist(models.Model):
    """F2 ip_whitelist - IP 白名单（CIDR 支持）"""

    id = models.BigAutoField(primary_key=True)
    ip_or_cidr = models.CharField(max_length=64, unique=True,
                                    help_text='单 IP 或 CIDR，如 10.0.0.0/8')
    description = models.CharField(max_length=128, blank=True, default='')
    is_enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey('users.SysUser', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='created_by')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'security_ip_whitelist'


class IpBlacklist(models.Model):
    """F3 ip_blacklist - IP 黑名单（含过期时间）"""

    REASON_CHOICES = [
        ('login_fail', '登录连续失败'),
        ('manual', '人工封禁'),
        ('bot', '爬虫/机器人'),
    ]

    id = models.BigAutoField(primary_key=True)
    ip = models.CharField(max_length=64, db_index=True)
    reason = models.CharField(max_length=32, choices=REASON_CHOICES, default='manual')
    detail = models.CharField(max_length=256, blank=True, default='')
    fail_count = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True,
                                       help_text='过期时间，NULL 表示永久')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'security_ip_blacklist'
        indexes = [
            models.Index(fields=['ip', 'is_active'], name='idx_bl_ip_active'),
            models.Index(fields=['expires_at'], name='idx_bl_expires'),
        ]


class LoginAttempt(models.Model):
    """F4 login_attempt - 登录尝试日志"""

    RESULT_CHOICES = [
        ('success', 'success'),
        ('wrong_password', 'wrong_password'),
        ('user_not_found', 'user_not_found'),
        ('locked', 'locked'),
        ('captcha_fail', 'captcha_fail'),
        ('ip_denied', 'ip_denied'),
    ]

    id = models.BigAutoField(primary_key=True)
    username = models.CharField(max_length=64, blank=True, default='')
    user = models.ForeignKey('users.SysUser', on_delete=models.SET_NULL, null=True, blank=True,
                              db_column='user_id')
    ip = models.CharField(max_length=64, db_index=True)
    user_agent = models.CharField(max_length=256, blank=True, default='')
    result = models.CharField(max_length=32, choices=RESULT_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'security_login_attempt_record'
        indexes = [
            models.Index(fields=['ip', '-created_at'], name='idx_la_ip_time'),
            models.Index(fields=['username', '-created_at'], name='idx_la_user_time'),
            models.Index(fields=['result'], name='idx_la_result'),
        ]


class SensitiveWord(models.Model):
    """F5 sensitive_word - 敏感词库（用于输入检查/脱敏）"""

    CATEGORY_CHOICES = [
        ('phone', '手机号'),
        ('id_card', '身份证'),
        ('email', '邮箱'),
        ('bank_card', '银行卡'),
        ('secret', '内部机密'),
        ('other', '其它'),
    ]
    ACTION_CHOICES = [
        ('mask', '脱敏'),
        ('block', '拦截'),
        ('warn', '仅告警'),
    ]

    id = models.BigAutoField(primary_key=True)
    word = models.CharField(max_length=128, unique=True)
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default='other')
    action = models.CharField(max_length=8, choices=ACTION_CHOICES, default='mask')
    is_regex = models.BooleanField(default=False, help_text='是否作为正则处理')
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'security_sensitive_word_list'
        indexes = [
            models.Index(fields=['category', 'is_enabled'], name='idx_sw_cat_enable'),
        ]
