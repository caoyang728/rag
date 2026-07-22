"""
SSE 流式响应工具
"""
import json
from loguru import logger
from typing import Iterator

from django.http import StreamingHttpResponse



def sse_pack(data: dict) -> str:
    """SSE 一条消息"""
    return f'data: {json.dumps(data, ensure_ascii=False)}\n\n'


def stream_response(generator: Iterator[dict]) -> StreamingHttpResponse:
    """把 Provider.stream 的输出包装成 SSE"""
    def _iter():
        try:
            for chunk in generator:
                yield sse_pack(chunk)
        except Exception as e:
            logger.exception('SSE stream error')
            yield sse_pack({'error': str(e), 'finish': True})
        yield 'data: [DONE]\n\n'

    resp = StreamingHttpResponse(_iter(), content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'  # nginx 关闭缓冲
    return resp
