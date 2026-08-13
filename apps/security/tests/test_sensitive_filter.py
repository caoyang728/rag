"""
apps.security.sensitive_filter 单元测试 —— AC 自动机敏感词审查器

覆盖范围：
- Ahocorasick: add_word / build / search / word_count（纯算法，无外部依赖）
- SensitiveFilter: check / feed / flush / disabled passthrough（mock DB + Redis）
- get_sensitive_filter: 便捷函数

测试策略：
- 纯单元测试，不依赖 DB 和 Redis
- mock SensitiveWord.objects.filter 提供 AC 构建数据，绕过 DB 加载
- mock _get_redis 返回 None，绕过多进程版本号同步
- 重置单例避免测试间状态污染（_instance / _redis_client 为类级属性）
"""
import time as time_mod

import pytest
from django.test import override_settings
from unittest.mock import patch, MagicMock

from apps.security.sensitive_filter import (
    Ahocorasick,
    HitResult,
    SensitiveFilter,
    get_sensitive_filter,
)


# ============================================================================
# 辅助函数
# ============================================================================

def _make_sw(word, category='other', action='mask', is_regex=False):
    """构造 mock SensitiveWord 对象，绕过 DB

    _load_from_db 通过属性访问 sw.word / sw.category / sw.action / sw.is_regex，
    用 MagicMock 模拟这些字段即可避免 ORM 查询。
    """
    sw = MagicMock()
    sw.word = word
    sw.category = category
    sw.action = action
    sw.is_regex = is_regex
    return sw


def _build_filter(words_spec):
    """构建带测试词的 SensitiveFilter 单例

    通过 mock SensitiveWord.objects.filter 的返回值，绕过 DB 加载。
    通过 mock _get_redis 返回 None，绕过 Redis 版本号检查（降级走 TTL 兜底）。

    Args:
        words_spec: [(word, category, action, is_regex), ...]

    Returns:
        SensitiveFilter 单例实例，_ac / _word_meta 已填充
    """
    mock_words = [_make_sw(w, c, a, r) for w, c, a, r in words_spec]
    # patch 必须覆盖 get_instance 全程：_load_from_db 读 DB、_read_redis_version 读 Redis
    with patch('apps.security.models.SensitiveWord.objects') as mock_manager, \
         patch.object(SensitiveFilter, '_get_redis', return_value=None):
        mock_manager.filter.return_value.iterator.return_value = mock_words
        sf = SensitiveFilter.get_instance()
        # 让返回实例后续的 check/feed 命中统计走熔断短路（_get_redis 返回 None），
        # 保持"纯单元测试、不依赖 Redis"——命中计数只在管理端 GET 时才落库
        SensitiveFilter._redis_unavailable_until = time_mod.time() + 300
        return sf


@pytest.fixture(autouse=True)
def _reset_sensitive_filter_singleton():
    """每个测试前后重置 SensitiveFilter 单例，避免测试间状态污染

    单例的 _ac / _word_meta 在测试间会残留，必须清理；
    _redis_client 同理，避免上个测试的 mock 连接影响下个测试。
    """
    SensitiveFilter._instance = None
    SensitiveFilter._redis_client = None
    SensitiveFilter._redis_unavailable_until = 0.0
    yield
    SensitiveFilter._instance = None
    SensitiveFilter._redis_client = None
    SensitiveFilter._redis_unavailable_until = 0.0


