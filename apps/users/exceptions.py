"""
DRF 自定义异常处理
统一错误响应格式：{'code': int, 'message': str, 'details': {}}
"""
from loguru import logger

from rest_framework.views import exception_handler
from rest_framework.exceptions import ValidationError, PermissionDenied, NotAuthenticated
from django.core.exceptions import PermissionDenied as DjPerm
from django.http import Http404



def custom_exception_handler(exc, context):
    resp = exception_handler(exc, context)
    if resp is None:
        logger.exception('Unhandled: %s', exc)
        return None

    code = 40000
    message = ''
    if isinstance(exc, ValidationError):
        code = 40001
        message = '参数校验失败'
    elif isinstance(exc, NotAuthenticated):
        code = 40100
        message = '未登录或 Token 无效'
    elif isinstance(exc, (PermissionDenied, DjPerm)):
        code = 40300
        message = '无权访问'
    elif isinstance(exc, Http404):
        code = 40400
        message = '资源不存在'
    else:
        code = 50000
        message = str(exc)[:200]

    resp.data = {
        'code': code,
        'message': message,
        'details': resp.data if isinstance(resp.data, (dict, list)) else {},
    }
    return resp
