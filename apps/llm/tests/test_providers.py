"""
apps.llm.providers 测试 —— Provider 抽象基类辅助方法 + DeepSeek / OpenAI 兼容 Provider

覆盖范围：
- BaseLLMProvider._extract_tool_calls：同步响应 tool_calls 提取（无/正常/无 function 跳过）
- BaseLLMProvider._merge_tool_call_deltas：流式 tool_calls 分片累积（按 index 拼接 arguments）
- DeepSeekProvider：_estimate_cost 定价、chat 成功/异常、stream 文本帧/结束帧/异常帧
- _OpenAICompatibleProvider：default_base_url 回退、tools/tool_choice 透传

采用 mock 而非真实 OpenAI 客户端：
Provider.__init__ 会实例化 openai.OpenAI 客户端（构造即读 env），chat/stream 会发起
真实 HTTP。本测试通过 __new__ 绕过 __init__ 手工注入 client mock，专注验证
响应解析、异常降级与参数拼装契约，不产生任何网络调用。
"""
import pytest
from unittest.mock import MagicMock, patch

from apps.llm.providers.base import BaseLLMProvider
from apps.llm.providers.deepseek import DeepSeekProvider, DEEPSEEK_PRICING
from apps.llm.providers.stubs import _OpenAICompatibleProvider, QwenProvider


def _make_deepseek(client=None, model='deepseek-chat'):
    """绕过 __init__ 直接构造 DeepSeekProvider，注入 mock client"""
    p = object.__new__(DeepSeekProvider)
    p.api_key = 'sk-test'
    p.base_url = 'https://api.deepseek.com/v1'
    p.model = model
    p.timeout = 60
    p.name = 'deepseek'
    p.extra = {}
    p.client = client or MagicMock()
    return p


def _make_resp(content='回答内容', finish_reason='stop', tool_calls=None,
               prompt_tokens=10, completion_tokens=20):
    """构造 OpenAI 风格响应对象（mock）"""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens

    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls or []

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _make_tool_call(tc_id='call_1', name='search', arguments='{}'):
    """构造 OpenAI ToolCall 对象（mock）"""
    fn = MagicMock()
    fn.name = name
    fn.arguments = arguments
    tc = MagicMock()
    tc.id = tc_id
    tc.function = fn
    return tc


# ============================================================================
# BaseLLMProvider._extract_tool_calls
# ============================================================================
@pytest.mark.unit
class TestExtractToolCalls:
    """同步响应的 tool_calls 提取测试"""

    def test_extract_tool_calls_empty(self):
        """message 无 tool_calls 时返回空列表"""
        msg = MagicMock()
        msg.tool_calls = []
        assert BaseLLMProvider._extract_tool_calls(msg) == []

    def test_extract_tool_calls_returns_dicts(self):
        """有 tool_calls 时转为 {id, name, arguments} 字典结构"""
        msg = MagicMock()
        msg.tool_calls = [_make_tool_call('call_1', 'search', '{"q":"x"}')]
        result = BaseLLMProvider._extract_tool_calls(msg)
        assert result == [{'id': 'call_1', 'name': 'search', 'arguments': '{"q":"x"}'}]

    def test_extract_tool_calls_skip_no_function(self):
        """tool_call 无 function 时应跳过，不产出记录"""
        tc = MagicMock()
        tc.function = None
        msg = MagicMock()
        msg.tool_calls = [tc, _make_tool_call('call_2', 'calc', '1+1')]
        result = BaseLLMProvider._extract_tool_calls(msg)
        assert len(result) == 1
        assert result[0]['id'] == 'call_2'


# ============================================================================
# BaseLLMProvider._merge_tool_call_deltas
# ============================================================================
@pytest.mark.unit
class TestMergeToolCallDeltas:
    """流式 tool_calls 分片累积测试"""

    def test_merge_tool_call_deltas_none(self):
        """delta.tool_calls 为空时返回原累积列表"""
        acc = [{'id': 'c1', 'name': 'n', 'arguments': 'a'}]
        assert BaseLLMProvider._merge_tool_call_deltas(acc, None) is acc

    def test_merge_tool_call_deltas_accumulate_arguments(self):
        """首帧带 id/name，后续帧补 arguments 分片，应按 index 拼接"""
        # 首帧：index=0，带 id 和 name
        first = MagicMock()
        first.index = 0
        first.id = 'call_1'
        fn1 = MagicMock()
        fn1.name = 'search'
        fn1.arguments = '{"q"'
        first.function = fn1
        # 第二帧：index=0，只补 arguments 片段
        second = MagicMock()
        second.index = 0
        second.id = None
        fn2 = MagicMock()
        fn2.name = None
        fn2.arguments = ':"x"}'
        second.function = fn2

        acc = []
        acc = BaseLLMProvider._merge_tool_call_deltas(acc, [first])
        acc = BaseLLMProvider._merge_tool_call_deltas(acc, [second])
        assert acc == [{'id': 'call_1', 'name': 'search', 'arguments': '{"q":"x"}'}]

    def test_merge_tool_call_deltas_expand_index(self):
        """跳 index 到达新工具时应自动扩展列表占位"""
        delta = MagicMock()
        delta.index = 2
        delta.id = 'call_3'
        fn = MagicMock()
        fn.name = 'calc'
        fn.arguments = '1'
        delta.function = fn

        acc = BaseLLMProvider._merge_tool_call_deltas([], [delta])
        assert len(acc) == 3  # index 0/1 占位补齐
        assert acc[2] == {'id': 'call_3', 'name': 'calc', 'arguments': '1'}