# ============================================================================
# Ahocorasick —— AC 自动机纯算法测试（无外部依赖）
# ============================================================================
class TestAhocorasick:
    """AC 自动机多模式匹配测试"""

    @pytest.mark.unit
    def test_add_word_and_search(self):
        """添加单个词后 search 能命中，返回 (start, end, word) 三元组"""
        ac = Ahocorasick()
        ac.add_word("敏感词")
        ac.build()
        results = ac.search("包含敏感词的文本")
        # 敏感词 在 "包含敏感词的文本" 中起始索引为 2，end exclusive 为 5
        assert (2, 5, "敏感词") in results

    @pytest.mark.unit
    def test_build_automatically(self):
        """search 前未显式 build() 时自动构建，搜索结果仍正确

        search 内部检查 _built 标志，未构建时自动调用 build()，
        避免调用方忘记 build 导致漏审。
        """
        ac = Ahocorasick()
        ac.add_word("自动构建")
        # 不调用 ac.build()，直接 search
        results = ac.search("测试自动构建功能")
        assert any(word == "自动构建" for _, _, word in results)

    @pytest.mark.unit
    def test_multiple_words(self):
        """添加多个词后 search 能一次性返回全部命中（AC 自动机核心优势）"""
        ac = Ahocorasick()
        ac.add_word("词一")
        ac.add_word("词二")
        ac.add_word("词三")
        ac.build()
        results = ac.search("这里有词一和词二以及词三")
        hit_words = {word for _, _, word in results}
        assert {"词一", "词二", "词三"} <= hit_words

    @pytest.mark.unit
    def test_overlapping_words(self):
        """重叠词匹配："abc" 和 "bcd" 在 "abcd" 中应分别命中

        AC 自动机通过 fail 指针处理重叠匹配，不会因已命中 "abc"
        就跳过 "bcd"（后者通过 "abc" 的 fail 链继承匹配）。
        """
        ac = Ahocorasick()
        ac.add_word("abc")
        ac.add_word("bcd")
        ac.build()
        results = ac.search("abcd")
        # "abc" 命中在 (0,3)，"bcd" 命中在 (1,4)
        assert (0, 3, "abc") in results
        assert (1, 4, "bcd") in results

    @pytest.mark.unit
    def test_empty_text(self):
        """空文本搜索返回空列表，不报错"""
        ac = Ahocorasick()
        ac.add_word("词")
        ac.build()
        assert ac.search("") == []

    @pytest.mark.unit
    def test_no_match(self):
        """文本不含任何敏感词时返回空列表"""
        ac = Ahocorasick()
        ac.add_word("敏感")
        ac.build()
        assert ac.search("这是一段正常文本") == []

    @pytest.mark.unit
    def test_word_count(self):
        """word_count 属性返回已添加词数（不含 fail 链继承的词）"""
        ac = Ahocorasick()
        ac.add_word("甲")
        ac.add_word("乙")
        ac.add_word("丙")
        assert ac.word_count == 3

    @pytest.mark.unit
    def test_add_word_empty_ignored(self):
        """添加空串词应被忽略，不改变词库"""
        ac = Ahocorasick()
        ac.add_word('')
        assert ac.word_count == 0
        ac.add_word('正常词')
        assert ac.word_count == 1


# ============================================================================
# HitResult —— 表示层
# ============================================================================
class TestHitResult:
    """HitResult repr 输出测试"""

    @pytest.mark.unit
    def test_repr(self):
        """repr 展示 word/action/category 三元信息"""
        h = HitResult('敏感词', 'political', 'block', start=2, end=5)
        r = repr(h)
        assert '敏感词' in r
        assert 'action=block' in r
        assert 'cat=political' in r


# ============================================================================
# SensitiveFilter 配置同步
# ============================================================================
class TestSensitiveFilterConfig:
    """_sync_config_from_settings 配置同步测试"""

    @pytest.mark.unit
    @override_settings(SENSITIVE_FILTER_CHUNK_SIZE='not-an-int')
    def test_sync_config_invalid_value_keeps_defaults(self):
        """settings 配置值类型非法时静默使用默认值（不抛异常）"""
        SensitiveFilter._sync_config_from_settings()
        assert SensitiveFilter.CHUNK_SIZE == 32


