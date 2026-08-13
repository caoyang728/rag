"""
apps.users.perm_cache 单元测试 —— RBAC 权限分层缓存（L1~L5）+ 延迟双删

覆盖范围：
- Key 生成函数 _key_l1 ~ _key_l5：保证 key 格式与设计文档一致（缓存分层定位依赖）
- 可缓存性判定 _is_cacheable：None / 未登录 / super_admin / 普通用户 四类场景
- L1~L4 读写（perm_fn / scope_dept / scope_team / scope_level）：命中/未命中/不可缓存
- L5 资源级读写（perm_doc）：按资源维度存储，不依赖用户
- invalidate_keys：None 过滤、去重、立即删除、Celery 延迟双删调度
- invalidate_user_perms：精确构建 L1~L4 key 一次性失效
- _collect_by_pattern：非 Redis 后端（LocMem）静默降级返回空列表
- delayed_delete_keys：延迟双删第二次删除的兜底逻辑

perm_cache 是权限系统的性能与安全关键路径，缓存读写/失效逻辑必须独立验证，
不应耦合 DB 或真实 Redis，避免环境依赖导致权限漏洞被掩盖。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.users import perm_cache


# ============================================================================
# Key 生成函数 —— 验证 key 格式与分层映射一致（缓存失效与定位依赖 key 格式稳定）
# ============================================================================
class TestKeyGenerators:
    """L1~L5 key 生成函数测试"""

    def test_key_l1_format(self):
        """L1 key 必须为 perm:fn:{uid} 格式，前端菜单渲染与鉴权中间件按此定位"""
        assert perm_cache._key_l1(123) == 'perm:fn:123'

    def test_key_l2_format(self):
        """L2 key 必须为 perm:scope:dept:{uid} 格式，部门级数据检索过滤按此定位"""
        assert perm_cache._key_l2(123) == 'perm:scope:dept:123'

    def test_key_l3_format(self):
        """L3 key 必须为 perm:scope:team:{uid} 格式，团队级数据检索过滤按此定位"""
        assert perm_cache._key_l3(123) == 'perm:scope:team:123'

    def test_key_l4_format(self):
        """L4 key 必须为 perm:scope:level:{uid} 格式，检索层过滤粒度决策按此定位"""
        assert perm_cache._key_l4(123) == 'perm:scope:level:123'

    def test_key_l5_format(self):
        """L5 key 必须为 perm:doc:{res_type}:{res_id} 格式，资源临时授权清单按资源维度定位"""
        assert perm_cache._key_l5('DOCUMENT', 456) == 'perm:doc:DOCUMENT:456'


# ============================================================================
# 可缓存性判定 —— 决定是否走缓存快路径，错误判定会导致越权或缓存穿透
# ============================================================================
class TestIsCacheable:
    """_is_cacheable 判定：未登录/super_admin 不走缓存"""

    def test_none_user_not_cacheable(self):
        """None 用户无 user_id，无法建 key，且匿名鉴权另有快路径"""
        assert perm_cache._is_cacheable(None) is False

    def test_unauthenticated_user_not_cacheable(self):
        """未登录用户无 user_id，不能缓存（避免匿名请求污染缓存）"""
        user = MagicMock()
        user.is_authenticated = False
        user.is_super_admin = False
        assert perm_cache._is_cacheable(user) is False

    def test_super_admin_not_cacheable(self):
        """super_admin 走系统级快路径直接放行，缓存其权限集既无意义又易脏（变更极低频）"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = True
        assert perm_cache._is_cacheable(user) is False

    def test_normal_user_cacheable(self):
        """已登录的普通用户走缓存，减少高频鉴权的 DB 压力"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        assert perm_cache._is_cacheable(user) is True


# ============================================================================
# L1 功能权限点集合读写 —— 高频鉴权路径，set 转 list 存储、get 转回 set
# ============================================================================
class TestPermFnCache:
    """L1 perm:fn:{uid} 功能权限点集合读写"""

    @pytest.mark.unit
    def test_get_perm_fn_uncacheable_returns_none(self):
        """不可缓存用户（super_admin/未登录）get 直接返回 None，
        调用方收到 None 应回源 get_user_permissions，不能当空集处理"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = True
        user.id = 1
        with patch.object(perm_cache, 'cache') as mock_cache:
            assert perm_cache.get_perm_fn(user) is None
            # 不可缓存时不查 cache，避免无意义的 key 计算与网络开销
            mock_cache.get.assert_not_called()

    @pytest.mark.unit
    def test_get_perm_fn_cache_miss_returns_none(self):
        """缓存未命中返回 None，调用方需回源 DB 计算权限集"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 10
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = None
            assert perm_cache.get_perm_fn(user) is None
            mock_cache.get.assert_called_once_with('perm:fn:10')

    @pytest.mark.unit
    def test_get_perm_fn_cache_hit_returns_set(self):
        """缓存命中时把存储的 list 转回 set 去重，调用方直接命中无需查 DB"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 10
        with patch.object(perm_cache, 'cache') as mock_cache:
            # 存储时 set 被转为 list（set 不可 JSON 序列化）
            mock_cache.get.return_value = ['user.manage', 'kb.document.read', 'user.manage']
            result = perm_cache.get_perm_fn(user)
            assert result == {'user.manage', 'kb.document.read'}
            assert isinstance(result, set)

    @pytest.mark.unit
    def test_set_perm_fn_uncacheable_noop(self):
        """super_admin/未登录 set 为 no-op，避免缓存其权限集导致脏数据"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = True
        user.id = 1
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.set_perm_fn(user, {'user.manage'})
            mock_cache.set.assert_not_called()

    @pytest.mark.unit
    def test_set_perm_fn_stores_as_list(self):
        """set 存为 list 以兼容 JSON 序列化（Redis 后端 JSON 编码）"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 10
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.set_perm_fn(user, {'user.manage', 'kb.document.read'})
            mock_cache.set.assert_called_once()
            key, value, ttl = mock_cache.set.call_args[0]
            assert key == 'perm:fn:10'
            # set 被转为 list（顺序不固定，断言元素一致即可）
            assert sorted(value) == ['kb.document.read', 'user.manage']
            assert ttl == perm_cache.CACHE_TTL

    @pytest.mark.unit
    def test_set_perm_fn_none_set_handled(self):
        """传入 None 时安全降级为空 list，避免 None 不可迭代报错"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 10
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.set_perm_fn(user, None)
            key, value, _ = mock_cache.set.call_args[0]
            assert value == []


# ============================================================================
# L2/L3/L4 数据范围缓存读写 —— 检索层过滤粒度决策依赖
# ============================================================================
class TestScopeCache:
    """L2 dept / L3 team / L4 level 数据范围缓存读写"""

    @pytest.mark.unit
    def test_get_scope_dept_cache_hit(self):
        """L2 命中返回 set<int>，供部门级数据检索过滤"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 7
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = [1, 2, 3]
            assert perm_cache.get_scope_dept(user) == {1, 2, 3}
            mock_cache.get.assert_called_once_with('perm:scope:dept:7')

    @pytest.mark.unit
    def test_get_scope_dept_cache_miss(self):
        """L2 未命中返回 None，调用方回源 get_user_managed_depts"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 7
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = None
            assert perm_cache.get_scope_dept(user) is None

    @pytest.mark.unit
    def test_set_scope_dept_stores_as_list(self):
        """L2 set<int> 存为 list<int>"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 7
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.set_scope_dept(user, {1, 2})
            key, value, _ = mock_cache.set.call_args[0]
            assert key == 'perm:scope:dept:7'
            assert sorted(value) == [1, 2]

    @pytest.mark.unit
    def test_get_scope_team_cache_hit(self):
        """L3 命中返回 set<int>，供团队级数据检索过滤"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 8
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = [10, 20]
            assert perm_cache.get_scope_team(user) == {10, 20}
            mock_cache.get.assert_called_once_with('perm:scope:team:8')

    @pytest.mark.unit
    def test_get_scope_team_cache_miss(self):
        """L3 未命中返回 None，调用方回源 get_user_managed_teams"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 8
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = None
            assert perm_cache.get_scope_team(user) is None

    @pytest.mark.unit
    def test_set_scope_team_stores_as_list(self):
        """L3 set<int> 存为 list<int>"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 8
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.set_scope_team(user, {10, 20})
            key, value, _ = mock_cache.set.call_args[0]
            assert key == 'perm:scope:team:8'
            assert sorted(value) == [10, 20]

    @pytest.mark.unit
    def test_get_scope_level_cache_hit(self):
        """L4 命中返回 str（TEAM/DEPT/GLOBAL），供检索层决定过滤粒度"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 9
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = 'DEPT'
            assert perm_cache.get_scope_level(user) == 'DEPT'
            mock_cache.get.assert_called_once_with('perm:scope:level:9')

    @pytest.mark.unit
    def test_get_scope_level_cache_miss(self):
        """L4 未命中返回 None，调用方回源 get_user_data_scope_level"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 9
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = None
            assert perm_cache.get_scope_level(user) is None

    @pytest.mark.unit
    def test_set_scope_level_stores_str(self):
        """L4 str 枚举值直接存储（无需转换）"""
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 9
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.set_scope_level(user, 'GLOBAL')
            key, value, _ = mock_cache.set.call_args[0]
            assert key == 'perm:scope:level:9'
            assert value == 'GLOBAL'


# ============================================================================
# L5 资源临时授权清单读写 —— 文档/节点可见性判定依赖，按资源维度存储
# ============================================================================
class TestPermDocCache:
    """L5 perm:doc:{res_type}:{res_id} 资源临时授权清单读写"""

    @pytest.mark.unit
    def test_get_perm_doc_cache_hit(self):
        """L5 命中返回 list[dict]，供文档/节点可见性判定"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            entries = [{'uid': 1, 'access_level': 'read', 'expires_at': None}]
            mock_cache.get.return_value = entries
            assert perm_cache.get_perm_doc('DOCUMENT', 100) == entries
            mock_cache.get.assert_called_once_with('perm:doc:DOCUMENT:100')

    @pytest.mark.unit
    def test_get_perm_doc_cache_miss(self):
        """L5 未命中返回 None，调用方回源查 ResourceShare 表重建清单"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.get.return_value = None
            assert perm_cache.get_perm_doc('DOCUMENT', 100) is None

    @pytest.mark.unit
    def test_set_perm_doc_stores_as_list(self):
        """L5 list[dict] 直接存储（后端负责序列化）"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            entries = [{'uid': 1, 'access_level': 'read'}]
            perm_cache.set_perm_doc('KNOWLEDGE_NODE', 50, entries)
            key, value, _ = mock_cache.set.call_args[0]
            assert key == 'perm:doc:KNOWLEDGE_NODE:50'
            assert value == entries

    @pytest.mark.unit
    def test_set_perm_doc_none_entries_handled(self):
        """传入 None 时安全降级为空 list"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.set_perm_doc('DOCUMENT', 100, None)
            key, value, _ = mock_cache.set.call_args[0]
            assert value == []


# ============================================================================
# invalidate_keys —— 立即删 + 延迟双删（防并发脏写回填）
# ============================================================================
class TestInvalidateKeys:
    """invalidate_keys：None 过滤、去重、立即删除、延迟双删调度"""

    @pytest.mark.unit
    def test_empty_keys_noop(self):
        """空列表 / None 直接返回，不触发任何缓存操作"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.invalidate_keys([])
            mock_cache.delete_many.assert_not_called()
            perm_cache.invalidate_keys(None)
            mock_cache.delete_many.assert_not_called()

    @pytest.mark.unit
    def test_filters_none_and_dedup_correct(self):
        """None 项被过滤、重复项去重并保持顺序，立即删除调用一次"""
        with patch.object(perm_cache, 'cache') as mock_cache, \
             patch.object(perm_cache, 'delayed_delete_keys') as mock_delayed:
            perm_cache.invalidate_keys(['a', None, 'b', 'a', None, 'b'])
            # 立即删除一次，key 列表为去重后的 ['a', 'b']
            mock_cache.delete_many.assert_called_once()
            keys_passed = mock_cache.delete_many.call_args[0][0]
            assert keys_passed == ['a', 'b']
            # 延迟双删调度一次
            mock_delayed.apply_async.assert_called_once()
            delayed_keys = mock_delayed.apply_async.call_args[1]['args'][0]
            assert delayed_keys == ['a', 'b']
            # countdown 必须为延迟双删间隔
            assert mock_delayed.apply_async.call_args[1]['countdown'] == perm_cache.DELAYED_DELETE_SECONDS

    @pytest.mark.unit
    def test_immediate_delete_fallback_on_exception(self):
        """立即 delete_many 异常时降级为逐个 delete，避免某后端异常导致整批漏删"""
        with patch.object(perm_cache, 'cache') as mock_cache, \
             patch.object(perm_cache, 'delayed_delete_keys') as mock_delayed:
            mock_cache.delete_many.side_effect = RuntimeError('backend error')
            perm_cache.invalidate_keys(['x', 'y'])
            # 降级逐个删除
            assert mock_cache.delete.call_count == 2

    @pytest.mark.unit
    def test_celery_unavailable_fallback_to_thread(self):
        """Celery broker 不可用时降级为 daemon 线程，权限安全优先（宁可多删）"""
        with patch.object(perm_cache, 'cache') as mock_cache, \
             patch.object(perm_cache, 'delayed_delete_keys') as mock_delayed:
            mock_delayed.apply_async.side_effect = RuntimeError('broker down')
            perm_cache.invalidate_keys(['x'])
            # 降级线程路径下立即删仍执行
            mock_cache.delete_many.assert_called_once()


