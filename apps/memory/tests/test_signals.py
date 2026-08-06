"""
apps.memory.signals 测试 —— 记忆清理/缓存失效信号

覆盖范围：
- on_session_delete_clean_short_term：会话删除时清理短时记忆
- on_global_memory_change_clear_cache：GlobalMemory 变更时清空全局缓存

采用 DB 集成：
信号由 ORM 的 post_delete / post_save 触发，需真实模型操作才能验证联动行为。
"""
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.memory.models import Session, GlobalMemory
from apps.memory.manager import MemoryManager
from apps.memory.short_term import ShortTermMemory
from apps.users.models import User


def _make_user(username='mem-signal-user'):
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')


@pytest.mark.django_db
@pytest.mark.integration
class TestSessionDeleteSignal:
    """会话删除 → 清理短时记忆信号测试"""

    def test_delete_session_clears_short_term(self):
        """删除会话时应调用 ShortTermMemory.clear(会话id)"""
        user = _make_user()
        sess = Session.objects.create(user=user, title='待删会话')
        with patch.object(ShortTermMemory, 'clear') as mock_clear:
            sess.delete()
        mock_clear.assert_called_once_with(sess.id)

    def test_clear_exception_not_block_delete(self):
        """短时记忆清理抛异常时不应阻断会话删除（日志降级）"""
        user = _make_user()
        sess = Session.objects.create(user=user, title='待删会话')
        with patch.object(ShortTermMemory, 'clear',
                          side_effect=RuntimeError('redis down')):
            sess.delete()  # 不应抛异常
        assert not Session.objects.filter(id=sess.id).exists()


@pytest.mark.django_db
@pytest.mark.integration
class TestGlobalMemoryCacheSignal:
    """GlobalMemory 变更 → 清空全局缓存信号测试"""

    def test_save_global_memory_clears_cache(self):
        """新建 GlobalMemory 后 MemoryManager._global_cache 应被清空"""
        MemoryManager._global_cache = {'key1': 'cache-data'}
        MemoryManager._global_cache_time = timezone.now().timestamp()
        GlobalMemory.objects.create(key='company_rules', content='规则内容')
        assert MemoryManager._global_cache == {}
        assert MemoryManager._global_cache_time == 0

    def test_update_global_memory_clears_cache(self):
        """更新 GlobalMemory 同样触发缓存清空（缓存是全局快照，变更即失效）"""
        gm = GlobalMemory.objects.create(key='company_rules', content='规则一')
        MemoryManager._global_cache = {'key1': 'cache-data'}
        gm.content = '规则二'
        gm.save()
        assert MemoryManager._global_cache == {}