# ============================================================================
# Redis 版本号同步与熔断降级
# ============================================================================
class TestSensitiveFilterRedis:
    """_get_redis / _read_redis_version / _incr_redis_version 熔断与降级测试"""

    @pytest.mark.unit
    def test_get_redis_circuit_breaker_active(self):
        """熔断窗口内直接返回 None，不再尝试建连"""
        SensitiveFilter._redis_client = None
        SensitiveFilter._redis_unavailable_until = time_mod.time() + 60
        with patch('redis.Redis') as mock_redis_cls:
            assert SensitiveFilter._get_redis() is None
        mock_redis_cls.assert_not_called()

    @pytest.mark.unit
    def test_get_redis_without_redis_url_uses_env_params(self):
        """REDIS_URL 未配置时用 env 参数建连"""
        SensitiveFilter._redis_client = None
        with override_settings(REDIS_URL=''), \
             patch.dict('os.environ', {
                 'REDIS_DB_HOST': 'my-redis',
                 'REDIS_DB_PORT': '6380',
                 'REDIS_DB_PASSWORD': 'secret',
                 'REDIS_DB_CAPTCHA': '3',
             }), \
             patch('redis.Redis') as mock_redis_cls:
            mock_redis_cls.return_value.ping.return_value = True
            client = SensitiveFilter._get_redis()

        assert client is mock_redis_cls.return_value
        mock_redis_cls.assert_called_once_with(
            host='my-redis', port=6380, password='secret',
            decode_responses=True, db=3)
        mock_redis_cls.return_value.ping.assert_called_once()

    @pytest.mark.unit
    def test_get_redis_connection_failure_sets_breaker(self):
        """建连/握手失败时设置 5s 熔断并返回 None（不报错）

        默认 settings.REDIS_URL 存在，走 Redis.from_url 建连路径。
        """
        SensitiveFilter._redis_client = None
        with patch('redis.Redis') as mock_redis_cls:
            mock_redis_cls.from_url.return_value.ping.side_effect = ConnectionError('redis down')
            assert SensitiveFilter._get_redis() is None
        assert SensitiveFilter._redis_client is None
        assert SensitiveFilter._redis_unavailable_until > time_mod.time()

    @pytest.mark.unit
    def test_read_redis_version_exception_clears_client(self):
        """读取版本号连接异常时清空缓存客户端并返回 0"""
        mock_client = MagicMock()
        mock_client.get.side_effect = ConnectionError('redis down')
        SensitiveFilter._redis_client = mock_client

        assert SensitiveFilter._read_redis_version() == 0
        assert SensitiveFilter._redis_client is None
        assert SensitiveFilter._redis_unavailable_until > time_mod.time()

    @pytest.mark.unit
    def test_incr_redis_version_without_redis_returns_zero(self):
        """Redis 不可用时自增返回 0（不报错）"""
        with patch.object(SensitiveFilter, '_get_redis', return_value=None):
            assert SensitiveFilter._incr_redis_version() == 0

    @pytest.mark.unit
    def test_incr_redis_version_exception_clears_client(self):
        """自增异常时清空缓存客户端并返回 0"""
        mock_client = MagicMock()
        mock_client.incr.side_effect = ConnectionError('redis down')
        SensitiveFilter._redis_client = mock_client

        assert SensitiveFilter._incr_redis_version() == 0
        assert SensitiveFilter._redis_client is None


