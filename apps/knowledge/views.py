"""
knowledge views
- 节点树 & CRUD
- 文档上传（sha256 去重 -> 存盘 -> 触发 parse_document Celery 任务）
- 文档 chunks 查看
"""
import hashlib
from loguru import logger
import os
import uuid as uuid_lib

from django.conf import settings
from django.db import transaction, models
from django.db.models import Count
from django.http import FileResponse, Http404
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk
from apps.knowledge.serializers import (
    KnowledgeNodeSerializer, KnowledgeNodeCreateSerializer,
    DocumentSerializer, DocumentChunkSerializer,
)
from apps.users.permissions import IsAdminOrOps


ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".md", ".markdown", ".txt",
    ".py", ".java", ".go", ".js", ".ts", ".c", ".cpp", ".rs",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".conf",
}
MAX_FILE_SIZE = int(getattr(settings, 'DOCUMENT_MAX_SIZE_MB', 100)) * 1024 * 1024

FILE_TYPE_MAP = {
    ".pdf": "pdf",
    ".doc": "docx", ".docx": "docx",
    ".md": "markdown", ".markdown": "markdown",
    ".txt": "txt",
    ".py": "code", ".java": "code", ".go": "code", ".js": "code",
    ".ts": "code", ".c": "code", ".cpp": "code", ".rs": "code",
    ".yaml": "config", ".yml": "config", ".json": "config",
    ".toml": "config", ".ini": "config", ".conf": "config",
}


def _detect_file_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return FILE_TYPE_MAP.get(ext, "other")


def _build_tree(qs):
    """扁平列表 -> 递归树"""
    nodes = list(qs)
    id2n = {n["id"]: {**n, "children": []} for n in nodes}
    roots = []
    for n in nodes:
        pid = n.get("parent_id")
        if pid and pid in id2n:
            id2n[pid]["children"].append(id2n[n["id"]])
        else:
            roots.append(id2n[n["id"]])
    return roots


class NodeTreeView(APIView):
    """GET /api/v1/knowledge/nodes/tree/?root_type=company_doc"""
    permission_classes = [IsAdminOrOps]

    def get(self, request):
        root_type = request.query_params.get("root_type")
        qs = KnowledgeNode.objects.filter(is_deleted=False)
        if root_type:
            qs = qs.filter(root_type=root_type)
        nodes = qs.order_by("depth", "order_no", "id").values(
            "id", "parent_id", "root_type", "node_type", "name", "depth"
        )
        data = list(nodes)
        return Response({"tree": _build_tree(data), "total": len(data)})