# ============================================================================
# DeepSeekProvider
# ============================================================================
@pytest.mark.unit
class TestDeepSeekProvider:
    """DeepSeek Provider 定价与 chat/stream 行为测试"""

    def test_estimate_cost_known_model(self):
        """已知模型按 DEEPSEEK_PRICING 定价换算"""
        p = _make_deepseek(model='deepseek-chat')
        price = DEEPSEEK_PRICING['deepseek-chat']
        assert p._estimate_cost(1000, 1000) == round(
            (1000 * price['prompt'] + 1000 * price['completion']) / 1000, 6)

    def test_estimate_cost_unknown_model_fallback(self):
        """未知模型回退到 deepseek-chat 定价"""
        p = _make_deepseek(model='unknown-model')
        price = DEEPSEEK_PRICING['deepseek-chat']
        assert p._estimate_cost(1000, 0) == round(1000 * price['prompt'] / 1000, 6)

    def test_chat_success_returns_full_dict(self):
        """chat 正常返回包含 content/tokens/cost/tool_calls 的完整字典"""
        p = _make_deepseek()
        p.client.chat.completions.create.return_value = _make_resp(
            content='你好', finish_reason='stop',
            tool_calls=[_make_tool_call('c1', 'search', '{}')])
        result = p.chat([{'role': 'user', 'content': 'hi'}])
        assert result['content'] == '你好'
        assert result['prompt_tokens'] == 10
        assert result['completion_tokens'] == 20
        assert result['total_tokens'] == 30
        assert result['model'] == 'deepseek-chat'
        assert result['provider'] == 'deepseek'
        assert result['finish_reason'] == 'stop'
        assert result['tool_calls'][0]['id'] == 'c1'
        assert result['cost'] == p._estimate_cost(10, 20)

    def test_chat_error_returns_error_dict(self):
        """chat 异常时返回带 error 字段的降级字典，不向上抛出"""
        p = _make_deepseek()
        p.client.chat.completions.create.side_effect = RuntimeError('boom')
        result = p.chat([{'role': 'user', 'content': 'hi'}])
        assert result['finish_reason'] == 'error'
        assert 'boom' in result['content']
        assert result['tool_calls'] == []
        assert result['cost'] == 0

    def test_stream_text_frames(self):
        """stream 逐帧 yield 文本增量，结束帧带完整 content"""
        p = _make_deepseek()
        chunk1 = MagicMock()
        chunk1.choices = [MagicMock()]
        chunk1.choices[0].finish_reason = None
        chunk1.choices[0].delta.content = '你'
        chunk2 = MagicMock()
        chunk2.choices = [MagicMock()]
        chunk2.choices[0].finish_reason = None
        chunk2.choices[0].delta.content = '好'
        resp = MagicMock()
        resp.__enter__.return_value = [chunk1, chunk2]
        p.client.chat.completions.create.return_value = resp

        frames = list(p.stream([{'role': 'user', 'content': 'hi'}]))
        assert frames[0] == {'delta': '你', 'finish': False}
        assert frames[1] == {'delta': '好', 'finish': False}
        end = frames[-1]
        assert end['finish'] is True
        assert end['content'] == '你好'
        assert end['finish_reason'] == 'stop'

    def test_stream_error_frame(self):
        """stream 异常时 yield 错误结束帧，不抛异常"""
        p = _make_deepseek()
        p.client.chat.completions.create.side_effect = ConnectionError('down')
        frames = list(p.stream([{'role': 'user', 'content': 'hi'}]))
        assert frames[0]['finish'] is True
        assert frames[0]['finish_reason'] == 'error'
        assert 'down' in frames[0]['delta']


# ============================================================================
# _OpenAICompatibleProvider（stubs）
# ============================================================================
@pytest.mark.unit
class TestOpenAICompatibleProvider:
    """OpenAI 兼容 Provider 的参数拼装与透传测试"""

    def test_default_base_url_fallback(self):
        """base_url 为空时回退子类 default_base_url"""
        with patch('apps.llm.providers.stubs.OpenAI') as mock_openai:
            p = QwenProvider(api_key='sk-test', base_url='', model='qwen-plus')
        assert p.base_url == QwenProvider.default_base_url
        # OpenAI 客户端应使用回退后的 base_url 实例化
        assert mock_openai.call_args.kwargs['base_url'] == QwenProvider.default_base_url

    def test_chat_passes_tools_and_tool_choice(self):
        """传入 tools/tool_choice 时应透传到 create 请求"""
        p = object.__new__(_OpenAICompatibleProvider)
        p.api_key = 'sk'
        p.base_url = 'http://x'
        p.model = 'm'
        p.timeout = 60
        p.name = 'qwen'
        p.extra = {}
        p.client = MagicMock()
        p.client.chat.completions.create.return_value = _make_resp()

        tools = [{'type': 'function', 'function': {'name': 'search'}}]
        p.chat([{'role': 'user', 'content': 'q'}], tools=tools, tool_choice='auto')

        kwargs = p.client.chat.completions.create.call_args.kwargs
        assert kwargs['tools'] == tools
        assert kwargs['tool_choice'] == 'auto'
        assert kwargs['stream'] is False

    def test_chat_without_tools_omits_tool_keys(self):
        """未传 tools 时请求 kwargs 不应包含 tools/tool_choice"""
        p = object.__new__(_OpenAICompatibleProvider)
        p.api_key = 'sk'
        p.base_url = 'http://x'
        p.model = 'm'
        p.timeout = 60
        p.name = 'qwen'
        p.extra = {}
        p.client = MagicMock()
        p.client.chat.completions.create.return_value = _make_resp()

        p.chat([{'role': 'user', 'content': 'q'}])
        kwargs = p.client.chat.completions.create.call_args.kwargs
        assert 'tools' not in kwargs
        assert 'tool_choice' not in kwargs