# ============================================================================
# 单例刷新：TTL / Redis 版本号分歧 / force_reload
# ============================================================================
class TestSensitiveFilterSingletonReload:
    """单例 TTL 刷新与 force_reload 重载行为测试"""

    @pytest.mark.unit
    def test_get_instance_redis_version_read_failure(self):
        """首次初始化时读 Redis 版本号异常不阻断实例创建"""
        mock_words = [_make_sw('违规', 'other', 'block', False)]
        with patch('apps.security.models.SensitiveWord.objects') as mock_manager, \
             patch.object(SensitiveFilter, '_read_redis_version',
                          side_effect=RuntimeError('redis down')):
            mock_manager.filter.return_value.iterator.return_value = mock_words
            sf = SensitiveFilter.get_instance()

        assert sf._version == 1
        assert sf._last_redis_version == 0

    @pytest.mark.unit
    def test_get_instance_reload_on_redis_version_diverged(self):
        """Redis 版本号分歧时立即重载词库（绕过 TTL 守卫）"""
        mock_words = [_make_sw('违规', 'other', 'block', False)]
        with patch('apps.security.models.SensitiveWord.objects') as mock_manager, \
             patch.object(SensitiveFilter, '_get_redis', return_value=None), \
             patch.object(SensitiveFilter, '_read_redis_version', side_effect=[0, 5]):
            mock_manager.filter.return_value.iterator.return_value = mock_words
            sf = SensitiveFilter.get_instance()
            assert sf._version == 1
            # 第二次调用：Redis 版本号 5 与本地 0 分歧 → 强制重载
            sf2 = SensitiveFilter.get_instance()

        assert sf2 is sf
        assert sf._version == 2
        assert sf._last_redis_version == 5

    @pytest.mark.unit
    def test_force_reload_with_instance_updates_version(self):
        """force_reload 重载本进程词库并同步 Redis 版本号"""
        mock_words = [_make_sw('违规', 'other', 'block', False)]
        # force_reload 内部会再次 _load_from_db，故 DB mock 需全程保持生效
        with patch('apps.security.models.SensitiveWord.objects') as mock_manager, \
             patch.object(SensitiveFilter, '_get_redis', return_value=None), \
             patch.object(SensitiveFilter, '_incr_redis_version', return_value=7):
            mock_manager.filter.return_value.iterator.return_value = mock_words
            sf = SensitiveFilter.get_instance()
            SensitiveFilter.force_reload()

        assert sf._version == 2
        assert sf._last_redis_version == 7

    @pytest.mark.unit
    def test_force_reload_incr_failure_keeps_local_version(self):
        """force_reload 时 Redis 自增失败不影响本进程重载"""
        mock_words = [_make_sw('违规', 'other', 'block', False)]
        with patch('apps.security.models.SensitiveWord.objects') as mock_manager, \
             patch.object(SensitiveFilter, '_get_redis', return_value=None), \
             patch.object(SensitiveFilter, '_incr_redis_version',
                          side_effect=RuntimeError('redis down')):
            mock_manager.filter.return_value.iterator.return_value = mock_words
            sf = SensitiveFilter.get_instance()
            SensitiveFilter.force_reload()  # 不应抛出

        assert sf._version == 2
        assert sf._last_redis_version == 0

    @pytest.mark.unit
    def test_force_reload_before_instance_incr_only(self):
        """单例未创建时 force_reload 仅自增版本号"""
        SensitiveFilter._instance = None
        with patch.object(SensitiveFilter, '_incr_redis_version') as mock_incr:
            SensitiveFilter.force_reload()
        mock_incr.assert_called_once()

    @pytest.mark.unit
    def test_maybe_reload_within_ttl_noop(self):
        """TTL 窗口内 _maybe_reload 不触发重载"""
        sf = _build_filter([])
        sf._maybe_reload()
        assert sf._version == 1

    @pytest.mark.unit
    def test_maybe_reload_ttl_expired_double_check(self):
        """TTL 过期后第一次检查通过、锁内二次检查发现未过期 → 不重载"""
        sf = _build_filter([])
        t0 = time_mod.time()
        with patch('apps.security.sensitive_filter.time.time',
                   side_effect=[t0 + 400, t0 + 100]):
            sf._maybe_reload()
        assert sf._version == 1

    @pytest.mark.unit
    def test_force_reload_local_same_version_noop(self):
        """版本号未分歧时 _force_reload_local 直接返回，不重载"""
        sf = _build_filter([])
        assert sf._last_redis_version == 0
        sf._force_reload_local(0)
        assert sf._version == 1


# ============================================================================
# _load_from_db 异常数据容错
# ============================================================================
class TestSensitiveFilterLoad:
    """_load_from_db 加载异常数据容错测试"""

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_load_skips_invalid_regex(self, _mock_enabled):
        """非法正则词跳过并记 warning，不影响其余词加载"""
        mock_words = [
            _make_sw('[', 'other', 'mask', True),  # 非法正则
            _make_sw('正常词', 'other', 'block', False),
        ]
        with patch('apps.security.models.SensitiveWord.objects') as mock_manager, \
             patch.object(SensitiveFilter, '_get_redis', return_value=None):
            mock_manager.filter.return_value.iterator.return_value = mock_words
            sf = SensitiveFilter.get_instance()

        assert sf._version == 1
        assert sf._ac.word_count == 1
        # 正常词仍可命中
        hits = sf.check('含正常词')
        assert len(hits) == 1
        assert hits[0].action == 'block'