class KnowledgeNodeViewSet(viewsets.ModelViewSet):
    """/api/v1/knowledge/nodes/ — 仅超级管理员和知识库运维可操作"""
    queryset = KnowledgeNode.objects.filter(is_deleted=False).order_by("path")
    serializer_class = KnowledgeNodeSerializer
    permission_classes = [IsAdminOrOps]
    filterset_fields = ["root_type", "node_type", "parent"]

    def get_queryset(self):
        qs = super().get_queryset()
        # 预计算 children_count 和 document_count，避免 N+1
        if self.action in ("retrieve", "list"):
            qs = qs.annotate(
                _children_count=Count("children", filter=models.Q(children__is_deleted=False)),
                _document_count=Count("documents", filter=models.Q(documents__is_deleted=False)),
            )
        return qs

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return KnowledgeNodeCreateSerializer
        return KnowledgeNodeSerializer

    def perform_create(self, serializer):
        parent = serializer.validated_data.get("parent")
        node_type = serializer.validated_data.get("node_type", "folder")

        # 根节点不应有父节点
        if node_type == "root" and parent:
            raise ValidationError({"parent": "根节点不能指定上级节点"})

        # root_type: 有父节点则继承，无父节点则强制 root 类型并给默认分类
        if parent:
            root_type = parent.root_type
        else:
            node_type = "root"
            root_type = serializer.validated_data.get("root_type") or "company_doc"
        depth = (parent.depth + 1) if parent else 0
        obj = serializer.save(depth=depth, node_type=node_type, root_type=root_type, created_by=self.request.user)
        # 更新 path（ID 零填充 4 位，确保按数值顺序排序）
        padded_id = f"{obj.id:04d}"
        if parent:
            obj.path = f"{parent.path}{padded_id}/"
        else:
            obj.path = f"/{padded_id}/"
        obj.save(update_fields=["path"])

    def destroy(self, request, *args, **kwargs):
        node = self.get_object()

        # 禁止删除根节点
        if node.node_type == "root":
            return Response(
                {"detail": "根节点不允许删除"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 检查是否存在子节点/文件夹
        child_count = KnowledgeNode.objects.filter(
            parent=node, is_deleted=False
        ).count()
        if child_count > 0:
            return Response(
                {"detail": f"该节点下存在 {child_count} 个子节点/文件夹，请先删除所有子节点后再删除此节点"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 检查该节点下是否存在文档
        doc_count = Document.objects.filter(node=node, is_deleted=False).count()
        if doc_count > 0:
            return Response(
                {"detail": f"该节点下存在 {doc_count} 个文档，请先删除或移走所有文档后再删除节点"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 软删除
        node.is_deleted = True
        node.save(update_fields=["is_deleted"])
        return Response(status=204)


class DocumentViewSet(viewsets.ModelViewSet):
    """/api/v1/knowledge/documents/"""
    queryset = Document.objects.filter(is_deleted=False).order_by("-created_at")
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["node", "status", "file_type", "visibility", "root_type"]
    search_fields = ["title", "file_name"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_superuser:
            return qs
        qs = qs.filter(
            models.Q(owner=user) |
            models.Q(visibility=4) |
            models.Q(visibility=3) |
            models.Q(visibility=2, owner_team_id__in=user.user_teams.values_list('team_id', flat=True))
        )
        return qs

    def get_object(self):
        obj = super().get_object()
        if not self._has_permission(self.request.user, obj):
            raise PermissionDenied("无权限操作此文档")
        return obj

    def destroy(self, request, *args, **kwargs):
        doc = self.get_object()
        doc.is_deleted = True
        doc.save(update_fields=["is_deleted"])
        try:
            from apps.retrieval.vector_store import delete_by_document
            delete_by_document(doc.id)
        except Exception:
            logger.exception("delete vector failed")
        return Response(status=204)

    @action(detail=True, methods=["post"])
    def reparse(self, request, pk=None):
        """重新解析"""
        doc = self.get_object()
        doc.status = "pending"
        doc.error_message = ""
        doc.save(update_fields=["status", "error_message"])
        try:
            from apps.knowledge.tasks import parse_document
            parse_document.delay(doc.id)
        except Exception:
            logger.exception("dispatch parse task failed")
        return Response({"ok": True, "status": "pending"})

    def _has_permission(self, user, doc):
        if user.is_superuser:
            return True
        if doc.owner_id == user.id:
            return True
        if doc.visibility == 4:
            return True
        if doc.visibility == 3:
            return True
        if doc.visibility == 2:
            return user.user_teams.filter(team_id=doc.owner_team_id).exists()
        return False


class DocumentUploadView(APIView):
    """
    POST /api/v1/knowledge/documents/upload/
    multipart/form-data: file, node_id, [title], [visibility], [force_upload]
    - sha256 去重
    - 存到 MEDIA_ROOT/documents/{uuid}_{name} 或 OSS
    - 触发 Celery 异步解析
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        f = request.FILES.get("file")
        node_id = request.data.get("node_id")
        if not f or not node_id:
            return Response({"detail": "file / node_id 必填"}, status=400)

        try:
            node = KnowledgeNode.objects.get(id=node_id, is_deleted=False)
        except KnowledgeNode.DoesNotExist:
            return Response({"detail": "node 不存在"}, status=404)

        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return Response({"detail": f"不支持的文件类型: {ext}"}, status=400)

        if f.size > MAX_FILE_SIZE:
            return Response({"detail": f"文件大小超过限制（最大 {MAX_FILE_SIZE//(1024*1024)} MB）"}, status=400)

        visibility = int(request.data.get("visibility") or 1)
        if visibility not in [1, 2, 3, 4]:
            return Response({"detail": "visibility 必须为 1-4"}, status=400)

        h = hashlib.sha256()
        total = 0
        for c in f.chunks():
            h.update(c)
            total += len(c)
        file_hash = h.hexdigest()

        force_upload = request.data.get("force_upload") in ("true", "True", "1", True)

        # -- 未删除的相同文件 → 返回冲突信息 --
        exist = Document.objects.filter(
            file_hash=file_hash, is_deleted=False
        ).select_related("owner").first()
        if exist and not force_upload:
            return Response({
                "conflict": "duplicate",
                "existing": {
                    "id": exist.id,
                    "title": exist.title,
                    "file_name": exist.file_name,
                    "owner_name": exist.owner.username if exist.owner else "",
                    "created_at": exist.created_at.strftime("%Y-%m-%d %H:%M"),
                    "status": exist.status,
                },
            }, status=200)

        # -- 已删除的相同文件 → 返回冲突信息 --
        deleted_exist = Document.objects.filter(
            file_hash=file_hash, is_deleted=True
        ).select_related("owner").first()
        if deleted_exist and not force_upload:
            return Response({
                "conflict": "deleted",
                "existing": {
                    "id": deleted_exist.id,
                    "title": deleted_exist.title,
                    "file_name": deleted_exist.file_name,
                    "owner_name": deleted_exist.owner.username if deleted_exist.owner else "",
                    "created_at": deleted_exist.created_at.strftime("%Y-%m-%d %H:%M"),
                },
            }, status=200)

        # -- force_upload + 已删除相同文件 → 恢复 --
        if force_upload and deleted_exist:
            with transaction.atomic():
                deleted_exist.is_deleted = False
                deleted_exist.node = node
                deleted_exist.owner = request.user
                deleted_exist.visibility = visibility
                deleted_exist.status = "pending"
                deleted_exist.error_message = ""
                deleted_exist.save(update_fields=[
                    "is_deleted", "node", "owner", "visibility",
                    "status", "error_message", "updated_at",
                ])
            try:
                from apps.knowledge.tasks import parse_document
                parse_document.delay(deleted_exist.id)
            except Exception as e:
                logger.warning("celery unreachable for restored doc %s: %s", deleted_exist.id, str(e)[:200])
            return Response({
                "document_id": deleted_exist.id,
                "status": "pending",
                "detail": "文件已恢复（sha256 匹配已删除记录）",
                "dedup": True,
            }, status=201)

        # -- 正常上传 / force_upload + 未删除相同文件（新建独立记录） --
        same_name_exist = Document.objects.filter(
            file_name=f.name[:256], node=node, is_deleted=False
        ).first()
        
        file_path = self._save_file(f)
        if not file_path:
            return Response({"detail": "文件存储失败"}, status=500)

        title = request.data.get("title") or f.name
        file_type = _detect_file_type(f.name)
        
        with transaction.atomic():
            if same_name_exist:
                same_name_exist.is_deleted = True
                same_name_exist.save(update_fields=["is_deleted", "updated_at"])
            
            doc = Document.objects.create(
                node=node,
                title=title[:256],
                file_name=f.name[:256],
                file_type=file_type,
                file_size=total,
                file_hash=file_hash,
                file_path=file_path,
                mime_type=(f.content_type or "")[:64],
                owner=request.user,
                owner_team_id=getattr(request.user, 'team_id', None),
                visibility=visibility,
                root_type=node.root_type,
                status="pending",
                version=same_name_exist.version + 1 if same_name_exist else 1,
            )

        celery_ok = True
        celery_error = ""
        try:
            from apps.knowledge.tasks import parse_document
            parse_document.delay(doc.id)
        except Exception as e:
            celery_ok = False
            celery_error = str(e)[:200]
            logger.warning("celery unreachable, doc %s will need manual reparse: %s", doc.id, celery_error)

        return Response({
            "document_id": doc.id,
            "uuid": str(doc.uuid),
            "status": doc.status,
            "file_hash": file_hash,
            "dedup": bool(exist),
            "celery_ok": celery_ok,
            "celery_error": celery_error,
            "version": doc.version,
        }, status=201)

    def _save_file(self, f):
        from apps.knowledge.storage import get_document_storage
        storage = get_document_storage()
        safe_name = f.name.replace("/", "_").replace("\\", "_")
        fname = f"{uuid_lib.uuid4().hex}_{safe_name}"
        return storage.save(fname, f)


class DocumentChunksView(APIView):
    """GET /api/v1/knowledge/documents/{id}/chunks/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_id):
        try:
            doc = Document.objects.get(id=doc_id, is_deleted=False)
        except Document.DoesNotExist:
            return Response({"detail": "文档不存在"}, status=404)
        
        if not self._has_permission(request.user, doc):
            return Response({"detail": "无权限查看此文档"}, status=403)

        chunks = DocumentChunk.objects.filter(document_id=doc_id).order_by("chunk_index")[:500]
        chunk_list = list(chunks)
        return Response({
            "document_id": int(doc_id),
            "total": len(chunk_list),
            "chunks": DocumentChunkSerializer(chunk_list, many=True).data,
        })

    def _has_permission(self, user, doc):
        if user.is_superuser:
            return True
        if doc.owner_id == user.id:
            return True
        if doc.visibility == 4:
            return True
        if doc.visibility == 3:
            return True
        if doc.visibility == 2:
            return user.user_teams.filter(team_id=doc.owner_team_id).exists()
        return False


class CeleryStatusView(APIView):
    """GET /api/v1/knowledge/celery/status/ — 检查文档解析服务状态"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from rag_project.celery import app
        from django.conf import settings
        import time
        import redis

        broker_url = getattr(settings, 'CELERY_BROKER_URL', '')
        result_backend = getattr(settings, 'CELERY_RESULT_BACKEND', '')

        diagnostics = {}

        try:
            conn = app.connection_for_read()
            conn.ensure_connection(max_retries=2)
            conn.close()
            diagnostics['broker_connected'] = True
        except Exception as e:
            logger.warning("celery broker connection failed: %s", e)
            return Response({
                "celery_ok": False,
                "detail": "消息队列（Redis）连接失败",
            }, status=200)

        try:
            response = app.control.ping(timeout=5)
            if response:
                worker_count = len(response)
                return Response({
                    "celery_ok": True,
                    "detail": f"文档解析服务运行正常（{worker_count} 个在线）",
                    "worker_count": worker_count,
                })
        except Exception as e:
            logger.warning("celery control.ping failed: %s", e)

        try:
            r = redis.Redis.from_url(broker_url)
            queues = ['default', 'parse', 'memory', 'email']
            queue_lengths = {}
            for q in queues:
                length = r.llen(q)
                queue_lengths[q] = int(length)

            if queue_lengths.get('default', 0) == 0 and queue_lengths.get('parse', 0) == 0:
                test_result = app.send_task('rag_project.celery.debug_task', args=[], queue='default')
                time.sleep(6)
                result_ready = test_result.ready()

                if result_ready:
                    return Response({
                        "celery_ok": True,
                        "detail": "文件解析服务运行正常",
                        "worker_count": 1,
                    })
        except Exception as e:
            logger.warning("celery redis check failed: %s", e)

        return Response({
            "celery_ok": False,
            "detail": "消息队列连接正常，但文档解析服务未运行",
        }, status=200)


class PendingDocsView(APIView):
    """GET /api/v1/knowledge/documents/pending/ — 获取待处理文档列表"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pending_query = Document.objects.filter(
            is_deleted=False,
            status__in=["pending"],
            owner=request.user
        ).order_by("-created_at")
        pending_count = pending_query.count()
        pending = pending_query[:20]
        serializer = DocumentSerializer(pending, many=True)
        return Response({
            "total": pending_count,
            "documents": serializer.data,
        })

    def post(self, request):
        """重新触发当前用户待处理文档的解析任务"""
        pending_query = Document.objects.filter(
            is_deleted=False,
            status__in=["pending"],
            owner=request.user
        )
        pending = list(pending_query)
        count = 0
        failed = []
        try:
            from apps.knowledge.tasks import parse_document
            for doc in pending:
                try:
                    parse_document.delay(doc.id)
                    count += 1
                except Exception as e:
                    failed.append({"doc_id": doc.id, "error": str(e)[:100]})
        except Exception as e:
            return Response({
                "ok": False,
                "detail": "Celery 连接失败",
                "error": str(e)[:200],
            }, status=500)
        return Response({
            "ok": True,
            "total_pending": len(pending),
            "retriggered": count,
            "failed": failed,
        })
