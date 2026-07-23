"""
security views - IP 白/黑名单 & 登录尝试 & 敏感词 & 验证码
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
    """GET/POST /api/v1/security/ip-whitelist/"""
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
        ip_or_cidr = request.data.get("ip_or_cidr")
        description = request.data.get("description", "")
        if not ip_or_cidr:
            return Response({"detail": "ip_or_cidr 必填"}, status=400)

        if IpWhitelist.objects.filter(ip_or_cidr=ip_or_cidr).exists():
            return Response({"detail": "该 IP/CIDR 已存在"}, status=400)

        obj = IpWhitelist.objects.create(
            ip_or_cidr=ip_or_cidr,
            description=description,
            is_enabled=True,
            created_by=request.user
        )
        return Response({
            "id": obj.id, "ip_or_cidr": obj.ip_or_cidr, "description": obj.description,
            "created_at": obj.created_at.isoformat()
        }, status=201)


class IpWhitelistDetailView(APIView):
    """PUT/DELETE /api/v1/security/ip-whitelist/{id}/"""
    permission_classes = [IsAdminUser]

    def put(self, request, pk):
        try:
            obj = IpWhitelist.objects.get(id=pk)
            obj.description = request.data.get("description", obj.description)
            obj.is_enabled = request.data.get("is_enabled", obj.is_enabled)
            obj.save()
            return Response({
                "id": obj.id, "ip_or_cidr": obj.ip_or_cidr, "description": obj.description,
                "is_enabled": obj.is_enabled
            })
        except IpWhitelist.DoesNotExist:
            return Response({"detail": "白名单不存在"}, status=404)

    def delete(self, request, pk):
        try:
            obj = IpWhitelist.objects.get(id=pk)
            obj.delete()
            return Response(status=204)
        except IpWhitelist.DoesNotExist:
            return Response({"detail": "白名单不存在"}, status=404)


class IpBlacklistView(APIView):
    """GET/POST /api/v1/security/ip-blacklist/"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = IpBlacklist.objects.filter(is_active=True).order_by('-created_at')
        rows = list(qs.values(
            "id", "ip", "reason", "detail", "fail_count", "is_active", "expires_at", "created_at"
        ))
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        ip = request.data.get("ip")
        reason = request.data.get("reason", "manual")
        detail = request.data.get("detail", "")
        if not ip:
            return Response({"detail": "ip 必填"}, status=400)

        obj, created = IpBlacklist.objects.update_or_create(
            ip=ip,
            defaults={
                "reason": reason,
                "detail": detail,
                "is_active": True,
            }
        )
        return Response({
            "id": obj.id, "ip": obj.ip, "reason": obj.reason, "detail": obj.detail,
            "created": created, "created_at": obj.created_at.isoformat()
        }, status=201 if created else 200)


class IpBlacklistDetailView(APIView):
    """PUT/DELETE /api/v1/security/ip-blacklist/{id}/"""
    permission_classes = [IsAdminUser]

    def put(self, request, pk):
        try:
            obj = IpBlacklist.objects.get(id=pk)
            obj.is_active = False
            obj.save()
            return Response({"id": obj.id, "ip": obj.ip, "is_active": obj.is_active})
        except IpBlacklist.DoesNotExist:
            return Response({"detail": "黑名单不存在"}, status=404)

    def delete(self, request, pk):
        try:
            obj = IpBlacklist.objects.get(id=pk)
            obj.delete()
            return Response(status=204)
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
    """GET/POST /api/v1/security/sensitive-words/"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = SensitiveWord.objects.filter(is_enabled=True).order_by('-created_at')
        rows = list(qs.values(
            "id", "word", "category", "action", "is_regex", "is_enabled", "created_at"
        ))
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        word = request.data.get("word")
        category = request.data.get("category", "other")
        action = request.data.get("action", "mask")
        is_regex = request.data.get("is_regex", False)
        if not word:
            return Response({"detail": "word 必填"}, status=400)

        if SensitiveWord.objects.filter(word=word).exists():
            return Response({"detail": "该敏感词已存在"}, status=400)

        obj = SensitiveWord.objects.create(
            word=word, category=category, action=action, is_regex=is_regex, is_enabled=True
        )
        return Response({
            "id": obj.id, "word": obj.word, "category": obj.category,
            "action": obj.action, "created_at": obj.created_at.isoformat()
        }, status=201)


class SensitiveWordDetailView(APIView):
    """PUT/DELETE /api/v1/security/sensitive-words/{id}/"""
    permission_classes = [IsAdminUser]

    def put(self, request, pk):
        try:
            obj = SensitiveWord.objects.get(id=pk)
            obj.action = request.data.get("action", obj.action)
            obj.is_enabled = request.data.get("is_enabled", obj.is_enabled)
            obj.save()
            return Response({
                "id": obj.id, "word": obj.word, "action": obj.action,
                "is_enabled": obj.is_enabled
            })
        except SensitiveWord.DoesNotExist:
            return Response({"detail": "敏感词不存在"}, status=404)

    def delete(self, request, pk):
        try:
            obj = SensitiveWord.objects.get(id=pk)
            obj.delete()
            return Response(status=204)
        except SensitiveWord.DoesNotExist:
            return Response({"detail": "敏感词不存在"}, status=404)