# ============================================================================
# SensitiveFilter.check —— 全量审查
# ============================================================================
class TestSensitiveFilterCheck:
    """SensitiveFilter.check 全量审查测试"""

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_check_block(self, _mock_enabled):
        """命中 block 词时返回 HitResult(action='block')"""
        sf = _build_filter([('违规词', 'other', 'block', False)])
        hits = sf.check('这段话包含违规词内容')
        assert len(hits) == 1
        assert hits[0].action == 'block'
        assert hits[0].word == '违规词'

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_check_mask(self, _mock_enabled):
        """命中 mask 词时返回 HitResult(action='mask')"""
        sf = _build_filter([('脱敏词', 'other', 'mask', False)])
        hits = sf.check('这里有一个脱敏词')
        assert len(hits) == 1
        assert hits[0].action == 'mask'
        assert hits[0].word == '脱敏词'

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_check_warn(self, _mock_enabled):
        """命中 warn 词时返回 HitResult(action='warn')"""
        sf = _build_filter([('警告词', 'other', 'warn', False)])
        hits = sf.check('这里有一个警告词')
        assert len(hits) == 1
        assert hits[0].action == 'warn'
        assert hits[0].word == '警告词'

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=False)
    def test_check_disabled(self, _mock_enabled):
        """审查关闭（_is_enabled=False）时 check 返回空列表

        显式 mock _is_enabled 而非依赖全局 settings：不同环境（test_settings 默认关闭 /
        生产 settings 默认开启）下测试行为保持确定，不受 DJANGO_SETTINGS_MODULE 覆盖影响。
        """
        sf = _build_filter([('违规词', 'other', 'block', False)])
        hits = sf.check('这段话包含违规词内容')
        assert hits == []

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_check_empty(self, _mock_enabled):
        """check('') 返回空列表，不扫描"""
        sf = _build_filter([('违规词', 'other', 'block', False)])
        assert sf.check('') == []


