"""
security views - IP 白/黑名单 & 登录尝试 & 敏感词 & 验证码

安全配置变更工单化：
- 低风险（直接生效）：黑名单新增、敏感词新增
- 中风险（单审）：黑名单解封、敏感词删除/禁用
- 高风险（双审）：白名单新增/删除/编辑
"""
import os
import io
import random
import string
import base64
from loguru import logger

from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.security.models import IpWhitelist, IpBlacklist, LoginAttempt, SensitiveWord


def _get_redis():
    """获取 Redis 连接（使用 Django settings 配置）"""
    import redis
    from django.conf import settings
    redis_url = getattr(settings, 'REDIS_URL', '')
    if redis_url:
        return redis.Redis.from_url(redis_url, decode_responses=True)
    # 降级：使用环境变量
    return redis.Redis(
        host=os.getenv('REDIS_DB_HOST', 'redis'),
        port=int(os.getenv('REDIS_DB_PORT', 6379)),
        password=os.getenv('REDIS_DB_PASSWORD', ''),
        decode_responses=True,
        db=int(os.getenv('REDIS_DB_CAPTCHA', '1'))
    )


def _generate_captcha_text(length=4):
    """生成随机验证码文本"""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def _generate_captcha_image(text):
    """生成大字体验证码图片"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        
        # 创建画布（150x50）
        width, height = 140, 41
        image = Image.new('RGB', (width, height), color=(255, 255, 255))
        draw = ImageDraw.Draw(image)
        
        # 使用较大字体（56px），适合 150x50 画布
        font_size = 24
        font = None
        
        # 优先使用项目内的字体文件（确保容器内可用）
        project_font_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'static', 'fonts', 'DejaVuSans-Bold.ttf')
        
        font_paths = [
            project_font_path,
            '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf',
        ]
        
        for p in font_paths:
            if os.path.exists(p):
                try:
                    font = ImageFont.truetype(p, font_size)
                    break
                except Exception as e:
                    logger.debug(f"字体加载失败 {p}: {e}")
        
        # 如果所有字体都失败，使用字体名称搜索
        if font is None:
            font_names = ['DejaVuSans-Bold', 'DejaVuSans', 'DejaVuSansMono-Bold']
            for name in font_names:
                try:
                    font = ImageFont.truetype(name, font_size)
                    break
                except Exception as e:
                    logger.debug(f"字体加载失败 {name}: {e}")
        
        # 如果所有字体都失败，使用默认字体
        if font is None:
            font = ImageFont.load_default()
            logger.warning(f"验证码使用默认字体，大小: {font.size}")
        
        # 计算字符位置，确保居中且不重叠
        # 140px / 4字符 = 35px 每字符
        char_spacing = 35
        for i, char in enumerate(text):
            char_bbox = font.getbbox(char)
            char_w = char_bbox[2] - char_bbox[0]
            char_h = char_bbox[3] - char_bbox[1]
            x = i * char_spacing + (char_spacing - char_w) // 2
            y = (height - char_h) // 2
            draw.text((x, y), char, font=font, fill=(37, 99, 235))
        
        # 添加少量干扰线段（不影响辨认）
        for _ in range(6):
            x1, y1 = random.randint(0, width), random.randint(0, height)
            x2, y2 = random.randint(0, width), random.randint(0, height)
            draw.line((x1, y1, x2, y2), fill=(200, 200, 200), width=2)
        
        # 保存为 PNG
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"生成验证码图片失败: {e}")
        return None


class CaptchaView(APIView):
    """GET /api/v1/security/captcha/ - 获取图形验证码"""
    permission_classes = [AllowAny]
    throttle_scope = "captcha"
    
    def get(self, request):
        captcha_text = _generate_captcha_text()
        image_buf = _generate_captcha_image(captcha_text)
        
        if image_buf is None:
            return Response({"detail": "验证码生成失败"}, status=500)
        
        # 存储到 Redis，有效期 5 分钟
        import uuid
        captcha_id = str(uuid.uuid4())
        r = _get_redis()
        r.setex(f'captcha:{captcha_id}', 300, captcha_text)
        
        return Response({
            "captcha_id": captcha_id,
            "image_b64": base64.b64encode(image_buf.read()).decode('utf-8')
        })


def verify_captcha(captcha_id, captcha_code):
    """验证验证码是否正确"""
    if not captcha_id or not captcha_code:
        return False
    
    # 验证码长度校验（防止超长攻击）
    if len(captcha_code) > 10:
        return False
    
    try:
        r = _get_redis()
        stored_code = r.get(f'captcha:{captcha_id}')
        if stored_code:
            # 验证后立即删除，防止重复使用
            r.delete(f'captcha:{captcha_id}')
            return stored_code.upper() == captcha_code.upper()
    except Exception as e:
        logger.error(f"验证码验证失败: {e}")
    
    return False



class IpWhitelistView(APIView):
    """GET/POST /api/v1/security/ip-whitelist/

    支持的 IP 模式格式：
    - 单 IP：10.0.0.1
    - CIDR：10.0.0.0/24
    - 通配符：10.0.*.*
    - IP 范围：10.0.0.1-10.0.0.100
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = IpWhitelist.objects.filter(is_enabled=True).order_by('-created_at')
        rows = list(qs.values(
            "id", "ip_or_cidr", "description", "is_enabled",
            "created_by__real_name", "created_by__username", "created_at"
        ))
        for r in rows:
            r["creator"] = r.pop("created_by__real_name") or r.pop("created_by__username") or ""
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        from apps.security.middleware import validate_ip_pattern
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        ip_or_cidr = request.data.get("ip_or_cidr")
        description = request.data.get("description", "").strip()
        if not ip_or_cidr:
            return Response({"detail": "ip_or_cidr 必填"}, status=400)
        if not description:
            return Response({"detail": "说明必填，便于后续审计追溯"}, status=400)

        if not validate_ip_pattern(ip_or_cidr):
            return Response({"detail": "IP 格式不合法，支持：单 IP / CIDR / 通配符（如 10.0.*.*）/ 范围（如 10.0.0.1-10.0.0.100）"}, status=400)

        if IpWhitelist.objects.filter(ip_or_cidr=ip_or_cidr).exists():
            return Response({"detail": "该 IP/CIDR 已存在"}, status=400)

        # 创建安全配置工单（白名单新增 = 高风险，需双审）
        ticket = create_security_ticket(
            actor=request.user,
            security_type=SecurityConfigType.IP_WHITELIST,
            operation=SecurityOperation.ADD,
            target_data={'ip_pattern': ip_or_cidr, 'description': description},
            reason=f'新增白名单: {ip_or_cidr}',
            new_data={'ip_pattern': ip_or_cidr, 'description': description},
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )

        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "白名单新增需双审，已创建审批工单"
        }, status=201)


