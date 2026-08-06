"""
apps.notification.models 集成测试 —— 邮件订阅 & 发送日志 Model

覆盖范围：
- EmailSubscription：创建 / 默认值 / category 选项 / unique_together 约束 / created_at 自动填充
- EmailSendLog：创建 / 默认值 / status 选项 / sent_at 可空 / created_at 自动填充
- 字符串表示：两个模型均未自定义 __str__，使用 Django 默认表示

采用 pytest-django（django_db 集成测试）：
两个模型均带 ForeignKey('users.User') 与唯一约束，需真实 DB 事务语义验证
落库字段、默认值与约束触发，mock 无法还原 unique_together 的数据库层校验。
"""
import pytest
from django.db import IntegrityError

from apps.notification.models import EmailSubscription, EmailSendLog
from apps.users.models import User


def _create_user(username='subuser', email=None):
    """创建普通测试用户，订阅模型通过 user_id 外键关联"""
    return User.objects.create_user(
        username=username,
        email=email or f'{username}@test.com',
        password='testpass123',
    )


# ============================================================================
# EmailSubscription —— 用户订阅模型
# ============================================================================
@pytest.mark.django_db
class TestEmailSubscription:
    """EmailSubscription 创建、默认值与约束测试"""

    @pytest.mark.integration
    def test_create_subscription_defaults(self):
        """创建订阅时 is_enabled 默认 True，created_at 自动填充"""
        user = _create_user()
        sub = EmailSubscription.objects.create(user=user, category='daily_report')

        assert sub.user_id == user.id
        assert sub.category == 'daily_report'
        # is_enabled 未显式传值时应为默认 True
        assert sub.is_enabled is True
        # created_at 由 auto_now_add 自动写入
        assert sub.created_at is not None

    @pytest.mark.integration
    def test_create_subscription_disabled(self):
        """显式传入 is_enabled=False 时应落库为 False"""
        user = _create_user('dis_user')
        sub = EmailSubscription.objects.create(
            user=user, category='system_notice', is_enabled=False)

        assert sub.is_enabled is False

    @pytest.mark.integration
    def test_category_choices(self):
        """CATEGORY_CHOICES 应覆盖全部 5 类订阅类别"""
        expected = {
            'feedback_reply', 'daily_report', 'system_notice',
            'node_update', 'keyword_alert',
        }
        actual = {value for value, _ in EmailSubscription.CATEGORY_CHOICES}
        assert actual == expected

    @pytest.mark.integration
    def test_unique_together_user_category(self):
        """同一 user + category 组合唯一，重复写入应触发 IntegrityError

        unique_together = [('user', 'category')] 在数据库层约束，
        即便绕过 ORM 校验也会被 PG 拒绝。
        """
        user = _create_user('uniq_user')
        EmailSubscription.objects.create(user=user, category='node_update')

        # 同一用户同一类别再次 create 应触发唯一约束
        with pytest.raises(IntegrityError):
            EmailSubscription.objects.create(user=user, category='node_update')

    @pytest.mark.integration
    def test_different_users_same_category_allowed(self):
        """不同用户订阅同一类别应允许（约束仅限定 user+category 组合）"""
        u1 = _create_user('u1')
        u2 = _create_user('u2')
        EmailSubscription.objects.create(user=u1, category='system_notice')
        # 不应抛异常
        sub2 = EmailSubscription.objects.create(user=u2, category='system_notice')
        assert sub2.id is not None

    @pytest.mark.integration
    def test_same_user_different_categories_allowed(self):
        """同一用户订阅多个不同类别应允许"""
        user = _create_user('multi_user')
        for cat in ['node_update', 'system_notice', 'keyword_alert']:
            EmailSubscription.objects.create(user=user, category=cat)
        # 该用户应有 3 条订阅记录
        assert EmailSubscription.objects.filter(user=user).count() == 3

    @pytest.mark.integration
    def test_str_representation(self):
        """模型未自定义 __str__，使用 Django 默认表示（含类名与主键）"""
        user = _create_user('str_user')
        sub = EmailSubscription.objects.create(user=user, category='daily_report')

        text = str(sub)
        # 默认 __str__ 格式为 "<ModelName> object (<pk>)"
        assert 'EmailSubscription' in text
        assert str(sub.id) in text

    @pytest.mark.integration
    def test_user_relation_reverse_name(self):
        """related_name='subscriptions'，应能反向查询用户的所有订阅"""
        user = _create_user('rel_user')
        EmailSubscription.objects.create(user=user, category='node_update')
        EmailSubscription.objects.create(user=user, category='daily_report')

        # 反向关系 subscriptions 应返回该用户的两条订阅
        assert user.subscriptions.count() == 2