# ============================================================================
# delayed_delete_keys —— 延迟双删第二次删除的兜底逻辑
# ============================================================================
class TestDelayedDeleteKeys:
    """delayed_delete_keys：Celery 任务，延迟双删的第 2 次删除"""

    @pytest.mark.unit
    def test_empty_keys_noop(self):
        """空 key 列表直接返回，不触发删除"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.delayed_delete_keys([])
            mock_cache.delete_many.assert_not_called()
            perm_cache.delayed_delete_keys(None)
            mock_cache.delete_many.assert_not_called()

    @pytest.mark.unit
    def test_delete_many_success(self):
        """正常路径走 delete_many 批量删除"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            perm_cache.delayed_delete_keys(['a', 'b'])
            mock_cache.delete_many.assert_called_once_with(['a', 'b'])

    @pytest.mark.unit
    def test_fallback_per_key_on_exception(self):
        """delete_many 异常时降级逐个删，避免整批漏删（权限缓存漏删会越权）"""
        with patch.object(perm_cache, 'cache') as mock_cache:
            mock_cache.delete_many.side_effect = RuntimeError('error')
            perm_cache.delayed_delete_keys(['a', 'b'])
            assert mock_cache.delete.call_count == 2


# ============================================================================
# invalidate_user_perms —— 用户调岗/拉黑/授权变更时全量失效个人缓存
# ============================================================================
class TestInvalidateUserPerms:
    """invalidate_user_perms：精确构建 L1~L4 key 一次性失效"""

    @pytest.mark.unit
    def test_empty_user_id_noop(self):
        """空 user_id 直接返回，避免误删 key=perm:fn:None 等脏 key"""
        with patch.object(perm_cache, 'invalidate_keys') as mock_inv:
            perm_cache.invalidate_user_perms(0)
            mock_inv.assert_not_called()
            perm_cache.invalidate_user_perms(None)
            mock_inv.assert_not_called()

    @pytest.mark.unit
    def test_builds_l1_to_l4_keys(self):
        """精确构建 L1~L4 四个 key 走延迟双删（高效，不依赖 scan）"""
        with patch.object(perm_cache, 'invalidate_keys') as mock_inv:
            perm_cache.invalidate_user_perms(42)
            mock_inv.assert_called_once()
            keys = mock_inv.call_args[0][0]
            assert keys == [
                'perm:fn:42',
                'perm:scope:dept:42',
                'perm:scope:team:42',
                'perm:scope:level:42',
            ]