class IpWhitelistDetailView(APIView):
    """PUT/DELETE /api/v1/security/ip-whitelist/{id}/

    白名单编辑/删除 = 高风险，需双审（compliance_admin 审核 + super_admin 复核）
    """
    permission_classes = [IsAdminUser]

    def put(self, request, pk):
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        try:
            obj = IpWhitelist.objects.get(id=pk)
            new_desc = request.data.get("description", obj.description)
            new_enabled = request.data.get("is_enabled", obj.is_enabled)

            # 创建安全配置工单（白名单编辑 = 高风险，需双审）
            ticket = create_security_ticket(
                actor=request.user,
                security_type=SecurityConfigType.IP_WHITELIST,
                operation=SecurityOperation.EDIT,
                target_data={'id': obj.id, 'ip_pattern': obj.ip_or_cidr},
                reason=f'编辑白名单: {obj.ip_or_cidr}',
                old_data={'description': obj.description, 'is_enabled': obj.is_enabled},
                new_data={'description': new_desc, 'is_enabled': new_enabled},
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )

            return Response({
                "ticket_no": ticket.ticket_no,
                "status": ticket.status,
                "risk_level": ticket.risk_level,
                "detail": "白名单编辑需双审，已创建审批工单"
            })
        except IpWhitelist.DoesNotExist:
            return Response({"detail": "白名单不存在"}, status=404)

    def delete(self, request, pk):
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        try:
            obj = IpWhitelist.objects.get(id=pk)

            # 创建安全配置工单（白名单删除 = 高风险，需双审）
            ticket = create_security_ticket(
                actor=request.user,
                security_type=SecurityConfigType.IP_WHITELIST,
                operation=SecurityOperation.DELETE,
                target_data={'id': obj.id, 'ip_pattern': obj.ip_or_cidr},
                reason=f'删除白名单: {obj.ip_or_cidr}',
                old_data={'ip_pattern': obj.ip_or_cidr, 'description': obj.description},
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )

            return Response({
                "ticket_no": ticket.ticket_no,
                "status": ticket.status,
                "risk_level": ticket.risk_level,
                "detail": "白名单删除需双审，已创建审批工单"
            })
        except IpWhitelist.DoesNotExist:
            return Response({"detail": "白名单不存在"}, status=404)


