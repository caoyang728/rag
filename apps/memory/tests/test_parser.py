"""
apps.memory.parser 单元测试 —— LLM 响应解析器（多层校验机制）

覆盖范围：
- extract_json：空输入 / 代码块围栏剥离 / 附带头尾说明文字 / 无 JSON
- parse_with_schema：合法 JSON / JSON 语法错误 / Schema 校验失败 / 空输入
- Pydantic Schema 默认值与边界约束（max_length / max_items）
- llm_with_retry：一次成功 / 重试后成功（消息列表替换不增长）/ 全部失败 /
  温度逐次衰减 / max_retries=1 / 缺 content 字段 / llm.chat 异常向上传播

全部使用 mock 的 llm 对象，不依赖真实 LLM 与 DB。
"""
import json

import pytest
from unittest.mock import MagicMock

from pydantic import ValidationError

from apps.memory.parser import (
    extract_json,
    parse_with_schema,
    llm_with_retry,
    SessionRefineSchema,
    UserRefineSchema,
)


class TestExtractJson:
    """extract_json 提取逻辑测试"""

    def test_empty(self):
        """空输入返回 None"""
        assert extract_json('') is None
        assert extract_json(None) is None

    def test_plain_json(self):
        """纯 JSON 原样返回"""
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_json_fence(self):
        """```json 代码块围栏应被剥离后再提取"""
        text = '```json\n{"summary": "s"}\n```'
        assert extract_json(text) == '{"summary": "s"}'

    def test_with_leading_text(self):
        """LLM 输出附带说明文字时，用正则提取花括号部分"""
        text = '好的，以下是结果：\n{"summary": "s"}，请查收'
        assert extract_json(text) == '{"summary": "s"}'

    def test_no_braces(self):
        """不含花括号的文本返回 None"""
        assert extract_json('没有任何 JSON') is None


class TestParseWithSchema:
    """parse_with_schema 解析与校验测试"""

    def test_valid_json(self):
        """合法 JSON 且符合 Schema 时返回 Pydantic 对象"""
        result = parse_with_schema(
            '{"summary": "会议纪要", "entities": ["张三"]}', SessionRefineSchema)
        assert result is not None
        assert result.summary == '会议纪要'
        assert result.entities == ['张三']

    def test_invalid_json(self):
        """JSON 语法错误返回 None"""
        assert parse_with_schema('{"summary": 未闭合', SessionRefineSchema) is None

    def test_schema_violation(self):
        """entities 超过 max_items=20 校验失败返回 None"""
        text = json.dumps({'entities': ['e'] * 21})
        assert parse_with_schema(text, SessionRefineSchema) is None

    def test_no_json(self):
        """无 JSON 内容返回 None"""
        assert parse_with_schema('没有内容', SessionRefineSchema) is None

    def test_empty_input(self):
        assert parse_with_schema('', SessionRefineSchema) is None


class TestSchemas:
    """Pydantic Schema 默认值与约束测试"""

    def test_session_refine_defaults(self):
        s = SessionRefineSchema()
        assert s.summary == ''
        assert s.entities == []
        assert s.keywords == []

    def test_session_refine_summary_max_length(self):
        """summary 超过 max_length=512 应抛 ValidationError"""
        with pytest.raises(ValidationError):
            SessionRefineSchema(summary='x' * 513)

    def test_session_refine_entities_max_items(self):
        """entities 超过 max_items=20 应抛 ValidationError"""
        with pytest.raises(ValidationError):
            SessionRefineSchema(entities=['e'] * 21)

    def test_user_refine_defaults(self):
        u = UserRefineSchema()
        assert u.domain_tags == []
        assert u.frequent_topics == []
        assert u.preferences == {}
        assert u.profile_text == ''


class TestLlmWithRetry:
    """llm_with_retry 重试机制测试"""

    @staticmethod
    def _mock_llm(contents):
        """构造按顺序返回不同内容的 llm mock"""
        llm = MagicMock()
        llm.chat.side_effect = [{'content': c} for c in contents]
        return llm

    def test_success_first_attempt(self):
        """首次调用即成功：只调用一次 LLM"""
        llm = self._mock_llm(['{"summary": "一次成功"}'])
        result = llm_with_retry(llm, [], SessionRefineSchema, max_retries=3)
        assert result is not None
        assert result.summary == '一次成功'
        assert llm.chat.call_count == 1

    def test_success_after_retry(self):
        """前几次输出非法，最后一次成功"""
        llm = self._mock_llm(['这不是 JSON', '{"summary": "重试成功"}'])
        result = llm_with_retry(
            llm, [{'role': 'user', 'content': '原始'}],
            SessionRefineSchema, max_retries=3)
        assert result is not None
        assert result.summary == '重试成功'
        assert llm.chat.call_count == 2
        # 重试时消息列表被替换为提示 JSON 的固定两条消息（不增长原始消息）
        retry_msgs = llm.chat.call_args_list[1][0][0]
        assert len(retry_msgs) == 2

    def test_all_attempts_fail(self):
        """全部尝试失败返回 None"""
        llm = self._mock_llm(['bad'] * 3)
        result = llm_with_retry(llm, [], SessionRefineSchema, max_retries=3)
        assert result is None
        assert llm.chat.call_count == 3

    def test_temperature_decay(self):
        """每次重试温度递减 0.1：0.2 -> 0.1 -> 0.0"""
        llm = self._mock_llm(['bad', 'bad', 'bad'])
        llm_with_retry(llm, [], SessionRefineSchema, max_retries=3, temperature=0.2)
        temps = [call.kwargs['temperature'] for call in llm.chat.call_args_list]
        assert temps == [0.2, 0.1, 0.0]

    def test_max_retries_one(self):
        """max_retries=1 时只尝试一次即返回 None"""
        llm = self._mock_llm(['bad'])
        result = llm_with_retry(llm, [], SessionRefineSchema, max_retries=1)
        assert result is None
        assert llm.chat.call_count == 1

    def test_missing_content_key(self):
        """响应缺少 content 字段按空内容处理，触发重试"""
        llm = MagicMock()
        llm.chat.side_effect = [
            {'other': 'field'},
            {'content': '{"summary": "ok"}'},
        ]
        result = llm_with_retry(llm, [], SessionRefineSchema, max_retries=3)
        assert result is not None
        assert result.summary == 'ok'

    def test_llm_exception_propagates(self):
        """llm.chat 抛异常时不吞掉，向上传播由调用方处理"""
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError('api down')
        with pytest.raises(RuntimeError):
            llm_with_retry(llm, [], SessionRefineSchema)
