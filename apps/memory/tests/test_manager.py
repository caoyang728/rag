"""
apps.memory.manager 单元测试 —— 四层记忆管理器

覆盖范围：
- estimate_tokens：中文/英文/混合/空/None 的 token 估算
- truncate_text：短文本原样 / 长文本截断 / 空文本
- MemoryManager.__init__：默认 budget 取自 settings
- load_context：拼装流程调用与异常隔离
- _load_global：类级缓存（5min 内复用）
- _load_user：已认证/未认证/无记忆
- _load_session：有摘要带实体 / 无记忆
- _assemble：全部分块 / 全空 / 超 50% 预算截断
- append_turn：turn_count 自增 / 每 5 轮触发提炼任务

记忆管理器是问答上下文拼装的关键路径，各层加载失败必须隔离，
缓存命中与 token 截断需独立验证，不耦合真实 DB 与 Redis。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.memory import manager
from apps.memory.manager import MemoryManager, estimate_tokens, truncate_text
from apps.memory.models import SessionMemory


# ============================================================================
# estimate_tokens —— 精确 token 估算（中文 1.5 / 英文 4 字符 ≈ 1 token）
# ============================================================================
class TestEstimateTokens:
    """estimate_tokens 估算测试"""

    @pytest.mark.unit
    def test_estimate_tokens_empty(self):
        """空字符串 token 为 0"""
        assert estimate_tokens('') == 0

    @pytest.mark.unit
    def test_estimate_tokens_chinese(self):
        """纯中文：每字 ≈ 1.5 token，4 字 = int(4*1.5 + 0//4 + 1) = 7"""
        assert estimate_tokens('你好世界') == 7

    @pytest.mark.unit
    def test_estimate_tokens_english(self):
        """纯英文：4 字符 ≈ 1 token，5 字符 = int(5//4 + 1) = 2"""
        assert estimate_tokens('hello') == 2

    @pytest.mark.unit
    def test_estimate_tokens_mixed(self):
        """中英混合：2 中文 + 5 英文 = int(2*1.5 + 5//4 + 1) = 5"""
        assert estimate_tokens('你好hello') == 5

    @pytest.mark.unit
    def test_estimate_tokens_none(self):
        """None 输入按 0 处理（falsy 走 not text 分支）"""
        assert estimate_tokens(None) == 0


# ============================================================================
# truncate_text —— 安全截断，不切断中文
# ============================================================================
class TestTruncateText:
    """truncate_text 截断测试"""

    @pytest.mark.unit
    def test_truncate_text_short(self):
        """短文本不超预算时原样返回"""
        text = 'short text'
        assert truncate_text(text, max_tokens=100) == text

    @pytest.mark.unit
    def test_truncate_text_long(self):
        """长文本超预算时截断并以 ... 收尾"""
        text = 'abcdefghijklmnopqrstuvwxyz'  # 26 字符
        # max_tokens=5 -> max_chars=10，截断后追加 ...
        result = truncate_text(text, max_tokens=5)
        assert result.endswith('...')
        # 截断后比原文短
        assert len(result) < len(text)
        # 头部内容应保留
        assert result.startswith('abcdefghij')

    @pytest.mark.unit
    def test_truncate_text_empty(self):
        """空文本原样返回"""
        assert truncate_text('', max_tokens=10) == ''


# ============================================================================
# MemoryManager.__init__ —— 默认 budget 来源
# ============================================================================
class TestMemoryManagerInit:
    """__init__ 初始化测试"""

    @pytest.mark.unit
    def test_memory_manager_init_default_budget(self):
        """未传 budget 时取 settings.MEMORY_TOKEN_BUDGET"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            from django.conf import settings
            assert mm.budget == settings.MEMORY_TOKEN_BUDGET

    @pytest.mark.unit
    def test_memory_manager_init_custom_budget(self):
        """显式传入 budget 时使用传入值"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager(budget=4096)
            assert mm.budget == 4096


# ============================================================================
# load_context —— 拼装主流程与异常隔离
# ============================================================================
class TestLoadContext:
    """load_context 拼装与异常隔离测试"""

    @pytest.mark.unit
    def test_load_context_assembles_parts(self):
        """各 _load_* 返回值应汇总到 parts 并交给 _assemble 拼装"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            mm._load_global = MagicMock(return_value='全局内容')
            mm._load_user = MagicMock(return_value='用户画像')
            mm._load_session = MagicMock(return_value='会话摘要')
            mm.short_term.get_turns = MagicMock(return_value=[
                {'question': 'Q1', 'answer': 'A1'},
            ])
            # 用 spy 监听 _assemble 调用，返回固定串避免依赖真实拼装
            with patch.object(mm, '_assemble', return_value='ASSEMBLED') as mock_asm:
                session = MagicMock()
                session.id = 1
                result = mm.load_context(user=MagicMock(), session=session,
                                          question='Q', root_type='company_doc')
            assert result['memory_block'] == 'ASSEMBLED'
            mock_asm.assert_called_once()
            parts = mock_asm.call_args[0][0]
            assert parts['global'] == '全局内容'
            assert parts['user'] == '用户画像'
            assert parts['session'] == '会话摘要'
            assert parts['short_term'] == [{'question': 'Q1', 'answer': 'A1'}]

    @pytest.mark.unit
    def test_load_context_exception_safe(self):
        """单个 _load_* 抛异常不应拖垮整体，对应 part 保持为空兜底"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            # _load_global 抛异常，其余正常
            mm._load_global = MagicMock(side_effect=RuntimeError('db down'))
            mm._load_user = MagicMock(return_value='用户画像')
            mm._load_session = MagicMock(return_value='会话摘要')
            mm.short_term.get_turns = MagicMock(return_value=[])
            session = MagicMock()
            session.id = 1
            result = mm.load_context(user=MagicMock(), session=session, question='Q')
            # 异常被捕获，global 回落到空串
            assert result['parts']['global'] == ''
            assert result['parts']['user'] == '用户画像'
            # memory_block 仍被拼装出来
            assert isinstance(result['memory_block'], str)


# ============================================================================
# _load_global —— 全局记忆（类级 5min 缓存）
# ============================================================================
class TestLoadGlobal:
    """_load_global 缓存与作用域过滤测试"""

    def _reset_class_cache(self):
        """每个用例前重置类级缓存，避免用例间互相污染"""
        MemoryManager._global_cache = {}
        MemoryManager._global_cache_time = 0

    @pytest.mark.unit
    def test_load_global_with_cache(self):
        """300s 内第二次调用应命中类级缓存，不再查 DB"""
        self._reset_class_cache()
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.manager.GlobalMemory') as mock_gm:
            gm1 = MagicMock()
            gm1.content = '规则一'
            gm1.scope_root_types = []
            # filter(...).order_by(...) 返回可迭代列表
            mock_gm.objects.filter.return_value.order_by.return_value = [gm1]
            mm = MemoryManager()
            first = mm._load_global('company_doc')
            assert '规则一' in first
            # 第一次应查 DB
            assert mock_gm.objects.filter.call_count == 1
            # 第二次应命中缓存，不再查 DB
            second = mm._load_global('company_doc')
            assert second == first
            assert mock_gm.objects.filter.call_count == 1

    @pytest.mark.unit
    def test_load_global_scope_filter(self):
        """scope_root_types 限定作用域：不在范围内的全局记忆被过滤"""
        self._reset_class_cache()
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.manager.GlobalMemory') as mock_gm:
            gm_in = MagicMock()
            gm_in.content = '适用规则'
            gm_in.scope_root_types = ['company_doc']
            gm_out = MagicMock()
            gm_out.content = '不适用规则'
            gm_out.scope_root_types = ['wiki_doc']
            mock_gm.objects.filter.return_value.order_by.return_value = [gm_in, gm_out]
            mm = MemoryManager()
            result = mm._load_global('company_doc')
            assert '适用规则' in result
            assert '不适用规则' not in result

    @pytest.mark.unit
    def test_load_global_all_scope_passes(self):
        """scope_root_types 含 'all' 时所有根类型都命中"""
        self._reset_class_cache()
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.manager.GlobalMemory') as mock_gm:
            gm_all = MagicMock()
            gm_all.content = '通用规则'
            gm_all.scope_root_types = ['all']
            mock_gm.objects.filter.return_value.order_by.return_value = [gm_all]
            mm = MemoryManager()
            result = mm._load_global('wiki_doc')
            assert '通用规则' in result


# ============================================================================
# _load_user —— 用户长期记忆
# ============================================================================
class TestLoadUser:
    """_load_user 用户画像加载测试"""

    @pytest.mark.unit
    def test_load_user_authenticated(self):
        """已认证用户且存在 UserMemory：返回 profile_text"""
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.manager.UserMemory') as mock_um:
            um = MagicMock()
            um.profile_text = '该用户偏好简洁回答'
            mock_um.objects.filter.return_value.first.return_value = um
            mm = MemoryManager()
            user = MagicMock()
            user.is_authenticated = True
            assert mm._load_user(user) == '该用户偏好简洁回答'

    @pytest.mark.unit
    def test_load_user_unauthenticated(self):
        """未登录用户直接返回空串"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            user = MagicMock()
            user.is_authenticated = False
            assert mm._load_user(user) == ''

    @pytest.mark.unit
    def test_load_user_no_memory(self):
        """已认证但未建 UserMemory 记录：返回空串"""
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.manager.UserMemory') as mock_um:
            mock_um.objects.filter.return_value.first.return_value = None
            mm = MemoryManager()
            user = MagicMock()
            user.is_authenticated = True
            assert mm._load_user(user) == ''

    @pytest.mark.unit
    def test_load_user_empty_profile(self):
        """UserMemory 存在但 profile_text 为空：返回空串"""
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.manager.UserMemory') as mock_um:
            um = MagicMock()
            um.profile_text = ''
            mock_um.objects.filter.return_value.first.return_value = um
            mm = MemoryManager()
            user = MagicMock()
            user.is_authenticated = True
            assert mm._load_user(user) == ''


# ============================================================================
# _load_session —— 会话摘要
# ============================================================================
class TestLoadSession:
    """_load_session 会话摘要加载测试"""

    @pytest.mark.unit
    def test_load_session_with_summary(self):
        """存在摘要时返回 summary + 关键实体拼接"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            session = MagicMock()
            session.memory.summary = '讨论了报销流程'
            session.memory.entities = ['报销', '财务']
            result = mm._load_session(session)
            assert '讨论了报销流程' in result
            assert '关键实体' in result
            assert '报销' in result
            assert '财务' in result

    @pytest.mark.unit
    def test_load_session_no_memory(self):
        """session.memory 抛 DoesNotExist 时返回空串"""
        from unittest.mock import PropertyMock
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            # OneToOneField 反向访问通过描述符实现，普通 MagicMock 的属性访问
            # 不会触发 side_effect，必须用 PropertyMock 模拟属性读取抛异常。
            # 使用独立子类避免污染全局 MagicMock 类（其他用例仍依赖自动属性）。
            SessionMock = type('SessionMock', (MagicMock,), {})
            session = SessionMock()
            type(session).memory = PropertyMock(side_effect=SessionMemory.DoesNotExist())
            assert mm._load_session(session) == ''

    @pytest.mark.unit
    def test_load_session_empty_summary(self):
        """摘要为空时返回空串（无内容可拼）"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            session = MagicMock()
            session.memory.summary = ''
            session.memory.entities = []
            assert mm._load_session(session) == ''


# ============================================================================
# _assemble —— 分层拼装 + token 截断
# ============================================================================
class TestAssemble:
    """_assemble 拼装与截断测试"""

    @pytest.mark.unit
    def test_assemble_all_parts(self):
        """四层记忆齐全时按层级拼装并带各层标题头"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            parts = {
                'global': '全局规则',
                'user': '用户画像',
                'session': '会话摘要',
                'short_term': [{'question': 'Q1', 'answer': 'A1'}],
            }
            result = mm._assemble(parts)
            assert '【全局记忆 · 公司规则】' in result
            assert '全局规则' in result
            assert '【用户画像】' in result
            assert '用户画像' in result
            assert '【会话摘要】' in result
            assert '会话摘要' in result
            assert '【最近对话】' in result
            assert 'Q1' in result and 'A1' in result

    @pytest.mark.unit
    def test_assemble_empty_parts(self):
        """所有分块为空时返回空串"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            parts = {'global': '', 'user': '', 'session': '', 'short_term': []}
            assert mm._assemble(parts) == ''

    @pytest.mark.unit
    def test_assemble_truncation(self):
        """总 token 超 50% 预算时触发截断，结果长度小于完整拼装"""
        with patch('apps.memory.manager.ShortTermMemory'):
            # 极小预算：max_mem = int(10 * 0.5) = 5
            mm = MemoryManager(budget=10)
            # 提供足够长的全局内容以超过 5 token
            long_global = '规则' * 50
            parts = {
                'global': long_global,
                'user': '',
                'session': '',
                'short_term': [],
            }
            result = mm._assemble(parts)
            # 截断后必然包含 ... 收尾标记
            assert '...' in result
            # 完整拼装串
            full = '【全局记忆 · 公司规则】\n' + long_global
            assert len(result) < len(full)


# ============================================================================
# append_turn —— 记录对话 + 每 5 轮触发提炼
# ============================================================================
class TestAppendTurn:
    """append_turn 计数与提炼触发测试"""

    @pytest.mark.unit
    def test_append_turn_increments_count(self):
        """每次追加对话应将 session.turn_count +1 并 save 指定字段"""
        with patch('apps.memory.manager.ShortTermMemory'):
            mm = MemoryManager()
            mm.short_term = MagicMock()
            session = MagicMock()
            session.turn_count = 3
            mm.append_turn(session, 'Q', 'A')
            assert session.turn_count == 4
            session.save.assert_called_once()
            # 只更新 turn_count 与 last_active_at，避免全字段写
            assert set(session.save.call_args[1]['update_fields']) == {'turn_count', 'last_active_at'}

    @pytest.mark.unit
    def test_append_turn_triggers_refine(self):
        """每 5 轮触发一次 refine_session_memory 异步任务"""
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.tasks.refine_session_memory') as mock_refine:
            mm = MemoryManager()
            mm.short_term = MagicMock()
            session = MagicMock()
            session.id = 99
            session.turn_count = 4  # +1 后为 5，触发提炼
            mm.append_turn(session, 'Q', 'A')
            mock_refine.delay.assert_called_once_with(99)

    @pytest.mark.unit
    def test_append_turn_not_multiple_of_five_no_refine(self):
        """非 5 的倍数轮次不触发提炼任务"""
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.tasks.refine_session_memory') as mock_refine:
            mm = MemoryManager()
            mm.short_term = MagicMock()
            session = MagicMock()
            session.id = 99
            session.turn_count = 2  # +1 后为 3，不触发
            mm.append_turn(session, 'Q', 'A')
            mock_refine.delay.assert_not_called()

    @pytest.mark.unit
    def test_append_turn_refine_error_swallowed(self):
        """refine_session_memory.delay 异常被吞掉，不抛给调用方"""
        with patch('apps.memory.manager.ShortTermMemory'), \
             patch('apps.memory.tasks.refine_session_memory') as mock_refine:
            mm = MemoryManager()
            mm.short_term = MagicMock()
            session = MagicMock()
            session.id = 99
            session.turn_count = 4
            mock_refine.delay.side_effect = RuntimeError('broker down')
            # 不应抛异常
            mm.append_turn(session, 'Q', 'A')
            assert session.turn_count == 5
