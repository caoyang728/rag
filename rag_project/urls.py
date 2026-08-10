"""
Root URL Conf - 所有 apps 的路由聚合入口
- /api/v1/auth/*        用户/角色/权限/JWT
- /api/v1/knowledge/*   节点/文档
- /api/v1/chat/*        会话/问答/反馈
- /api/v1/agent/*       复杂任务拆分
- /api/v1/retrieval/*   混合检索调试
- /api/v1/memory/*      记忆调试
- /api/v1/audit/*       审计
- /api/v1/analytics/*   看板
- /api/v1/notification/* 订阅
- /api/v1/system/*      健康检查/配置
- /api/v1/wiki/*        Wiki 页面
- /api/v1/graph/*       知识图谱可视化与实体检索
- /                     前端静态 HTML（前后端分离）
"""
import os

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponse, JsonResponse
from django.urls import include, path
from django.views.generic.base import RedirectView


def healthz(request):
    checks = {
        "service": "rag-agent-backend",
        "database": "ok",
        "redis": "ok"
    }
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as e:
        checks["database"] = f"failed: {str(e)[:50]}"
    
    try:
        import redis
        r = redis.Redis(
            host=os.getenv('REDIS_DB_HOST', 'redis'),
            port=int(os.getenv('REDIS_DB_PORT', 6379)),
            password=os.getenv('REDIS_DB_PASSWORD', ''),
            decode_responses=True
        )
        r.ping()
    except Exception as e:
        checks["redis"] = f"failed: {str(e)[:50]}"
    
    checks["ok"] = checks["database"] == "ok" and checks["redis"] == "ok"
    return JsonResponse(checks, status=200 if checks["ok"] else 503)


def _serve_frontend(page):
    """读取 static/ 目录下的纯静态 HTML，不走 Django 模板引擎（前后端分离）"""
    def view(_request):
        path = os.path.join(settings.BASE_DIR, 'static', f'{page}.html')
        with open(path, 'r', encoding='utf-8') as f:
            return HttpResponse(f.read(), content_type='text/html; charset=utf-8')
    return view


api_v1 = [
    path("auth/", include("apps.users.urls")),
    path("knowledge/", include("apps.knowledge.urls")),
    path("chat/", include("apps.chat.urls")),
    path("agent/", include("apps.agent.urls")),
    path("retrieval/", include("apps.retrieval.urls")),
    path("memory/", include("apps.memory.urls")),
    path("audit/", include("apps.audit.urls")),
    path("analytics/", include("apps.analytics.urls")),
    path("security/", include("apps.security.urls")),
    path("notification/", include("apps.notification.urls")),
    path("system/", include("apps.system.urls")),
    path("wiki/", include("apps.wiki.urls")),
    path("graph/", include("apps.graph.urls")),
]

urlpatterns = [
    # favicon 兜底：301 永久重定向，浏览器记住后不再请求根路径，消除 404 告警
    path("favicon.ico", RedirectView.as_view(url='/static/favicon.svg', permanent=True), name="favicon"),
    path("", _serve_frontend("index"), name="index"),
    path("login/", _serve_frontend("login"), name="login"),
    path("reset-password/", _serve_frontend("reset-password"), name="reset-password"),
    path("chat/", _serve_frontend("chat"), name="chat"),
    path("upload/", _serve_frontend("upload"), name="upload"),
    path("profile/", _serve_frontend("profile"), name="profile"),
    path("admin-users/", _serve_frontend("admin-users"), name="admin-users"),
    path("admin-nodes/", _serve_frontend("admin-nodes"), name="admin-nodes"),
    path("ticket/", _serve_frontend("ticket"), name="ticket"),
    path("admin-docs/", _serve_frontend("admin-docs"), name="admin-docs"),
    path("admin-analytics/", _serve_frontend("admin-analytics"), name="admin-analytics"),
    path("admin-eval/", _serve_frontend("admin-eval"), name="admin-eval"),
    path("admin-audit/", _serve_frontend("admin-audit"), name="admin-audit"),
    path("admin-rbac/", _serve_frontend("admin-rbac"), name="admin-rbac"),
    path("admin-org/", _serve_frontend("admin-org"), name="admin-org"),
    path("admin-system-config/", _serve_frontend("admin-system-config"), name="admin-system-config"),
    path("admin-scheduler/", _serve_frontend("admin-scheduler"), name="admin-scheduler"),
    path("wiki/", _serve_frontend("wiki"), name="wiki"),
    path("graph/", _serve_frontend("graph"), name="graph"),
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
    path("healthz", healthz),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # 仅开发环境：暴露 pytest-cov 生成的测试覆盖率报告（HTML 静态文件）
    # 生产环境 DEBUG=False 时此路由不挂载，访问 /coverage/ 直接 404
    # 原因：覆盖率报告含完整源码路径，属于内部开发信息，不应在生产环境暴露
    # 报告由 .coveragerc 输出到 static/coverage/，走 WhiteNoise 服务（gzip + 长缓存）；
    urlpatterns += [
        path("coverage/", RedirectView.as_view(url="/static/coverage/index.html", permanent=False), name="coverage-index"),
    ]