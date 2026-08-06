"""
apps.memory.short_term 单元测试 —— Redis LIST 短时记忆

覆盖范围：
- _key：key 格式与 session_id 拼接
- _get_client：连接缓存 + 失败降级返回 None
- get_turns：正常返回 / 空数据 / Redis 异常降级空列表
- append_turn：pipeline 三连（rpush + ltrim + expire）+ 异常吞掉
- clear：delete 调用 + 异常吞掉

短时记忆走 Redis 不入库，读写快但依赖网络，
所有异常路径必须降级为空/静默，避免拖垮主问答流程。
"""
import json

import pytest
from unittest.mock import patch, MagicMock

from apps.memory.short_term import ShortTermMemory


# ============================================================================
# _key —— key 格式稳定性（缓存定位依赖 key 拼接稳定）
# ============================================================================
class TestKeyFormat:
    """_key 格式测试"""

    @pytest.mark.unit
    def test_key_format(self):
        """key 必须为 short_term:sess:{id} 格式，读写与清理按此定位"""
        stm = ShortTermMemory()
        assert stm._key(1) == 'short_term:sess:1'
        assert stm._key(42) == 'short_term:sess:42'


# ============================================================================
# _get_client —— Redis 连接缓存 + 失败降级
# ============================================================================
class TestGetClient:
    """_get_client 连接缓存与失败降级测试"""

    @pytest.mark.unit
    def test_get_client_cached(self):
        """第二次调用应复用第一次缓存的连接，避免重复建连"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            first = stm._get_client()
            second = stm._get_client()
            assert first is fake_conn
            assert second is fake_conn
            # 连接缓存后只调用一次 get_redis_connection
            mock_get_conn.assert_called_once_with('default')

    @pytest.mark.unit
    def test_get_client_failure(self):
        """get_redis_connection 抛异常时返回 None，不拖垮调用方"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            mock_get_conn.side_effect = RuntimeError('redis down')
            stm = ShortTermMemory()
            result = stm._get_client()
            assert result is None
            # 失败后 _client 仍为 None，下次调用会重试（不缓存 None）


# ============================================================================
# get_turns —— 读取短时记忆轮次
# ============================================================================
class TestGetTurns:
    """get_turns 读取与降级测试"""

    @pytest.mark.unit
    def test_get_turns_success(self):
        """LRANGE 返回的 JSON 字符串解析为 dict 列表"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            raw_turns = [
                json.dumps({'question': 'Q1', 'answer': 'A1'}),
                json.dumps({'question': 'Q2', 'answer': 'A2'}),
            ]
            fake_conn.lrange.return_value = raw_turns
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            result = stm.get_turns(7)
            assert result == [
                {'question': 'Q1', 'answer': 'A1'},
                {'question': 'Q2', 'answer': 'A2'},
            ]
            fake_conn.lrange.assert_called_once_with('short_term:sess:7', 0, -1)

    @pytest.mark.unit
    def test_get_turns_empty(self):
        """无数据时返回空列表"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            fake_conn.lrange.return_value = []
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            assert stm.get_turns(7) == []

    @pytest.mark.unit
    def test_get_turns_redis_error(self):
        """Redis 异常时降级返回空列表，不抛异常"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            fake_conn.lrange.side_effect = RuntimeError('conn lost')
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            assert stm.get_turns(7) == []

    @pytest.mark.unit
    def test_get_turns_no_client(self):
        """连接获取失败（返回 None）时直接返回空列表"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            mock_get_conn.side_effect = RuntimeError('redis down')
            stm = ShortTermMemory()
            assert stm.get_turns(7) == []


# ============================================================================
# append_turn —— 写入一轮对话（rpush + ltrim + expire pipeline）
# ============================================================================
class TestAppendTurn:
    """append_turn pipeline 写入与降级测试"""

    @pytest.mark.unit
    def test_append_turn_pipeline(self):
        """正常路径应在一个 pipeline 内执行 rpush/ltrim/expire"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            pipe = MagicMock()
            fake_conn.pipeline.return_value = pipe
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            stm.append_turn(7, '你好', '世界')
            # pipeline 应被创建并执行
            fake_conn.pipeline.assert_called_once()
            pipe.execute.assert_called_once()
            # rpush 写入 key + JSON 串
            rpush_args = pipe.rpush.call_args[0]
            assert rpush_args[0] == 'short_term:sess:7'
            payload = json.loads(rpush_args[1])
            assert payload == {'question': '你好', 'answer': '世界'}
            # ltrim 控制最大轮数（保留尾部 max_turns 条）
            pipe.ltrim.assert_called_once_with('short_term:sess:7', -stm.max_turns, -1)
            # expire 设置 TTL
            pipe.expire.assert_called_once_with('short_term:sess:7', stm.ttl)

    @pytest.mark.unit
    def test_append_turn_redis_error(self):
        """pipeline 执行异常被吞掉，不抛异常影响主流程"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            pipe = MagicMock()
            pipe.execute.side_effect = RuntimeError('redis down')
            fake_conn.pipeline.return_value = pipe
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            # 不应抛异常
            stm.append_turn(7, 'Q', 'A')

    @pytest.mark.unit
    def test_append_turn_no_client(self):
        """连接获取失败时静默返回，不抛异常"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            mock_get_conn.side_effect = RuntimeError('redis down')
            stm = ShortTermMemory()
            stm.append_turn(7, 'Q', 'A')


# ============================================================================
# clear —— 清空短时记忆
# ============================================================================
class TestClear:
    """clear 清空与降级测试"""

    @pytest.mark.unit
    def test_clear(self):
        """clear 应对对应 key 调用 delete"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            stm.clear(9)
            fake_conn.delete.assert_called_once_with('short_term:sess:9')

    @pytest.mark.unit
    def test_clear_redis_error(self):
        """delete 异常被吞掉，不抛异常"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            fake_conn = MagicMock()
            fake_conn.delete.side_effect = RuntimeError('redis down')
            mock_get_conn.return_value = fake_conn
            stm = ShortTermMemory()
            # 不应抛异常
            stm.clear(9)

    @pytest.mark.unit
    def test_clear_no_client(self):
        """连接获取失败时静默返回，不抛异常"""
        with patch('apps.memory.short_term.get_redis_connection') as mock_get_conn:
            mock_get_conn.side_effect = RuntimeError('redis down')
            stm = ShortTermMemory()
            stm.clear(9)