# ============================================================================
# EmailSendLog —— 邮件发送日志模型
# ============================================================================
@pytest.mark.django_db
class TestEmailSendLog:
    """EmailSendLog 创建、默认值与状态选项测试"""

    @pytest.mark.integration
    def test_create_log_defaults(self):
        """创建发送日志时 status/body/error_message/retry_count 均为默认值"""
        log = EmailSendLog.objects.create(
            to_email='dest@test.com', subject='测试邮件')

        assert log.to_email == 'dest@test.com'
        assert log.subject == '测试邮件'
        # body 未传时默认空串
        assert log.body == ''
        # category 默认 system_notice
        assert log.category == 'system_notice'
        # status 默认 pending
        assert log.status == 'pending'
        # error_message 默认空串
        assert log.error_message == ''
        # retry_count 默认 0
        assert log.retry_count == 0
        # sent_at 默认 None（尚未发送）
        assert log.sent_at is None
        # created_at 自动填充
        assert log.created_at is not None

    @pytest.mark.integration
    def test_create_log_full_fields(self):
        """显式传入全部字段时应如实落库"""
        log = EmailSendLog.objects.create(
            to_email='full@test.com',
            subject='完整字段邮件',
            body='邮件正文内容',
            category='feedback_reply',
            status='success',
            error_message='',
            retry_count=2,
        )

        assert log.body == '邮件正文内容'
        assert log.category == 'feedback_reply'
        assert log.status == 'success'
        assert log.retry_count == 2

    @pytest.mark.integration
    def test_status_choices(self):
        """STATUS_CHOICES 应覆盖 pending/sending/success/failed 四态"""
        expected = {'pending', 'sending', 'success', 'failed'}
        actual = {value for value, _ in EmailSendLog.STATUS_CHOICES}
        assert actual == expected

    @pytest.mark.integration
    def test_sent_at_nullable(self):
        """sent_at 允许为空（未发送或发送中），落库 None 不报错"""
        log = EmailSendLog.objects.create(
            to_email='null@test.com', subject='未发送', sent_at=None)
        assert log.sent_at is None

    @pytest.mark.integration
    def test_status_transition_flow(self):
        """模拟一次发送状态流转：pending → sending → success

        验证 status 字段可多次更新，记录发送生命周期。
        """
        log = EmailSendLog.objects.create(
            to_email='flow@test.com', subject='状态流转')

        log.status = 'sending'
        log.save()
        log.refresh_from_db()
        assert log.status == 'sending'

        log.status = 'success'
        log.save()
        log.refresh_from_db()
        assert log.status == 'success'

    @pytest.mark.integration
    def test_failed_with_error_message(self):
        """发送失败时应能记录 error_message 与 retry_count"""
        log = EmailSendLog.objects.create(
            to_email='fail@test.com',
            subject='失败邮件',
            status='failed',
            error_message='SMTP connection timeout',
            retry_count=3,
        )
        assert log.status == 'failed'
        assert log.error_message == 'SMTP connection timeout'
        assert log.retry_count == 3

    @pytest.mark.integration
    def test_str_representation(self):
        """模型未自定义 __str__，使用 Django 默认表示"""
        log = EmailSendLog.objects.create(
            to_email='str@test.com', subject='字符串测试')

        text = str(log)
        assert 'EmailSendLog' in text
        assert str(log.id) in text
