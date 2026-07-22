"""
security views - IP 白/黑名单 & 登录尝试 & 敏感词
"""
from loguru import logger
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.security.models import IpWhitelist, IpBlacklist, LoginAttempt, SensitiveWord



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