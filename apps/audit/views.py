"""
audit views
- GET /api/v1/audit/logs/         审计日志列表
- POST /api/v1/audit/verify-chain/ 校验哈希链完整性
"""
import hashlib

from django.core.paginator import Paginator
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditLog
from apps.users.permissions import perm_class, IsSuperAdmin

MAX_PAGE_SIZE = 200


class AuditLogListView(APIView):
    """GET /api/v1/audit/logs/?action=&q=&ip=&start_date=&end_date=&page="""
    permission_classes = [perm_class("audit:read:all")]

    def get(self, request):
        qs = AuditLog.objects.all().order_by("-id")
        for f in ("action", "action_category", "result", "target_type"):
            v = request.query_params.get(f)
            if v:
                qs = qs.filter(**{f: v})
        uid = request.query_params.get("user_id")
        if uid:
            qs = qs.filter(actor_id=uid)
        keyword = request.query_params.get("q")
        if keyword:
            qs = qs.filter(actor_username__icontains=keyword[:100])
        ip = request.query_params.get("ip")
        if ip:
            qs = qs.filter(ip_address__icontains=ip[:64])
        start_date = request.query_params.get("start_date")
        if start_date:
            qs = qs.filter(created_at__gte=start_date)
        end_date = request.query_params.get("end_date")
        if end_date:
            qs = qs.filter(created_at__lte=end_date)

        try:
            page = int(request.query_params.get("page") or 1)
        except (ValueError, TypeError):
            page = 1
        try:
            size = int(request.query_params.get("page_size") or 20)
        except (ValueError, TypeError):
            size = 20
        size = min(size, MAX_PAGE_SIZE)

        paginator = Paginator(qs, size)
        pg = paginator.get_page(page)
        rows = [{
            "id": r.id, "actor_id": r.actor_id, "actor_username": r.actor_username,
            "action": r.action, "action_category": r.action_category,
            "target_type": r.target_type, "target_id": r.target_id,
            "result": r.result, "ip_address": r.ip_address,
            "user_agent": r.user_agent[:80],
            "prev_hash": getattr(r, "prev_hash", ""),
            "row_hash": getattr(r, "row_hash", ""),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in pg.object_list]
        return Response({
            "total": paginator.count,
            "page": page,
            "page_size": size,
            "total_pages": paginator.num_pages,
            "rows": rows,
        })


class VerifyChainView(APIView):
    """
    POST /api/v1/audit/verify-chain/
    sha256 哈希链完整性校验 — 复用 model 的 verify_chain 逻辑
    """
    permission_classes = [IsSuperAdmin]

    def post(self, request):
        try:
            limit = int(request.data.get("limit") or 10000)
        except (ValueError, TypeError):
            limit = 10000
        limit = min(limit, 50000)

        qs = AuditLog.objects.order_by("id")[:limit]
        broken = []
        prev = ""
        count = 0
        for r in qs:
            count += 1
            payload = (prev + "|" + r._build_payload()).encode("utf-8")
            expected = hashlib.sha256(payload).hexdigest()
            actual = getattr(r, "row_hash", "") or ""
            if actual and actual != expected:
                broken.append({"id": r.id, "expected": expected, "actual": actual})
            prev = actual or expected
        return Response({
            "checked": count,
            "broken_count": len(broken),
            "broken": broken[:50],
            "ok": len(broken) == 0,
        })