class IpBlacklistView(APIView):
    """GET/POST /api/v1/security/ip-blacklist/

    支持的 IP 模式格式：
    - 单 IP：10.0.0.1
    - 通配符：10.0.*.*
    - IP 范围：10.0.0.1-10.0.0.100

    黑名单新增 = 低风险（直接生效），解封/删除 = 中风险（单审）
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = IpBlacklist.objects.filter(is_active=True).order_by('-created_at')
        rows = list(qs.values(
            "id", "ip", "reason", "detail", "fail_count", "is_active", "expires_at", "created_at"
        ))
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        from apps.security.middleware import validate_ip_pattern
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        ip = request.data.get("ip")
        reason = request.data.get("reason", "").strip()
        detail = request.data.get("detail", "")
        if not ip:
            return Response({"detail": "ip 必填"}, status=400)
        if not reason:
            return Response({"detail": "封禁原因必填"}, status=400)

        if not validate_ip_pattern(ip):
            return Response({"detail": "IP 格式不合法，支持：单 IP / 通配符（如 10.0.*.*）/ 范围（如 10.0.0.1-10.0.0.100）"}, status=400)

        # 创建安全配置工单（黑名单新增 = 低风险，直接生效）
        ticket = create_security_ticket(
            actor=request.user,
            security_type=SecurityConfigType.IP_BLACKLIST,
            operation=SecurityOperation.ADD,
            target_data={'ip_pattern': ip, 'reason': reason, 'detail': detail},
            reason=f'新增黑名单: {ip}',
            new_data={'ip_pattern': ip, 'reason': reason, 'detail': detail},
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )

        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "黑名单新增已立即生效"
        }, status=201)


class IpBlacklistDetailView(APIView):
    """PUT/DELETE /api/v1/security/ip-blacklist/{id}/

    黑名单解封/删除 = 中风险，需单审（compliance_admin 审核）
    """
    permission_classes = [IsAdminUser]

    def put(self, request, pk):
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        try:
            obj = IpBlacklist.objects.get(id=pk)

            # 创建安全配置工单（黑名单解封 = 中风险，需单审）
            ticket = create_security_ticket(
                actor=request.user,
                security_type=SecurityConfigType.IP_BLACKLIST,
                operation=SecurityOperation.DELETE,
                target_data={'id': obj.id, 'ip_pattern': obj.ip},
                reason=f'解封黑名单: {obj.ip}',
                old_data={'ip_pattern': obj.ip, 'reason': obj.reason, 'is_active': obj.is_active},
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )

            return Response({
                "ticket_no": ticket.ticket_no,
                "status": ticket.status,
                "risk_level": ticket.risk_level,
                "detail": "黑名单解封需单审，已创建审批工单"
            })
        except IpBlacklist.DoesNotExist:
            return Response({"detail": "黑名单不存在"}, status=404)

    def delete(self, request, pk):
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        try:
            obj = IpBlacklist.objects.get(id=pk)

            # 创建安全配置工单（黑名单删除 = 中风险，需单审）
            ticket = create_security_ticket(
                actor=request.user,
                security_type=SecurityConfigType.IP_BLACKLIST,
                operation=SecurityOperation.DELETE,
                target_data={'id': obj.id, 'ip_pattern': obj.ip},
                reason=f'删除黑名单: {obj.ip}',
                old_data={'ip_pattern': obj.ip, 'reason': obj.reason},
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )

            return Response({
                "ticket_no": ticket.ticket_no,
                "status": ticket.status,
                "risk_level": ticket.risk_level,
                "detail": "黑名单删除需单审，已创建审批工单"
            })
        except IpBlacklist.DoesNotExist:
            return Response({"detail": "黑名单不存在"}, status=404)


class LoginAttemptView(APIView):
    """GET /api/v1/security/login-attempts/"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = LoginAttempt.objects.all().order_by('-created_at')

        result = request.query_params.get("result")
        if result:
            qs = qs.filter(result=result)

        username = request.query_params.get("username")
        if username:
            qs = qs.filter(username__icontains=username)

        ip = request.query_params.get("ip")
        if ip:
            qs = qs.filter(ip__icontains=ip)

        page = int(request.query_params.get("page") or 1)
        size = int(request.query_params.get("page_size") or 20)
        offset = (page - 1) * size

        rows = list(qs[offset:offset + size].values(
            "id", "username", "ip", "user_agent", "result", "created_at"
        ))
        return Response({
            "total": qs.count(),
            "page": page,
            "page_size": size,
            "rows": rows,
        })