# ============================================================================
# SensitiveFilter.feed / flush —— 流式增量审查
# ============================================================================
class TestSensitiveFilterFeed:
    """SensitiveFilter.feed / flush 流式审查测试"""

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_feed_accumulate(self, _mock_enabled):
        """delta 不足 CHUNK_SIZE 且无分隔符时累积到 buffer，返回 ([], None)

        关键词可能跨 delta 边界，过早下发会漏审，故短片段暂存。
        """
        sf = _build_filter([('违规', 'other', 'block', False)])
        state = sf.new_state()
        # 3 字符，无分隔符，远小于默认 CHUNK_SIZE=32
        outputs, hit = sf.feed(state, 'abc')
        assert outputs == []
        assert hit is None
        # 内容应累积到 buffer
        assert state['buffer'] == 'abc'

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_feed_with_separator(self, _mock_enabled):
        """delta 含分隔符时触发审查并下发安全内容

        分隔符（句号/换行等）强制送审，避免长缓冲导致延迟。
        审查后保留尾部 WINDOW_SIZE 字符防跨边界，其余下发。
        """
        sf = _build_filter([])  # 无敏感词，审查不命中
        state = sf.new_state()
        # 25 字符含句号分隔符，超过 WINDOW_SIZE(16)，审查后应下发部分内容
        delta = 'abcdefghij。klmnopqrstuvwx'
        outputs, hit = sf.feed(state, delta)
        assert hit is None
        # 应有部分内容下发（总长 25 > WINDOW_SIZE 16，下发前 9 字符）
        assert len(outputs) > 0
        # 下发内容 + buffer 残留 = 原始 delta
        assert ''.join(outputs) + state['buffer'] == delta

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_feed_block(self, _mock_enabled):
        """feed 命中 block 词时返回 ([], HitResult)，不下发任何内容

        block 是最高优先级：立即中断流，前端清空已展示内容。
        """
        sf = _build_filter([('违规', 'other', 'block', False)])
        state = sf.new_state()
        # 含分隔符触发审查，命中 block
        outputs, hit = sf.feed(state, '这是违规。')
        assert outputs == []
        assert hit is not None
        assert hit.action == 'block'
        assert hit.word == '违规'

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_feed_mask(self, _mock_enabled):
        """feed 命中 mask 词时下发脱敏后内容（敏感词替换为 ***）"""
        sf = _build_filter([('秘密', 'secret', 'mask', False)])
        state = sf.new_state()
        # 构造超过 CHUNK_SIZE 的文本，确保触发审查
        delta = '这是秘密内容' + 'x' * 40
        outputs, hit = sf.feed(state, delta)
        assert hit is None
        # 下发内容中不应出现原始敏感词
        combined = ''.join(outputs)
        assert '秘密' not in combined
        # 下发内容中应包含脱敏替换符
        assert '***' in combined

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_flush_remaining(self, _mock_enabled):
        """flush 下发 buffer 中残余的安全内容

        流结束时必须调用 flush，否则尾部内容会滞留在 buffer 中丢失。
        """
        sf = _build_filter([('违规', 'other', 'block', False)])
        state = sf.new_state()
        # 先 feed 一个短片段，累积到 buffer（不足 CHUNK_SIZE，无分隔符）
        sf.feed(state, '安全内容')
        assert state['buffer'] == '安全内容'
        # flush 下发残余
        outputs, hit = sf.flush(state)
        assert hit is None
        assert outputs == ['安全内容']
        assert state['buffer'] == ''

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_flush_empty(self, _mock_enabled):
        """buffer 为空时 flush 返回 ([], None)"""
        sf = _build_filter([('违规', 'other', 'block', False)])
        state = sf.new_state()
        outputs, hit = sf.flush(state)
        assert outputs == []
        assert hit is None

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=False)
    def test_disabled_passthrough(self, _mock_enabled):
        """审查关闭时 feed 透传 delta（先把 buffer 残余下发，再透传 delta）

        显式 mock _is_enabled 而非依赖全局 settings：不同环境（test_settings 默认关闭 /
        生产 settings 默认开启）下测试行为保持确定，不受 DJANGO_SETTINGS_MODULE 覆盖影响。
        关闭审查不能影响正常输出，delta 原样下发。
        """
        sf = _build_filter([('违规', 'other', 'block', False)])
        state = sf.new_state()
        outputs, hit = sf.feed(state, '正常内容')
        assert hit is None
        assert outputs == ['正常内容']

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_feed_empty_delta(self, _mock_enabled):
        """空 delta 直接返回 ([], None)，不修改 buffer"""
        sf = _build_filter([('违规', 'other', 'block', False)])
        state = sf.new_state()
        outputs, hit = sf.feed(state, '')
        assert outputs == []
        assert hit is None
        assert state['buffer'] == ''

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_feed_mask_result_below_window_kept_in_buffer(self, _mock_enabled):
        """脱敏后文本不足窗口长度时留在 buffer 暂不下发"""
        sf = _build_filter([('秘密', 'secret', 'mask', False)])
        state = sf.new_state()
        # 分隔符触发审查；脱敏后 '***。' 长度 4 <= WINDOW_SIZE 16
        outputs, hit = sf.feed(state, '秘密。')
        assert hit is None
        assert outputs == []
        assert state['buffer'] == '***。'

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=False)
    def test_flush_disabled_drains_buffer(self, _mock_enabled):
        """审查关闭时 flush 下发 buffer 残余内容"""
        sf = _build_filter([])
        state = sf.new_state()
        state['buffer'] = '残余内容'
        outputs, hit = sf.flush(state)
        assert hit is None
        assert outputs == ['残余内容']
        assert state['buffer'] == ''

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_check_regex_hit(self, _mock_enabled):
        """正则词（手机号）通过 re 模块命中并带 start/end"""
        sf = _build_filter([(r'1[3-9]\d{9}', 'phone', 'mask', True)])
        hits = sf.check('联系电话13812345678')
        assert len(hits) == 1
        assert hits[0].word == r'1[3-9]\d{9}'
        assert hits[0].category == 'phone'
        assert hits[0].action == 'mask'
        assert hits[0].start == 4  # '联系电话' 4 个字符
        assert hits[0].end == 15

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_feed_warn_records_hit(self, _mock_enabled):
        """warn 命中不影响下发，仅记录到 warn_hits 供审计"""
        sf = _build_filter([('提醒', 'other', 'warn', False)])
        state = sf.new_state()
        outputs, hit = sf.feed(state, '这里有提醒。')
        assert hit is None
        assert len(state['warn_hits']) == 1
        assert state['warn_hits'][0].word == '提醒'


