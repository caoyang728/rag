"""
审计中间件
- 拦截所有 API 写请求
- 按 URL 模式匹配决定 action + action_category + target_type
- 从 JWT Authorization header 提取用户（非 Django session user）
- 走 AuditLog.save() 自动挂 sha256 哈希链
"""
from loguru import logger
import re
import jwt

from django.utils.deprecation import MiddlewareMixin
from django.conf import settings
from django.contrib.auth import get_user_model

User = get_user_model()

# (pattern, action, category, target_type)
# target_type 用于设置 AuditLog.target_type；target_id 由正则捕获组提取
_ACTION_MAP = [
    (re.compile(r'/api/v1/auth/login/?$'), 'login', 'auth', 'auth'),
    (re.compile(r'/api/v1/auth/logout/?$'), 'logout', 'auth', 'auth'),
    (re.compile(r'/api/v1/auth/reset-password/?$'), 'reset_password', 'auth', 'auth'),
    (re.compile(r'/api/v1/auth/refresh/?$'), 'token_refresh', 'auth', 'auth'),
    (re.compile(r'/api/v1/documents/upload/?$'), 'upload_document', 'document', 'document'),
    (re.compile(r'/api/v1/documents/(\d+)/?$'), 'delete_document', 'document', 'document'),
    (re.compile(r'/api/v1/nodes/?$'), 'create_node', 'node', 'node'),
    (re.compile(r'/api/v1/nodes/(\d+)/?$'), 'update_node', 'node', 'node'),
    (re.compile(r'/api/v1/users/?$'), 'admin_users', 'user', 'user'),
    (re.compile(r'/api/v1/users/(\d+)/?$'), 'update_user', 'user', 'user'),
    (re.compile(r'/api/v1/users/(\d+)/toggle_status/?$'), 'toggle_user_status', 'user', 'user'),
    (re.compile(r'/api/v1/chat/ask/?$'), 'chat_ask', 'chat', 'chat'),
    (re.compile(r'/api/v1/feedback/?$'), 'feedback', 'chat', 'feedback'),
    (re.compile(r'/api/v1/export/?$'), 'export', 'export', 'export'),
    (re.compile(r'/api/v1/security/ip-whitelist/?$'), 'manage_whitelist', 'security', 'ip_whitelist'),
    (re.compile(r'/api/v1/security/ip-whitelist/(\d+)/?$'), 'manage_whitelist', 'security', 'ip_whitelist'),
    (re.compile(r'/api/v1/security/ip-blacklist/?$'), 'manage_blacklist', 'security', 'ip_blacklist'),
    (re.compile(r'/api/v1/security/ip-blacklist/(\d+)/?$'), 'manage_blacklist', 'security', 'ip_blacklist'),
]

_AUDIT_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


class AuditMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        try:
            if request.method not in _AUDIT_METHODS:
                return response
            path = request.path
            if not path.startswith('/api/v1/'):
                return response

            action, category, target_type, target_id = self._match_action(path)
            if not action:
                return response

            actor_id, username = _get_user_from_jwt(request)

            # 登录/登出/改密等接口：请求时无 JWT，从 request body 提取用户名
            if not username:
                username = _get_username_from_body(request) or ''

            result = 'success' if response.status_code < 400 else 'failed'
            if response.status_code == 403:
                result = 'denied'

            from apps.audit.models import AuditLog
            AuditLog.objects.create(
                actor_id=actor_id, actor_username=username[:64],
                action=action, action_category=category,
                target_type=target_type, target_id=target_id[:64],
                result=result,
                ip_address=_get_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method=request.method, path=path[:256],
                detail={'status': response.status_code},
            )
        except Exception:
            logger.exception('AuditMiddleware error')
        return response

    def _match_action(self, path: str):
        for pat, action, cat, target_type in _ACTION_MAP:
            m = pat.search(path)
            if m:
                target_id = m.group(1) if m.lastindex and m.lastindex >= 1 else ''
                return action, cat, target_type, target_id
        return None, None, '', ''


def _get_user_from_jwt(request):
    """从 Authorization header 解码 JWT，提取 user_id 和 username"""
    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth_header.startswith('Bearer '):
        return None, ''
    token = auth_header[7:].strip()
    if not token:
        return None, ''
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=['HS256'],
            options={'verify_exp': True},
        )
        user_id = payload.get('user_id')
        if user_id is None:
            return None, ''
        user = User.objects.filter(id=user_id).first()
        if user:
            return user.id, user.username or ''
        return None, ''
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError):
        return None, ''
    except Exception:
        logger.exception('JWT decode error in audit middleware')
        return None, ''


def _get_username_from_body(request):
    """从 POST/PUT request body (JSON) 中提取 username，用于登录等无 JWT 场景"""
    import json
    try:
        body = getattr(request, 'body', None)
        if not body:
            return ''
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        data = json.loads(body) if isinstance(body, str) else body
        return (data.get('username') or '').strip()
    except Exception:
        return ''


def _get_ip(request):
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '') or request.META.get('REMOTE_ADDR', '')
    if ',' in ip:
        ip = ip.split(',')[0]
    return ip.strip()[:64]
