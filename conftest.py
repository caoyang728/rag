"""
测试配置 conftest.py
- 提供共享的 fixtures
"""
import pytest
from django.test.client import Client


@pytest.fixture
def api_client():
    """返回 Django 测试客户端"""
    return Client()


@pytest.fixture
def auth_client(db, test_user):
    """返回已认证的 Django 测试客户端"""
    client = Client()
    client.force_login(test_user)
    return client


@pytest.fixture
def test_user(db):
    """创建测试用户"""
    from apps.users.models import User
    user = User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )
    return user