# ============================================================================
# get_sensitive_filter —— 便捷函数
# ============================================================================
class TestGetSensitiveFilter:
    """get_sensitive_filter 便捷函数测试"""

    @pytest.mark.unit
    def test_returns_instance(self):
        """get_sensitive_filter 返回 SensitiveFilter 实例"""
        sf = get_sensitive_filter()
        assert isinstance(sf, SensitiveFilter)


# ============================================================================
# 敏感词命中统计 —— _record_hits / flush_hit_stats_to_db
# ============================================================================
class TestSensitiveHitStats:
    """命中计数：_scan 记录到 Redis 哈希，管理端 flush 落库到 SensitiveWord.hit_count"""

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_record_hits_aggregates_by_word(self, _mock_enabled):
        """同词多次命中按词聚合后通过 pipeline 一次写入 Redis"""
        mock_pipe = MagicMock()
        mock_r = MagicMock()
        mock_r.pipeline.return_value = mock_pipe
        with patch.object(SensitiveFilter, '_get_redis', return_value=mock_r):
            sf = _build_filter([('违规', 'other', 'block', False)])
            hits = sf.check('违规违规，这里又违规')
            assert len(hits) == 3
        # 聚合后仅一条 hincrby 命令：词 -> 3
        mock_pipe.hincrby.assert_called_once_with(
            SensitiveFilter._HIT_STATS_KEY, '违规', 3)
        mock_pipe.execute.assert_called_once()

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_record_hits_skipped_when_redis_unavailable(self, _mock_enabled):
        """Redis 不可用（_get_redis 返回 None）时命中计数静默降级，不抛异常"""
        with patch.object(SensitiveFilter, '_get_redis', return_value=None):
            sf = _build_filter([('违规', 'other', 'block', False)])
            hits = sf.check('包含违规词')
        assert len(hits) == 1  # 审查结果不受统计降级影响

    @pytest.mark.unit
    @patch.object(SensitiveFilter, '_is_enabled', return_value=True)
    def test_record_hits_pipeline_exception_swallowed(self, _mock_enabled):
        """Redis 执行异常时计数丢弃并熔断，审查主流程不受影响"""
        mock_r = MagicMock()
        mock_r.pipeline.return_value.execute.side_effect = ConnectionError('redis down')
        with patch.object(SensitiveFilter, '_get_redis', return_value=mock_r):
            sf = _build_filter([('违规', 'other', 'block', False)])
            hits = sf.check('包含违规词')
        assert len(hits) == 1
        assert SensitiveFilter._redis_client is None

    @pytest.mark.integration
    @pytest.mark.django_db
    def test_flush_hit_stats_to_db_accumulates_and_cleans(self):
        """flush 将 Redis 计数原子累加到 DB hit_count，并清理已落库的 Redis 字段"""
        from apps.security.models import SensitiveWord
        from apps.security.sensitive_filter import flush_hit_stats_to_db

        sw = SensitiveWord.objects.create(
            word='统计词', category='other', action='mask', hit_count=2)

        mock_r = MagicMock()
        # 已删除的词也会出现在计数里：不落库但需清理，防止字段残留
        mock_r.hgetall.return_value = {'统计词': '3', '已删除词': '5'}
        with patch.object(SensitiveFilter, '_get_redis', return_value=mock_r):
            flush_hit_stats_to_db()

        sw.refresh_from_db()
        assert sw.hit_count == 5  # 2 + 3
        mock_r.hdel.assert_called_once_with(
            SensitiveFilter._HIT_STATS_KEY, '统计词', '已删除词')

    @pytest.mark.integration
    @pytest.mark.django_db
    def test_flush_hit_stats_noop_when_redis_empty(self):
        """Redis 无计数时 flush 直接返回，不产生 DB 写入"""
        from apps.security.models import SensitiveWord
        from apps.security.sensitive_filter import flush_hit_stats_to_db

        sw = SensitiveWord.objects.create(
            word='无命中词', category='other', action='mask', hit_count=1)

        mock_r = MagicMock()
        mock_r.hgetall.return_value = {}
        with patch.object(SensitiveFilter, '_get_redis', return_value=mock_r):
            flush_hit_stats_to_db()

        sw.refresh_from_db()
        assert sw.hit_count == 1
        mock_r.hdel.assert_not_called()