# ============================================================================
# _collect_by_pattern —— 非 Redis 后端（LocMem）静默降级
# ============================================================================
class TestCollectByPattern:
    """_collect_by_pattern：按 glob 模式扫描 key，非 Redis 后端降级返回空列表"""

    @pytest.mark.unit
    def test_returns_empty_when_django_redis_unavailable(self):
        """未安装 django_redis（LocMem 后端）时静默降级返回空列表，
        开发环境进程内缓存重启即清，可接受"""
        import sys
        # 模拟 django_redis 未安装：注入 ImportError
        with patch.dict(sys.modules, {'django_redis': None}):
            with patch('builtins.__import__', side_effect=ImportError):
                result = perm_cache._collect_by_pattern('perm:doc:*')
                assert result == []

    @pytest.mark.unit
    def test_returns_keys_from_redis_scan(self):
        """Redis 后端 scan_iter 返回的 bytes key 被解码为 str，兼容 cache.delete API"""
        fake_conn = MagicMock()
        fake_conn.scan_iter.return_value = [b'perm:doc:DOCUMENT:1', b'perm:doc:DOCUMENT:2']
        fake_redis_module = MagicMock()
        fake_redis_module.get_redis_connection.return_value = fake_conn
        import sys
        with patch.dict(sys.modules, {'django_redis': fake_redis_module}):
            result = perm_cache._collect_by_pattern('perm:doc:*')
            assert result == ['perm:doc:DOCUMENT:1', 'perm:doc:DOCUMENT:2']

    @pytest.mark.unit
    def test_returns_empty_on_scan_exception(self):
        """scan 异常时返回空列表，不让缓存层异常阻断主业务"""
        fake_redis_module = MagicMock()
        fake_redis_module.get_redis_connection.side_effect = RuntimeError('conn lost')
        import sys
        with patch.dict(sys.modules, {'django_redis': fake_redis_module}):
            result = perm_cache._collect_by_pattern('perm:doc:*')
            assert result == []


