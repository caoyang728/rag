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
import pytest
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
        return SensitiveFilter.get_instance()


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
