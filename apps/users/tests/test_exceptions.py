"""
apps.users.exceptions 单元测试 —— DRF 自定义异常处理

覆盖范围：
- custom_exception_handler 对各类 DRF 异常的映射（code/message 分支）
- DRF 标准 exception_handler 返回 None 时的降级（返回 None + logger.exception）
- resp.data 非 dict/list 时 details 降级为空 dict
- Throttled 异常等待时间拼入 message

异常映射分支：
- ValidationError → 40001
- MethodNotAllowed → 40500
- NotAuthenticated → 40100
- PermissionDenied（DRF + Django）→ 40300
- NotFound / Http404 → 40400
- Throttled → 42900（含等待秒数）
- 其他 DRF 异常 → 50000

所有测试为纯逻辑，不依赖 DB，使用 @pytest.mark.unit。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.users.exceptions import custom_exception_handler


# ============================================================================
# 辅助：构造 mock DRF exception_handler 返回值
# ============================================================================
def _make_resp(data='__default__'):
    """构造一个模拟的 DRF Response 对象，供 exception_handler 分支使用"""
    resp = MagicMock()
    if data == '__default__':
        resp.data = {'detail': 'test'}
    else:
        resp.data = data
    return resp


def _make_exc(exc_class, **kwargs):
    """构造指定类型的 DRF 异常实例（无需真正 raise）"""
    return exc_class(**kwargs)


# ============================================================================
# exception_handler 返回 None —— DRF 无法处理的异常（如原生 Python 异常）
# ============================================================================
class TestUnhandledException:
    """DRF exception_handler 返回 None 时，custom_exception_handler 也应返回 None"""

    @patch('apps.users.exceptions.exception_handler', return_value=None)
    @patch('apps.users.exceptions.logger')
    def test_returns_none_when_exception_handler_returns_none(self, mock_logger, mock_handler):
        """非 DRF 异常 → exception_handler 返回 None → 本函数也返回 None"""
        exc = ValueError('some internal error')
        result = custom_exception_handler(exc, {'view': None})
        assert result is None

    @patch('apps.users.exceptions.exception_handler', return_value=None)
    @patch('apps.users.exceptions.logger')
    def test_logs_exception_when_unhandled(self, mock_logger, mock_handler):
        """未处理异常应触发 logger.exception 记录，便于运维排查"""
        exc = RuntimeError('unexpected')
        custom_exception_handler(exc, {'view': None})
        mock_logger.exception.assert_called_once()
        assert 'unexpected' in mock_logger.exception.call_args[0][0]


# ============================================================================
# ValidationError → code 40001
# ============================================================================
class TestValidationError:
    """DRF ValidationError 映射为参数校验失败"""

    @patch('apps.users.exceptions.exception_handler')
    def test_validation_error_maps_to_40001(self, mock_handler):
        """ValidationError → code=40001, message='参数校验失败'"""
        from rest_framework.exceptions import ValidationError
        mock_handler.return_value = _make_resp({'field': ['required']})
        exc = ValidationError({'field': ['required']})
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 40001
        assert result.data['message'] == '参数校验失败'
        assert result.data['details'] == {'field': ['required']}


# ============================================================================
# MethodNotAllowed → code 40500
# ============================================================================
class TestMethodNotAllowed:
    """DRF MethodNotAllowed 映射为请求方法不支持"""

    @patch('apps.users.exceptions.exception_handler')
    def test_method_not_allowed_maps_to_40500(self, mock_handler):
        """MethodNotAllowed → code=40500, message='请求方法不支持'"""
        from rest_framework.exceptions import MethodNotAllowed
        mock_handler.return_value = _make_resp({'detail': 'Method GET not allowed'})
        exc = MethodNotAllowed('GET')
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 40500
        assert result.data['message'] == '请求方法不支持'


# ============================================================================
# NotAuthenticated → code 40100
# ============================================================================
class TestNotAuthenticated:
    """DRF NotAuthenticated 映射为未登录或 Token 无效"""

    @patch('apps.users.exceptions.exception_handler')
    def test_not_authenticated_maps_to_40100(self, mock_handler):
        """NotAuthenticated → code=40100, message='未登录或 Token 无效'"""
        from rest_framework.exceptions import NotAuthenticated
        mock_handler.return_value = _make_resp({'detail': 'Authentication credentials were not provided.'})
        exc = NotAuthenticated()
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 40100
        assert result.data['message'] == '未登录或 Token 无效'


# ============================================================================
# PermissionDenied（DRF）→ code 40300
# ============================================================================
class TestPermissionDeniedDRF:
    """DRF PermissionDenied 映射为无权访问"""

    @patch('apps.users.exceptions.exception_handler')
    def test_drf_permission_denied_maps_to_40300(self, mock_handler):
        """DRF PermissionDenied → code=40300, message='无权访问'"""
        from rest_framework.exceptions import PermissionDenied
        mock_handler.return_value = _make_resp({'detail': 'You do not have permission.'})
        exc = PermissionDenied()
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 40300
        assert result.data['message'] == '无权访问'


# ============================================================================
# Django PermissionDenied → code 40300
# ============================================================================
class TestPermissionDeniedDjango:
    """Django 原生 PermissionDenied 与 DRF 异常同码，保持权限错误一致对外暴露"""

    @patch('apps.users.exceptions.exception_handler')
    def test_django_permission_denied_maps_to_40300(self, mock_handler):
        """Django PermissionDenied → code=40300, message='无权访问'"""
        from django.core.exceptions import PermissionDenied as DjPerm
        mock_handler.return_value = _make_resp({'detail': 'Forbidden'})
        exc = DjPerm('forbidden')
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 40300
        assert result.data['message'] == '无权访问'


# ============================================================================
# NotFound → code 40400
# ============================================================================
class TestNotFoundDRF:
    """DRF NotFound 映射为资源不存在"""

    @patch('apps.users.exceptions.exception_handler')
    def test_drf_not_found_maps_to_40400(self, mock_handler):
        """NotFound → code=40400, message='资源不存在'"""
        from rest_framework.exceptions import NotFound
        mock_handler.return_value = _make_resp({'detail': 'Not found.'})
        exc = NotFound()
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 40400
        assert result.data['message'] == '资源不存在'


# ============================================================================
# Http404 → code 40400
# ============================================================================
class TestHttp404:
    """Django Http404 与 DRF NotFound 同码，统一返回 40400"""

    @patch('apps.users.exceptions.exception_handler')
    def test_django_http404_maps_to_40400(self, mock_handler):
        """Http404 → code=40400, message='资源不存在'"""
        from django.http import Http404
        mock_handler.return_value = _make_resp({'detail': 'No Question matches the given query.'})
        exc = Http404('No Question matches the given query.')
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 40400
        assert result.data['message'] == '资源不存在'


# ============================================================================
# Throttled → code 42900（含等待时间）
# ============================================================================
class TestThrottled:
    """DRF Throttled 映射为限流提示，等待时间拼入 message"""

    @patch('apps.users.exceptions.exception_handler')
    def test_throttled_maps_to_42900_with_wait_time(self, mock_handler):
        """Throttled → code=42900, message 包含等待秒数"""
        from rest_framework.exceptions import Throttled
        mock_handler.return_value = _make_resp({'detail': 'Request was throttled.'})
        exc = Throttled(wait=37.5)
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 42900
        # int(exc.wait) 截断后应出现在 message 中
        assert str(int(exc.wait)) in result.data['message']
        assert '秒后重试' in result.data['message']

    @patch('apps.users.exceptions.exception_handler')
    def test_throttled_zero_wait(self, mock_handler):
        """Throttled(wait=0) → message 显示 0 秒后重试"""
        from rest_framework.exceptions import Throttled
        mock_handler.return_value = _make_resp({'detail': 'Throttled'})
        exc = Throttled(wait=0)
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 42900
        assert '0' in result.data['message']


# ============================================================================
# 其他未识别 DRF 异常 → code 50000（兜底分支）
# ============================================================================
class TestOtherDRFException:
    """未在显式分支中的 DRF 异常走 else 兜底 → 50000"""

    @patch('apps.users.exceptions.exception_handler')
    def test_unknown_drf_exception_maps_to_50000(self, mock_handler):
        """APIException 子类未单独处理时 → code=50000, message='服务器内部错误'"""
        from rest_framework.exceptions import APIException
        mock_handler.return_value = _make_resp({'detail': 'Something broke'})
        exc = APIException('Something broke')
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['code'] == 50000
        assert result.data['message'] == '服务器内部错误'


# ============================================================================
# resp.data 非 dict/list → details 降级为空 dict
# ============================================================================
class TestResponseDataNonDictList:
    """resp.data 为字符串/数值等非 dict/list 类型时，details 应降级为空 dict"""

    @patch('apps.users.exceptions.exception_handler')
    def test_resp_data_string_becomes_empty_details(self, mock_handler):
        """resp.data 为字符串 → details={}，避免前端解析异常"""
        from rest_framework.exceptions import ValidationError
        mock_handler.return_value = _make_resp('raw string error')
        exc = ValidationError('bad input')
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['details'] == {}

    @patch('apps.users.exceptions.exception_handler')
    def test_resp_data_int_becomes_empty_details(self, mock_handler):
        """resp.data 为整数 → details={}，兜底非结构化响应"""
        from rest_framework.exceptions import ValidationError
        mock_handler.return_value = _make_resp(42)
        exc = ValidationError(42)
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['details'] == {}

    @patch('apps.users.exceptions.exception_handler')
    def test_resp_data_list_preserved_as_details(self, mock_handler):
        """resp.data 为 list 时保留原值（list 也是合法的 details 格式）"""
        from rest_framework.exceptions import ValidationError
        mock_handler.return_value = _make_resp(['error_a', 'error_b'])
        exc = ValidationError(['error_a', 'error_b'])
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['details'] == ['error_a', 'error_b']

    @patch('apps.users.exceptions.exception_handler')
    def test_resp_data_none_becomes_empty_details(self, mock_handler):
        """resp.data 为 None → details={}，防止 NoneType 直出"""
        from rest_framework.exceptions import ValidationError
        mock_handler.return_value = _make_resp(None)
        exc = ValidationError(None)
        result = custom_exception_handler(exc, {'view': None})
        assert result.data['details'] == {}


# ============================================================================
# 通用：所有成功分支都返回 resp 对象且 data 被完整覆写
# ============================================================================
class TestResponseStructure:
    """验证所有映射分支统一返回 code/message/details 三字段结构"""

    @patch('apps.users.exceptions.exception_handler')
    def test_response_always_has_three_keys(self, mock_handler):
        """无论异常类型，resp.data 必须包含 code, message, details 三个键"""
        from rest_framework.exceptions import NotFound
        mock_handler.return_value = _make_resp({'detail': 'gone'})
        exc = NotFound()
        result = custom_exception_handler(exc, {'view': None})
        assert set(result.data.keys()) == {'code', 'message', 'details'}