# ============================================================================
# invalidate_resource_share / invalidate_resource_block
# —— 失效函数对齐设计文档失效映射表
# ============================================================================
class TestInvalidateHelpers:
    """资源失效辅助函数"""

    @pytest.mark.unit
    def test_invalidate_resource_share_invalidates_l5(self):
        """资源共享变更始终失效对应资源 L5，重建时从 ResourceShare 表回源"""
        with patch.object(perm_cache, 'invalidate_keys') as mock_inv:
            perm_cache.invalidate_resource_share('DOCUMENT', 100)
            mock_inv.assert_called_once_with(['perm:doc:DOCUMENT:100'])

    @pytest.mark.unit
    def test_invalidate_resource_share_user_scope_clears_all_l5(self):
        """USER 级共享变更时全量清 L5（保守过失效，权限安全优先于性能）"""
        with patch.object(perm_cache, 'invalidate_keys') as mock_inv, \
             patch.object(perm_cache, '_collect_by_pattern') as mock_collect:
            mock_collect.return_value = ['perm:doc:DOCUMENT:1', 'perm:doc:DOCUMENT:2']
            perm_cache.invalidate_resource_share('DOCUMENT', 100, share_scope_type='USER')
            # 第 1 次精确失效该资源，第 2 次全量清 L5
            assert mock_inv.call_count == 2
            mock_inv.assert_any_call(['perm:doc:DOCUMENT:100'])
            mock_inv.assert_any_call(['perm:doc:DOCUMENT:1', 'perm:doc:DOCUMENT:2'])

    @pytest.mark.unit
    def test_invalidate_resource_share_empty_res_noop(self):
        """空 res_type 或 None res_id 直接返回"""
        with patch.object(perm_cache, 'invalidate_keys') as mock_inv:
            perm_cache.invalidate_resource_share('', 100)
            mock_inv.assert_not_called()
            perm_cache.invalidate_resource_share('DOCUMENT', None)
            mock_inv.assert_not_called()

    @pytest.mark.unit
    def test_invalidate_resource_block_clears_l5_and_user_perms(self):
        """拉黑/解封同时清资源 L5 与被拉黑人 L1~L4（高风险权限事件，强制重算）"""
        with patch.object(perm_cache, 'invalidate_keys') as mock_inv, \
             patch.object(perm_cache, 'invalidate_user_perms') as mock_user_inv:
            perm_cache.invalidate_resource_block('DOCUMENT', 100, blocked_user_id=42)
            mock_inv.assert_called_once_with(['perm:doc:DOCUMENT:100'])
            mock_user_inv.assert_called_once_with(42)
