"""
SSE 流式响应工具
"""
import json
from loguru import logger
from typing import Iterator

from django.http import StreamingHttpResponse


# 客户端断开时 WSGI 写入会抛出的异常（BrokenPipeError/ConnectionResetError 等均为 OSError 子类）
_CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)


def sse_pack(data: dict) -> str:
    """SSE 一条消息"""
    return f'data: {json.dumps(data, ensure_ascii=False)}\n\n'


def stream_response(generator: Iterator[dict]) -> StreamingHttpResponse:
    """把 Provider.stream 的输出包装成 SSE

    客户端断开处理：当 yield 写入失败（BrokenPipe 等），主动调用 generator.close()
    触发底层 ask_stream 生成器的 GeneratorExit，从而中断 LLM 流式迭代、释放 HTTP 连接。
    """
    def _iter():
        try:
            for chunk in generator:
                yield sse_pack(chunk)
        except GeneratorExit:
            # WSGI 服务器关闭了生成器（客户端断开），传播以触发底层 generator.close()
            logger.info('SSE generator exit (client closed)')
            raise
        except _CLIENT_DISCONNECT_ERRORS as e:
            # 写入失败：客户端已断开，主动关闭底层生成器以停止 LLM 流
            logger.info('SSE client disconnected: %s', e)
            try:
                generator.close()
            except Exception:
                pass
            return
        except Exception as e:
            logger.exception('SSE stream error')
            try:
                yield sse_pack({'error': str(e), 'finish': True})
            except _CLIENT_DISCONNECT_ERRORS:
                pass
        # 正常结束：发送 [DONE] 标记
        try:
            yield 'data: [DONE]\n\n'
        except _CLIENT_DISCONNECT_ERRORS:
            pass

    resp = StreamingHttpResponse(_iter(), content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'  # nginx 关闭缓冲
    return resp
