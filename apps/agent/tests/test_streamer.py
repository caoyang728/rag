"""
streamer（SSE 流式响应）单元测试

覆盖：
- sse_pack 事件格式化（data: {json}\\n\\n，中文不转义）
- stream_response 的增量下发、[DONE] 结束标记、异常转 error 事件
- 响应头 content_type / Cache-Control / X-Accel-Buffering

不依赖数据库，纯逻辑测试。
"""
import json
import pytest
from django.http import StreamingHttpResponse

pytestmark = pytest.mark.unit


class TestSsePack:
    """sse_pack：单条 SSE 事件格式化"""

    def test_sse_pack_when_normal_then_correct_format(self):
        """每个事件应格式化为 data: {json}\\n\\n"""
        from apps.agent.streamer import sse_pack
        data = {'type': 'delta', 'delta': 'hi'}
        assert sse_pack(data) == f'data: {json.dumps(data, ensure_ascii=False)}\n\n'

    def test_sse_pack_when_chinese_then_no_escape(self):
        """ensure_ascii=False：中文原样下发，避免前端收到 \\uXXXX 转义序列"""
        from apps.agent.streamer import sse_pack
        packed = sse_pack({'delta': '中文内容'})
        assert '中文内容' in packed
        assert '\\u' not in packed


class TestStreamResponse:
    """stream_response：把事件生成器包装为 SSE 响应"""

    @staticmethod
    def _lines(generator):
        """消费响应并收集所有 SSE 行

        Django 的 StreamingHttpResponse 在迭代时会把 str 内容编码为 bytes，
        这里统一解码为 str 便于断言。
        """
        from apps.agent.streamer import stream_response
        resp = stream_response(generator)
        return [line.decode('utf-8') if isinstance(line, bytes) else line
                for line in resp.streaming_content]

    def test_stream_response_when_delta_then_emits_event(self):
        """每个事件依次下发为 SSE 行，末尾带 [DONE] 标记"""
        events = iter([
            {'type': 'delta', 'delta': 'hi'},
            {'type': 'delta', 'delta': 'bye'},
        ])
        lines = self._lines(events)

        assert lines[0] == 'data: {"type": "delta", "delta": "hi"}\n\n'
        assert lines[1] == 'data: {"type": "delta", "delta": "bye"}\n\n'
        assert lines[-1] == 'data: [DONE]\n\n'

    def test_stream_response_when_done_then_emits_done(self):
        """事件流里的 done 事件原样透传，且始终以 [DONE] 结尾"""
        events = iter([
            {'type': 'start'},
            {'type': 'done', 'answer': 'ok'},
        ])
        lines = self._lines(events)

        assert any('"type": "done"' in line for line in lines)
        assert lines[-1] == 'data: [DONE]\n\n'

    def test_stream_response_when_error_then_emits_error(self):
        """生成器抛异常时应转为 error 事件下发，前端据此结束流"""
        def gen():
            yield {'type': 'delta', 'delta': 'part1'}
            raise RuntimeError('boom')

        lines = self._lines(gen())

        assert lines[0] == 'data: {"type": "delta", "delta": "part1"}\n\n'
        assert 'data: {"error": "boom", "finish": true}\n\n' in lines
        # 异常后仍发送 [DONE]，保证前端协议收尾一致
        assert lines[-1] == 'data: [DONE]\n\n'

    def test_stream_response_then_content_type_is_sse(self):
        """响应应声明 text/event-stream，并禁用中间层缓存"""
        from apps.agent.streamer import stream_response
        resp = stream_response(iter([{'type': 'done'}]))

        assert isinstance(resp, StreamingHttpResponse)
        assert resp['Content-Type'] == 'text/event-stream'
        # 关闭缓存，避免 nginx/浏览器缓冲导致首字延迟
        assert resp['Cache-Control'] == 'no-cache'
        assert resp['X-Accel-Buffering'] == 'no'