class SensitiveWordView(APIView):
    """GET/POST /api/v1/security/sensitive-words/

    敏感词新增 = 低风险（直接生效），编辑/删除/禁用 = 中风险（单审）
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = SensitiveWord.objects.filter(is_enabled=True).order_by('-created_at')
        rows = list(qs.values(
            "id", "word", "category", "action", "is_regex", "is_enabled", "created_at"
        ))
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        word = request.data.get("word")
        category = request.data.get("category", "other")
        action = request.data.get("action", "mask")
        is_regex = request.data.get("is_regex", False)
        if not word:
            return Response({"detail": "word 必填"}, status=400)

        # word 预处理：strip 去除首尾空白 + 长度校验
        # 避免纯空格词污染词库、超长词拖慢 AC 自动机构建
        word = word.strip()
        if not word:
            return Response({"detail": "word 不能为空白"}, status=400)
        if len(word) > 128:
            return Response({"detail": "word 长度不能超过 128"}, status=400)

        # choices 校验：objects.create 不触发 full_clean，需在视图层显式校验
        # 非法 action 会导致 SensitiveFilter 的 block/mask/warn 分支全部失配，安全防线被绕过
        valid_actions = [c[0] for c in SensitiveWord.ACTION_CHOICES]
        if action not in valid_actions:
            return Response({"detail": f"action 必须是 {valid_actions} 之一"}, status=400)
        valid_categories = [c[0] for c in SensitiveWord.CATEGORY_CHOICES]
        if category not in valid_categories:
            return Response({"detail": f"category 必须是 {valid_categories} 之一"}, status=400)

        # 正则词合法性校验：非法正则会在 _load_from_db 被静默跳过，
        # 用户以为已生效但实际未入词库，提前校验给即时反馈
        if is_regex:
            import re as re_module
            try:
                re_module.compile(word)
            except re_module.error as e:
                return Response({"detail": f"正则表达式非法: {e}"}, status=400)

        if SensitiveWord.objects.filter(word=word).exists():
            return Response({"detail": "该敏感词已存在"}, status=400)

        # 创建安全配置工单（敏感词新增 = 低风险，直接生效）
        ticket = create_security_ticket(
            actor=request.user,
            security_type=SecurityConfigType.SENSITIVE_WORD,
            operation=SecurityOperation.ADD,
            target_data={'word': word, 'category': category, 'action': action, 'is_regex': is_regex},
            reason=f'新增敏感词: {word}',
            new_data={'word': word, 'category': category, 'action': action, 'is_regex': is_regex},
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )

        # 词库变更：触发 SensitiveFilter 重建 AC 自动机（异步失败不影响接口）
        self._trigger_reload()
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "敏感词新增已立即生效"
        }, status=201)

    @staticmethod
    def _trigger_reload():
        """词库变更后触发 SensitiveFilter 重建

        放在 try/except 中：过滤器未启用或初始化失败不应阻断 CRUD 接口。
        """
        try:
            from apps.security.sensitive_filter import SensitiveFilter
            SensitiveFilter.force_reload()
        except Exception:
            logger.exception('[SensitiveWord] force_reload failed, other workers will reload on TTL')


class SensitiveWordDetailView(APIView):
    """PUT/DELETE /api/v1/security/sensitive-words/{id}/

    敏感词编辑/删除/禁用 = 中风险，需单审（compliance_admin 审核）
    """
    permission_classes = [IsAdminUser]

    def put(self, request, pk):
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        try:
            obj = SensitiveWord.objects.get(id=pk)
            new_action = request.data.get("action", obj.action)
            new_enabled = request.data.get("is_enabled", obj.is_enabled)

            # choices 校验：防止非法 action 导致审查分支失配
            valid_actions = [c[0] for c in SensitiveWord.ACTION_CHOICES]
            if new_action not in valid_actions:
                return Response({"detail": f"action 必须是 {valid_actions} 之一"}, status=400)

            # 判断是编辑还是禁用
            if new_enabled != obj.is_enabled and not new_enabled:
                operation = SecurityOperation.DISABLE
                reason = f'禁用敏感词: {obj.word}'
            else:
                operation = SecurityOperation.EDIT
                reason = f'编辑敏感词: {obj.word}'

            # 创建安全配置工单（敏感词编辑/禁用 = 中风险，需单审）
            ticket = create_security_ticket(
                actor=request.user,
                security_type=SecurityConfigType.SENSITIVE_WORD,
                operation=operation,
                target_data={'id': obj.id, 'word': obj.word},
                reason=reason,
                old_data={'action': obj.action, 'is_enabled': obj.is_enabled},
                new_data={'action': new_action, 'is_enabled': new_enabled},
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )

            return Response({
                "ticket_no": ticket.ticket_no,
                "status": ticket.status,
                "risk_level": ticket.risk_level,
                "detail": "敏感词变更需单审，已创建审批工单"
            })
        except SensitiveWord.DoesNotExist:
            return Response({"detail": "敏感词不存在"}, status=404)

    def delete(self, request, pk):
        from apps.users.ticket_service import create_security_ticket
        from apps.users.models import SecurityConfigType, SecurityOperation

        try:
            obj = SensitiveWord.objects.get(id=pk)

            # 创建安全配置工单（敏感词删除 = 中风险，需单审）
            ticket = create_security_ticket(
                actor=request.user,
                security_type=SecurityConfigType.SENSITIVE_WORD,
                operation=SecurityOperation.DELETE,
                target_data={'id': obj.id, 'word': obj.word},
                reason=f'删除敏感词: {obj.word}',
                old_data={'word': obj.word, 'category': obj.category, 'action': obj.action},
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )

            return Response({
                "ticket_no": ticket.ticket_no,
                "status": ticket.status,
                "risk_level": ticket.risk_level,
                "detail": "敏感词删除需单审，已创建审批工单"
            })
        except SensitiveWord.DoesNotExist:
            return Response({"detail": "敏感词不存在"}, status=404)