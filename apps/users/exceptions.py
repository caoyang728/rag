"""
DRF 自定义异常处理
统一错误响应格式：{'code': int, 'message': str, 'details': {}}
"""
from loguru import logger

from rest_framework.views import exception_handler
from rest_framework.exceptions import (
    ValidationError, PermissionDenied, NotAuthenticated,
    MethodNotAllowed, Throttled, NotFound,
)
from django.core.exceptions import PermissionDenied as DjPerm
from django.http import Http404


def custom_exception_handler(exc, context):
    resp = exception_handler(exc, context)
    if resp is None:
        logger.exception(f'Unhandled: {exc}')
        return None

    code = 40000
    message = ''
    if isinstance(exc, ValidationError):
        code = 40001
        message = '参数校验失败'
    elif isinstance(exc, MethodNotAllowed):
        code = 40500
        message = '请求方法不支持'
    elif isinstance(exc, (NotAuthenticated,)):
        code = 40100
        message = '未登录或 Token 无效'
    elif isinstance(exc, (PermissionDenied, DjPerm)):
        code = 40300
        message = '无权访问'
    elif isinstance(exc, (Http404, NotFound)):
        code = 40400
        message = '资源不存在'
    elif isinstance(exc, Throttled):
        code = 42900
        # 展示限流等待时间，便于前端实现重试倒计时
        message = f'请求过于频繁，请 {int(exc.wait)} 秒后重试'
    else:
        code = 50000
        # 不向客户端暴露内部异常详情，避免泄露栈信息/SQL/路径等敏感信息
        message = '服务器内部错误'

    resp.data = {
        'code': code,
        'message': message,
        'details': resp.data if isinstance(resp.data, (dict, list)) else {},
    }
    return resp