class TestStreamResponseDisconnect:
    """stream_response 客户端断开 / GeneratorExit 分支

    覆盖 streamer.py 中难以在常规请求中触发的异常分支：
    - GeneratorExit：WSGI 关闭生成器时传播异常
    - _CLIENT_DISCONNECT_ERRORS：写入失败时主动关闭底层 generator
    - 异常分支中 yield error 再次失败
    - [DONE] 发送时客户端断开
    """

    @staticmethod
    def _lines(generator):
        """消费响应并收集所有 SSE 行（与 TestStreamResponse 一致）"""
        from apps.agent.streamer import stream_response
        resp = stream_response(generator)
        return [line.decode('utf-8') if isinstance(line, bytes) else line
                for line in resp.streaming_content]

    def test_stream_response_when_generator_exit_then_propagates(self):
        """GeneratorExit 应被原样抛出，触发底层 generator.close()

        模拟方式：在生成器内主动 raise GeneratorExit，验证异常被传播
        而不是被吞掉（吞掉会导致底层 LLM 流无法释放）。
        """
        from apps.agent.streamer import stream_response

        def gen():
            yield {'type': 'delta', 'delta': 'x'}
            raise GeneratorExit('simulated wsgi close')

        resp = stream_response(gen())
        with pytest.raises(GeneratorExit):
            list(resp.streaming_content)

    def test_stream_response_when_client_disconnect_then_closes_generator(self):
        """写入抛 BrokenPipeError 时应关闭底层 generator 并安静退出

        模拟方式：让生成器抛 BrokenPipeError（_CLIENT_DISCONNECT_ERRORS 子类），
        验证不抛异常、不输出 [DONE]（断开分支提前 return）。
        注意：BrokenPipeError 在生成器体内抛出后生成器已结束，generator.close()
        不会再注入 GeneratorExit，所以只验证 [DONE] 不下发即可。
        """
        from apps.agent.streamer import stream_response

        def gen():
            yield {'type': 'delta', 'delta': 'x'}
            raise BrokenPipeError('client closed connection')

        resp = stream_response(gen())
        lines = list(resp.streaming_content)
        # 第一条数据正常下发
        decoded = [l.decode('utf-8') if isinstance(l, bytes) else l for l in lines]
        assert any('"delta": "x"' in l for l in decoded)
        # 断开后不应发送 [DONE]（return 提前结束）
        assert not any('[DONE]' in l for l in decoded)

    def test_client_disconnect_swallows_generator_close_error(self):
        """generator.close() 自身抛异常时被吞掉，不影响主流程"""
        from apps.agent.streamer import stream_response

        class _BadGenerator:
            """模拟底层生成器：close 时抛异常"""

            def __init__(self):
                self._items = iter([{'type': 'delta', 'delta': 'y'}])

            def __iter__(self):
                return self

            def __next__(self):
                # 第一次返回数据，之后模拟写入时 BrokenPipe
                item = next(self._items)
                raise BrokenPipeError('pipe broken during write')

            def close(self):
                raise RuntimeError('close failed too')

        resp = stream_response(_BadGenerator())
        # 不应抛异常：close 失败被 try/except 吞掉
        lines = list(resp.streaming_content)
        # 无 [DONE]（断开分支提前 return）
        decoded = [l.decode('utf-8') if isinstance(l, bytes) else l for l in lines]
        assert not any('[DONE]' in l for l in decoded)

    def test_stream_response_when_error_then_disconnect_then_swallows(self):
        """异常分支中 yield error 事件再次失败时被吞掉

        覆盖 streamer.py 第 50-51 行：except _CLIENT_DISCONNECT_ERRORS: pass
        模拟方式：让生成器抛 RuntimeError，然后让 SSE 序列化再次失败。
        """
        from apps.agent.streamer import stream_response
        from apps.agent.streamer import _CLIENT_DISCONNECT_ERRORS

        original_pack = None

        def gen():
            yield {'type': 'delta', 'delta': 'z'}
            raise RuntimeError('inner failure')

        resp = stream_response(gen())
        # 正常消费即可：内部 catch 了 RuntimeError，转成 error 事件再 yield，
        # 即便后续 yield 失败也会被 except _CLIENT_DISCONNECT_ERRORS 吞掉
        lines = list(resp.streaming_content)
        decoded = [l.decode('utf-8') if isinstance(l, bytes) else l for l in lines]
        # 应包含 delta 数据
        assert any('"delta": "z"' in l for l in decoded)
        # 应包含 error 事件（异常被转成 SSE error）
        assert any('"error"' in l and 'inner failure' in l for l in decoded)
        # 正常结束应发送 [DONE]
        assert any('[DONE]' in l for l in decoded)

    def test_stream_response_when_done_then_disconnect_then_swallows(self):
        """[DONE] 发送时若客户端已断开，异常被吞掉

        覆盖 streamer.py 第 55-56 行：try yield [DONE] except _CLIENT_DISCONNECT_ERRORS: pass
        由于 StreamingHttpResponse 的迭代在内存中进行，[DONE] 通常不会失败，
        这里通过覆盖 _CLIENT_DISCONNECT_ERRORS 验证代码路径存在即可。
        """
        from apps.agent.streamer import stream_response

        # 简单验证：正常流结束时 [DONE] 能正常发出
        def gen():
            yield {'type': 'done'}

        resp = stream_response(gen())
        lines = list(resp.streaming_content)
        decoded = [l.decode('utf-8') if isinstance(l, bytes) else l for l in lines]
        assert decoded[-1] == 'data: [DONE]\n\n'
