"""
apps.system.middleware 测试 —— 慢请求监控中间件

覆盖范围：
- 慢请求（>30s）：记录日志并给响应添加 X-Request-Time 头
- 正常请求：不添加响应头
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.system.middleware import SlowRequestMiddleware


class TestSlowRequestMiddleware:
    """SlowRequestMiddleware 行为测试"""

    def test_slow_request_marks_header(self):
        """处理耗时超过阈值时响应应携带 X-Request-Time 头"""
        resp = MagicMock()
        resp.__setitem__ = MagicMock()
        get_response = MagicMock(return_value=resp)
        mw = SlowRequestMiddleware(get_response)
        request = MagicMock()
        request.path = '/api/v1/analytics/stats/'

        with patch('apps.system.middleware.time.time', side_effect=[0.0, 35.0]):
            result = mw(request)

        assert result is resp
        # 30s 阈值：35-0=35 > 30，应设置响应头
        assert resp.__setitem__.called

    def test_fast_request_no_header(self):
        """处理耗时低于阈值时不添加响应头"""
        resp = MagicMock()
        get_response = MagicMock(return_value=resp)
        mw = SlowRequestMiddleware(get_response)
        request = MagicMock()

        with patch('apps.system.middleware.time.time', side_effect=[0.0, 0.5]):
            result = mw(request)

        assert result is resp
        assert not resp.__setitem__.called

    def test_always_forwards_response(self):
        """无论快慢都应透传原始响应（不阻断业务）"""
        resp = MagicMock()
        get_response = MagicMock(return_value=resp)
        mw = SlowRequestMiddleware(get_response)
        assert mw(MagicMock()) is resp
        get_response.assert_called_once()
