"""
knowledge views
- 节点树 & CRUD
- 文档上传（sha256 去重 -> 存盘 -> 触发 parse_document Celery 任务）
- 文档 chunks 查看
"""
import hashlib
import magic
import os
import re
from loguru import logger
import uuid as uuid_lib

from django.conf import settings
from django.db import transaction, models
from django.db.models import Count
from django.http import FileResponse, Http404
from django.utils import text as django_text
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk, DocOperationLog
from apps.knowledge.serializers import (
    KnowledgeNodeSerializer, KnowledgeNodeCreateSerializer,
    DocumentSerializer, DocumentChunkSerializer,
)
from apps.knowledge.access import resolve_doc_access, build_user_context, build_grants_map
from apps.knowledge.storage import get_document_storage
from apps.users.models import User, DocDenyUser, DocAllowUser, DocCrossTeam, AccessApplication
from apps.users.permissions import IsAdminOrOps


def _log_operation(request, action, document=None, node=None, detail=None):
    """记录操作日志"""
    try:
        user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None
        xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
        ip = xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')
        ua = request.META.get('HTTP_USER_AGENT', '')[:512]
        DocOperationLog.objects.create(
            action=action,
            operator=user,
            operator_name=user.username if user else '',
            document=document,
            node=node,
            detail=detail or {},
            ip_address=ip or None,
            user_agent=ua,
        )
    except Exception:
        logger.exception("log operation failed")


def _get_user_role(user):
    """获取用户角色信息（严格权限检查）"""
    if not user or not getattr(user, 'is_authenticated', False):
        return None, None, []

    # 检查是否是超级管理员（直接使用属性，不使用 getattr）
    if hasattr(user, 'is_super_admin') and user.is_super_admin:
        return 'super_admin', None, []

    # 检查是否是知识库管理员（文档管理权限等同于超管）
    try:
        user_roles = list(user.user_roles.values_list('role__code', flat=True))
    except Exception:
        user_roles = []

    if getattr(user, 'is_kb_admin', False):
        return 'kb_admin', None, []

    if 'dept_manager' in user_roles:
        return 'dept_manager', getattr(user, 'department_id', None), []
    elif 'team_leader' in user_roles:
        # 获取用户所在的团队
        try:
            team_ids = list(user.user_teams.values_list('team_id', flat=True))
        except Exception:
            team_ids = []
        return 'team_leader', getattr(user, 'department_id', None), team_ids
    else:
        # 普通员工
        try:
            team_ids = list(user.user_teams.values_list('team_id', flat=True))
        except Exception:
            team_ids = []
        return 'employee', getattr(user, 'department_id', None), team_ids


def _validate_visibility_scope(user, visible_scope):
    """
    验证用户是否有权限设置指定的可见范围
    返回 (is_valid, error_message)

    visible_scope 三档: team(仅归属团队) / dept(归属全部门) / public(全公司)
    """
    if visible_scope not in ('team', 'dept', 'public'):
        return False, "无效的可见范围设置"

    # super_admin / kb_admin 可以设置任意可见范围
    if getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False):
        return True, None

    # 所有用户都可以设置 team 和 dept
    if visible_scope in ('team', 'dept'):
        return True, None

    # public 需要审批（通过 access_application 流程），但创建时可以设置
    return True, None


ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".md", ".markdown", ".txt", ".rst",
    ".py", ".java", ".go", ".js", ".ts", ".jsx", ".tsx", ".c", ".cpp", ".h", ".rs",
    ".yaml", ".yml", ".json", ".xml", ".toml", ".ini", ".conf", ".cfg",
    ".sh", ".bat", ".ps1", ".css",
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


def _extract_text_content(content: bytes, file_type: str, filename: str) -> str:
    """从文件内容中提取文本（用于预览）"""
    ext = os.path.splitext(filename)[1].lower()

    # 文本类文件：直接解码
    if file_type in ("txt", "markdown") or ext in (".txt", ".md", ".markdown"):
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return content.decode("gbk")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="ignore")

    # PDF
    if file_type == "pdf" or ext == ".pdf":
        try:
            from PyPDF2 import PdfReader
            import io
            reader = PdfReader(io.BytesIO(content))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
            return text if text.strip() else "PDF 文档无文本内容"
        except ImportError:
            return "需要安装 PyPDF2 才能预览 PDF 内容"
        except Exception as e:
            logger.error(f"PDF extract failed: {e}")
            return f"PDF 解析失败: {str(e)}"

    # DOCX
    if file_type == "docx" or ext in (".doc", ".docx"):
        try:
            from docx import Document
            import io
            doc = Document(io.BytesIO(content))
            text = "\n\n".join(paragraph.text for paragraph in doc.paragraphs)
            return text if text.strip() else "Word 文档无文本内容"
        except ImportError:
            return "需要安装 python-docx 才能预览 Word 内容"
        except Exception as e:
            logger.error(f"DOCX extract failed: {e}")
            return f"Word 解析失败: {str(e)}"

    # 代码/配置文件
    if file_type in ("code", "config"):
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return content.decode("gbk")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="ignore")

    # 未知类型：尝试文本解码或显示二进制提示
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return f"[{filename}] 无法预览此类型文件，建议下载查看"


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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        root_type = request.query_params.get("root_type")
        qs = KnowledgeNode.objects.filter(is_deleted=False)
        if root_type:
            qs = qs.filter(root_type=root_type)
        # 统计每个节点下未删除的文档数（与详情页 document_count 口径一致）
        qs = qs.annotate(
            document_count=Count(
                "documents",
                filter=models.Q(documents__is_deleted=False),
            ),
        )
        nodes = qs.order_by("depth", "order_no", "id").values(
            "id", "parent_id", "root_type", "node_type", "name", "depth",
            "node_level", "document_count", "ref_id", "path",
        )
        data = list(nodes)
        return Response({"tree": _build_tree(data), "total": len(data)})


class RootTypesView(APIView):
    """GET /api/v1/knowledge/nodes/root_types/ - 动态获取所有根类型"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        root_types = KnowledgeNode.objects.filter(
            node_type='root', is_deleted=False
        ).values_list('root_type', flat=True).distinct()
        return Response({
            "root_types": [
                {"code": rt, "name": rt} for rt in root_types
            ]
        })


class AllowedVisibilityView(APIView):
    """GET /api/v1/knowledge/documents/allowed_visibility/ - 获取当前用户可选的部门/团队"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import Department, Team
        from django.core.cache import cache

        role, user_dept_id, user_team_ids = _get_user_role(request.user)
        
        # 构建缓存key：不同角色有不同的缓存
        cache_key = f'allowed_visibility_{role}_{user_dept_id}_{tuple(sorted(user_team_ids))}'
        
        # 尝试从缓存获取
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return Response(cached_result)

        result = {
            'role': role,
            'can_set_public': True,  # 所有角色都可以设置公开
            'departments': [],
            'teams': [],
        }

        result['departments'] = list(Department.objects.filter(is_deleted=False).values('id', 'name'))
        result['teams'] = list(Team.objects.filter(is_deleted=False).values('id', 'name', 'code', 'department_id'))

        # 缓存1小时（3600秒）
        cache.set(cache_key, result, 3600)
        
        return Response(result)


class KnowledgeNodeViewSet(viewsets.ModelViewSet):
    """/api/v1/knowledge/nodes/ — retrieve 全员可见；写操作 admin/ops 或团队组长（仅本团队范围内）"""
    queryset = KnowledgeNode.objects.filter(is_deleted=False).order_by("path")
    serializer_class = KnowledgeNodeSerializer
    filterset_fields = ["root_type", "node_type", "parent"]

    # ── 团队组长权限辅助方法 ────────────

    def _is_team_leader(self, user):
        """用户是否为团队组长"""
        if not user or not user.is_authenticated:
            return False
        try:
            return user.user_roles.filter(role__code='team_leader').exists()
        except Exception:
            return False

    def _get_team_leader_paths(self, user):
        """获取团队组长管理的团队节点 path 列表"""
        try:
            team_ids = list(user.user_teams.values_list('team_id', flat=True))
        except Exception:
            team_ids = []
        if not team_ids:
            return []
        return list(KnowledgeNode.objects.filter(
            node_level=3, ref_id__in=team_ids, is_deleted=False,
        ).values_list('path', flat=True))

    def _check_team_node_write(self, node, user):
        """检查团队组长是否有权操作该节点（节点必须在组长团队子树内）"""
        team_paths = self._get_team_leader_paths(user)
        for tp in team_paths:
            # tp 本身以 / 结尾；子节点 path 以 tp 开头即为团队子树内
            if node.path == tp or node.path.startswith(tp):
                return
        raise PermissionDenied("您只能操作自己团队范围内的分类节点")

    def _is_admin_user(self, user):
        """用户是否为管理员（RBAC：knowledge:manage:all）"""
        try:
            return bool(user.is_kb_admin)
        except Exception:
            return False

    # ── 权限 ────────────

    def get_permissions(self):
        """retrieve 面向所有登录用户开放；写操作允许 admin/ops 或团队组长"""
        if self.action == 'retrieve':
            return [IsAuthenticated()]
        if self.action in ('create', 'destroy', 'update', 'partial_update'):
            # 需要登录，具体权限在方法内校验
            return [IsAuthenticated()]
        return [IsAdminOrOps()]

    # ── Level 保护：禁止直接 CRUD Level 1-3 节点 ────────────
    _LEVEL_LABELS = {0: '根节点', 1: '部门节点', 2: '团队节点'}

    def _check_level_writable(self, depth):
        """depth <= 2 即 Level 1-3，禁止通过节点API直接操作"""
        if depth is not None and depth <= 2:
            label = self._LEVEL_LABELS.get(depth, '系统节点')
            raise ValidationError(
                {'parent': f'{label}不支持直接创建或修改，请通过部门/团队管理功能操作'}
            )

    def _check_node_not_managed(self, node):
        """禁止通过节点API直接增删改 Level 1-3 节点"""
        if node.node_level and node.node_level <= 3:
            label = self._LEVEL_LABELS.get(node.node_level - 1, '系统节点')
            raise ValidationError(
                {'detail': f'{label}不支持直接操作，请通过部门/团队管理功能操作'}
            )

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
        user = self.request.user

        # 团队组长：只能在本团队下创建分类节点
        if not self._is_admin_user(user):
            if not parent:
                raise PermissionDenied("您只能在自己团队下创建分类节点，必须选择上级节点")
            if parent.node_level and parent.node_level < 3:
                raise PermissionDenied("您只能在自己团队下创建分类节点")
            self._check_team_node_write(parent, user)

        # 根节点不应有父节点
        if node_type == "root" and parent:
            raise ValidationError({"parent": "根节点不能指定上级节点"})

        # root_type: 有父节点则继承，无父节点则强制 root 类型并从数据库获取默认值
        if parent:
            root_type = parent.root_type
        else:
            node_type = "root"
            root_type = serializer.validated_data.get("root_type")
            if not root_type:
                # 从数据库获取第一个根类型作为默认值
                default_root = KnowledgeNode.objects.filter(
                    node_type='root', is_deleted=False
                ).first()
                root_type = default_root.root_type if default_root else 'company_doc'
        depth = (parent.depth + 1) if parent else 0
        self._check_level_writable(depth)
        obj = serializer.save(depth=depth, node_type=node_type, root_type=root_type, created_by=user)
        # 更新 path（ID 零填充 4 位，确保按数值顺序排序）
        padded_id = f"{obj.id:04d}"
        if parent:
            obj.path = f"{parent.path}{padded_id}/"
        else:
            obj.path = f"/{padded_id}/"
        obj.save(update_fields=["path"])
        _log_operation(self.request, 'node_create', node=obj,
                       detail={'name': obj.name, 'node_type': obj.node_type, 'root_type': obj.root_type})

    def destroy(self, request, *args, **kwargs):
        node = self.get_object()

        # 团队组长：只能删除本团队范围内的分类节点
        if not self._is_admin_user(request.user):
            if node.node_level and node.node_level <= 3:
                raise PermissionDenied("您只能删除自己团队下的分类节点")
            self._check_team_node_write(node, request.user)

        # Level 1-3 保护：禁止直接删除
        if node.node_level and node.node_level <= 3:
            label = self._LEVEL_LABELS.get(node.node_level - 1, '系统节点')
            return Response(
                {"detail": f"{label}不支持直接删除，请通过部门/团队管理功能操作"},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
                {"detail": f"该节点下存在 {child_count} 个子分类，请先删除所有子分类后再删除此节点"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 递归检查该节点及其所有子孙节点下是否存在文档
        from apps.knowledge.node_sync import count_docs_in_subtree
        doc_count = count_docs_in_subtree(node.id)
        if doc_count > 0:
            return Response(
                {"detail": f"该分类下存在 {doc_count} 个文档，请先迁移或删除所有文档后再删除此节点"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 软删除
        node.is_deleted = True
        node.save(update_fields=["is_deleted"])
        _log_operation(request, 'node_delete', node=node,
                       detail={'name': node.name, 'node_type': node.node_type})
        return Response(status=204)

    def perform_update(self, serializer):
        old_obj = self.get_object()

        # 团队组长：只能编辑本团队范围内的分类节点
        if not self._is_admin_user(self.request.user):
            self._check_team_node_write(old_obj, self.request.user)

        self._check_node_not_managed(old_obj)
        old_data = {
            'name': old_obj.name,
            'description': old_obj.description,
            'order_no': old_obj.order_no,
        }
        new_obj = serializer.save()
        new_data = {
            'name': new_obj.name,
            'description': new_obj.description,
            'order_no': new_obj.order_no,
        }
        _log_operation(self.request, 'node_update', node=new_obj,
                       detail={'old': old_data, 'new': new_data})


class DocumentViewSet(viewsets.ModelViewSet):
    """/api/v1/knowledge/documents/"""
    queryset = Document.objects.order_by("-created_at")
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["node", "status", "file_type", "visible_scope", "root_type", "owner", "is_deleted", "dept_node_id"]
    search_fields = ["title", "file_name", "owner__username", "owner__real_name"]

    def get_queryset(self):
        qs = super().get_queryset().select_related("owner", "node")
        user = self.request.user
        include_deleted = self.request.query_params.get("include_deleted") == "true"
        
        if not include_deleted:
            qs = qs.filter(is_deleted=False)
        
        dept_id = self.request.query_params.get("dept_id")
        if dept_id:
            from apps.knowledge.models import KnowledgeNode
            dept_node = KnowledgeNode.objects.filter(
                ref_id=dept_id, node_level=2
            ).first()
            if dept_node:
                qs = qs.filter(dept_node_id=dept_node.id)
        
        if self.request.query_params.get("discover"):
            # 发现模式：返回全部文档用于浏览与申请权限，
            # 但绝密(secret_level=4)文档的条目名仅 owner 和管理员可见
            if not (getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False)):
                qs = qs.exclude(
                    models.Q(secret_level=4) & ~models.Q(owner=user)
                )
            return qs
        if getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False):
            return qs
        qs = qs.filter(
            models.Q(owner=user) |
            models.Q(visible_scope='public')
        )
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["_user_ctx"] = build_user_context(self.request.user)
        return ctx

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            ctx = self.get_serializer_context()
            ctx["_grants_map"] = build_grants_map(request.user, [d.id for d in page])
            serializer = self.get_serializer(page, many=True, context=ctx)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def available_depts(self, request):
        """获取部门列表（用于筛选），使用Redis缓存"""
        from django.core.cache import cache
        from apps.users.models import Department
        
        cache_key = "available_depts_list"
        cached_depts = cache.get(cache_key)
        
        if cached_depts is not None:
            return Response(cached_depts)
        
        depts = Department.objects.filter(is_deleted=False).values('id', 'name')
        dept_list = list(depts)
        
        cache.set(cache_key, dept_list, 3600)
        
        return Response(dept_list)

    def get_object(self):
        obj = super().get_object()
        # 读取级校验：至少需要 can_read 才能获取单条详情
        if not self._access(obj)["can_read"]:
            raise PermissionDenied("无权限查看此文档")
        return obj

    def _access(self, doc, user=None):
        return resolve_doc_access(user or self.request.user, doc,
                                  ctx=build_user_context(user or self.request.user))

    def _require_write(self, doc):
        """写操作（编辑/删除/分享/管理授权）仅限所有者或管理员"""
        a = self._access(doc)
        if not (a["is_owner"] or a["is_manager"]):
            raise PermissionDenied("仅文档所有者或管理员可执行此操作")

    def perform_update(self, serializer):
        old_obj = self.get_object()
        access = self._access(old_obj)

        # 检查是否在修改可见范围
        new_visible_scope = serializer.validated_data.get('visible_scope', old_obj.visible_scope)
        is_changing_visibility = new_visible_scope != old_obj.visible_scope

        if is_changing_visibility:
            user = self.request.user

            # 验证可见范围是否合法
            is_valid, error_msg = _validate_visibility_scope(user, new_visible_scope)
            if not is_valid:
                raise PermissionDenied(error_msg)

            # 可见范围扩大（team→dept / team→public / dept→public）需要双层审批
            scope_order = {'team': 0, 'dept': 1, 'public': 2}
            if scope_order.get(new_visible_scope, 0) > scope_order.get(old_obj.visible_scope, 0):
                # 创建 AccessApplication 审批记录，标记需要双层审批
                AccessApplication.objects.create(
                    applicant=user,
                    target_type='doc',
                    target_id=old_obj.id,
                    action='visibility_change',
                    new_visibility=new_visible_scope,
                    reason=f"申请将文档可见范围从「{old_obj.get_visible_scope_display()}」"
                           f"扩大为「{dict(Document.VISIBLE_SCOPE_CHOICES).get(new_visible_scope, new_visible_scope)}」",
                    status='pending',
                    need_double_approval=True,
                )
                raise PermissionDenied(
                    "扩大可见范围需要双层审批，已自动提交审批申请，需两位管理员先后审批"
                )
        else:
            # 其他字段修改：需要写权限
            self._require_write(old_obj)

        old_data = {
            'visible_scope': old_obj.visible_scope,
            'allow_download': old_obj.allow_download,
            'allow_share': old_obj.allow_share,
            'title': old_obj.title,
            'node_id': old_obj.node_id,
        }
        new_obj = serializer.save()
        new_data = {
            'visible_scope': new_obj.visible_scope,
            'allow_download': new_obj.allow_download,
            'allow_share': new_obj.allow_share,
            'title': new_obj.title,
            'node_id': new_obj.node_id,
        }
        # 检测可见性变更
        if is_changing_visibility:
            _log_operation(self.request, 'doc_visibility_change', document=new_obj,
                           detail={'old': old_data, 'new': new_data})

    def destroy(self, request, *args, **kwargs):
        doc = self.get_object()
        self._require_write(doc)
        doc.is_deleted = True
        doc.delete_time = timezone.now()
        doc.save(update_fields=["is_deleted", "delete_time"])
        _log_operation(request, 'doc_delete', document=doc,
                       detail={'title': doc.title, 'file_name': doc.file_name})
        try:
            from apps.retrieval.vector_store import delete_by_document
            delete_by_document(doc.id)
        except Exception:
            logger.exception("delete vector failed")
        return Response(status=204)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        """恢复已删除的文档"""
        doc = self.get_object()
        self._require_write(doc)
        if not doc.is_deleted:
            return Response({"detail": "文档未被删除"}, status=400)
        
        doc.is_deleted = False
        doc.restored_at = timezone.now()
        doc.restored_by = request.user
        doc.save(update_fields=["is_deleted", "restored_at", "restored_by"])
        _log_operation(request, 'doc_restore', document=doc,
                       detail={'title': doc.title, 'file_name': doc.file_name})
        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def hard_delete(self, request, pk=None):
        """
        物理删除已删除的文档（删除物理文件）
        
        限制条件：
        - 文档必须已被逻辑删除（is_deleted=True）
        - 删除时间超过30天（DEBUG模式下不受限制）
        - 超过180天的已删除文档会被自动清理任务删除
        
        物理删除后无法恢复。
        """
        from django.conf import settings
        from apps.knowledge.storage import get_document_storage
        
        doc = self.get_object()
        self._require_write(doc)
        
        if not doc.is_deleted:
            return Response({"detail": "文档未被逻辑删除，请先执行逻辑删除"}, status=400)
        
        if not doc.file_path:
            return Response({"detail": "文档没有物理文件可删除"}, status=400)
        
        min_retention_days = 30
        if not settings.DEBUG and doc.delete_time:
            days_since_delete = (timezone.now() - doc.delete_time).days
            if days_since_delete < min_retention_days:
                remaining_days = min_retention_days - days_since_delete
                return Response({
                    "detail": f"文档删除不足 {min_retention_days} 天，还需等待 {remaining_days} 天才能物理删除",
                    "remaining_days": remaining_days,
                    "days_since_delete": days_since_delete
                }, status=403)
        
        storage = get_document_storage()
        try:
            storage.delete(doc.file_path)
            doc.file_path = ''
            doc.save(update_fields=['file_path'])
            _log_operation(request, 'doc_hard_delete', document=doc,
                           detail={'title': doc.title, 'file_name': doc.file_name})
            return Response({"ok": True})
        except Exception as e:
            logger.exception("Failed to hard delete file for doc=%d", doc.id)
            return Response({"detail": f"物理删除失败: {str(e)[:200]}"}, status=500)

    @action(detail=True, methods=["post"])
    def reparse(self, request, pk=None):
        """重新解析文档：删除旧向量/切片/代码块/图片资源，基于原文件重新解析"""
        doc = self.get_object()
        self._require_write(doc)
        doc.status = "pending"
        doc.error_message = ""
        doc.save(update_fields=["status", "error_message"])
        _log_operation(request, 'doc_reparse', document=doc,
                       detail={'title': doc.title})
        try:
            from apps.knowledge.tasks import parse_document
            parse_document.delay(doc.id)
        except Exception:
            logger.exception("dispatch parse task failed")
        return Response({"ok": True, "status": "pending"})

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        doc = self.get_object()
        if not self._access(doc)["can_download"]:
            raise PermissionDenied("无权限下载此文档")
        if not doc.file_path:
            raise Http404("文件不存在")
        _log_operation(request, 'doc_download', document=doc,
                       detail={'file_name': doc.file_name, 'file_size': doc.file_size})
        # OSS：返回签名 URL 跳转；本地：直接返回文件流
        if doc.file_path.startswith("oss://"):
            storage = get_document_storage()
            url = storage.get_url(doc.file_path)
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(url)
        if not os.path.exists(doc.file_path):
            raise Http404("文件不存在")
        fp = open(doc.file_path, "rb")
        return FileResponse(fp, as_attachment=True, filename=doc.file_name)

    # ------------------------------------------------------------------
    # 原始内容预览：返回文档原始文本内容（支持分页，用于前端预览，不可复制）
    # ------------------------------------------------------------------
    @action(detail=True, methods=["get"])
    def raw_content(self, request, pk=None):
        doc = self.get_object()
        if not self._access(doc)["can_read"]:
            raise PermissionDenied("无权限预览此文档")
        if not doc.file_path:
            raise Http404("文件不存在")

        # 分页参数（每页字符数）
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(max(1000, int(request.query_params.get("page_size", 5000))), 20000)  # 1k-20k 字符

        # 获取完整文本内容（最大 50MB，超出则截断）
        MAX_PREVIEW_SIZE = 50 * 1024 * 1024
        text_content = self._get_document_text(doc, MAX_PREVIEW_SIZE)
        
        if text_content is None:
            return Response({"error": "无法获取文件内容"}, status=500)
        
        total_chars = len(text_content)
        total_pages = max(1, (total_chars + page_size - 1) // page_size)
        page = min(page, total_pages)
        
        # 计算当前页内容（按字符数分页，尽量在段落边界处断开）
        start = (page - 1) * page_size
        end = start + page_size
        
        # 尽量在换行符处断开
        if end < total_chars:
            # 向后找最近的换行符
            newline_pos = text_content.find('\n', end - 100, end + 200)
            if newline_pos != -1:
                end = newline_pos + 1  # 包含换行符
        
        current_content = text_content[start:end]
        
        # 添加上下文提示
        if start > 0:
            current_content = '...' + current_content
        if end < total_chars:
            current_content = current_content + '...'

        return Response({
            "content": current_content,
            "file_type": doc.file_type,
            "file_name": doc.file_name,
            "size": doc.file_size or total_chars,
            "total_chars": total_chars,
            "total_pages": total_pages,
            "current_page": page,
            "page_size": page_size,
            "can_copy": False,
        })

    def _get_document_text(self, doc, max_size):
        """提取文档文本内容（内部方法）"""
        content = None
        if doc.file_path.startswith("oss://"):
            storage = get_document_storage()
            url = storage.get_url(doc.file_path)
            try:
                import requests
                resp = requests.get(url, timeout=30, stream=True)
                content = b""
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    content += chunk
                    if len(content) > max_size:
                        content = content[:max_size]
                        break
            except Exception as e:
                logger.error(f"OSS raw content fetch failed: {e}")
                return None
        else:
            if not os.path.exists(doc.file_path):
                raise Http404("文件不存在")
            with open(doc.file_path, "rb") as f:
                content = f.read(max_size)
        
        return _extract_text_content(content, doc.file_type, doc.file_name)

    # ------------------------------------------------------------------
    # 申请权限
    # ------------------------------------------------------------------
    @action(detail=True, methods=["post"])
    def request_access(self, request, pk=None):
        """POST /documents/{id}/request_access/  {action, reason?}
        注意：申请者通常尚无读取权限，故不走 get_object 的 can_read 校验。"""
        doc = Document.objects.filter(id=pk, is_deleted=False).first()
        if not doc:
            raise Http404("文档不存在")
        action = request.data.get("action", "read")
        if action not in ("read", "download"):
            raise ValidationError({"action": "无效的申请类型"})

        # 已有相同 pending 申请则不重复创建
        exists = AccessApplication.objects.filter(
            applicant=request.user, target_type='doc', target_id=doc.id,
            action=action, status="pending"
        ).exists()
        if exists:
            return Response({"ok": False, "detail": "已存在待审批的相同申请"}, status=200)

        app = AccessApplication.objects.create(
            applicant=request.user,
            target_type='doc',
            target_id=doc.id,
            action=action,
            reason=(request.data.get("reason") or "")[:1000],
            status="pending",
        )
        logger.info("[AccessRequest] doc=%s applicant=%s action=%s",
                    doc.id, request.user.username, action)
        return Response({
            "id": app.id,
            "doc_id": doc.id,
            "action": app.action,
            "reason": app.reason,
            "status": app.status,
            "created_at": app.created_at,
        }, status=201)

    # ------------------------------------------------------------------
    # 访问授权管理（所有者/管理员查看与撤销）
    # ------------------------------------------------------------------
    @action(detail=True, methods=["get"])
    def access_grants(self, request, pk=None):
        """GET /documents/{id}/access_grants/  查看该文档的所有授权"""
        doc = self.get_object()
        self._require_write(doc)

        result = {
            'allow_users': [],      # 个人白名单
            'cross_teams': [],      # 跨团队授权
            'deny_users': [],       # 黑名单
            'visible_scope': doc.visible_scope,
        }

        # 1. DocAllowUser（个人白名单）
        for au in DocAllowUser.objects.filter(doc_id=doc.id):
            result['allow_users'].append({
                'id': au.id,
                'uid': au.uid,
                'expire_time': au.expire_time,
                'create_time': au.create_time,
            })

        # 2. DocCrossTeam（跨团队授权）
        for ct in DocCrossTeam.objects.filter(doc_id=doc.id):
            result['cross_teams'].append({
                'id': ct.id,
                'team_code': ct.team_code,
                'expire_time': ct.expire_time,
                'create_time': ct.create_time,
            })

        # 3. DocDenyUser（黑名单）
        for du in DocDenyUser.objects.filter(doc_id=doc.id):
            result['deny_users'].append({
                'id': du.id,
                'uid': du.uid,
                'create_time': du.create_time,
            })

        return Response(result)

    @action(detail=True, methods=["post"], url_path="grant_access")
    def grant_access(self, request, pk=None):
        """POST /documents/{id}/grant_access/  {grant_type, team_code/uid}  创建跨团队/个人授权
        grant_type: cross_team / allow_user
        缩小可见范围时，为指定团队创建跨团队授权"""
        doc = self.get_object()
        self._require_write(doc)
        grant_type = request.data.get("grant_type")
        if grant_type not in ("cross_team", "allow_user"):
            raise ValidationError({"grant_type": "无效的授权类型，可选: cross_team/allow_user"})
        try:
            if grant_type == "cross_team":
                team_code = request.data.get("team_code", "").strip()
                if not team_code:
                    raise ValidationError({"team_code": "team_code 不能为空"})
                grant, created = DocCrossTeam.objects.get_or_create(
                    doc_id=doc.id,
                    team_code=team_code,
                    defaults={"create_by": request.user.id},
                )
                if created:
                    doc.has_cross_team = True
                    doc.save(update_fields=["has_cross_team"])
                _log_operation(request, 'doc_share', document=doc,
                               detail={'grant_type': 'cross_team', 'team_code': team_code,
                                       'created': created})
                return Response({
                    "id": grant.id,
                    "grant_type": "cross_team",
                    "team_code": team_code,
                    "created": created,
                })
            else:
                uid = request.data.get("uid")
                if not uid:
                    raise ValidationError({"uid": "uid 不能为空"})
                grant, created = DocAllowUser.objects.get_or_create(
                    doc_id=doc.id,
                    uid=uid,
                    defaults={"create_by": request.user.id},
                )
                _log_operation(request, 'doc_share', document=doc,
                               detail={'grant_type': 'allow_user', 'uid': uid,
                                       'created': created})
                return Response({
                    "id": grant.id,
                    "grant_type": "allow_user",
                    "uid": uid,
                    "created": created,
                })
        except Exception as e:
            raise ValidationError({"detail": str(e)})

    @action(detail=True, methods=["post"], url_path="revoke_grant")
    def revoke_grant(self, request, pk=None):
        """POST /documents/{id}/revoke_grant/  {grant_type, grant_id}
        grant_type: allow_user / cross_team / deny_user"""
        doc = self.get_object()
        self._require_write(doc)
        grant_type = request.data.get("grant_type")
        grant_id = request.data.get("grant_id")
        if grant_type not in ("allow_user", "cross_team", "deny_user"):
            raise ValidationError({"grant_type": "无效的授权类型，可选: allow_user/cross_team/deny_user"})
        try:
            if grant_type == "allow_user":
                grant = DocAllowUser.objects.get(id=grant_id, doc_id=doc.id)
            elif grant_type == "cross_team":
                grant = DocCrossTeam.objects.get(id=grant_id, doc_id=doc.id)
            else:
                grant = DocDenyUser.objects.get(id=grant_id, doc_id=doc.id)
        except (DocAllowUser.DoesNotExist, DocCrossTeam.DoesNotExist, DocDenyUser.DoesNotExist):
            raise Http404("授权记录不存在")
        revoked_detail = {
            'grant_type': grant_type,
            'grant_id': grant.id,
        }
        if grant_type == "allow_user":
            revoked_detail['uid'] = grant.uid
        elif grant_type == "cross_team":
            revoked_detail['team_code'] = grant.team_code
        else:
            revoked_detail['uid'] = grant.uid
        grant.delete()
        _log_operation(request, 'doc_revoke', document=doc,
                       detail=revoked_detail)
        return Response({"ok": True, "grant_type": grant_type, "grant_id": grant_id})

    # ------------------------------------------------------------------
    # 访问申请单：我的申请 / 待我审批 / 审批
    # ------------------------------------------------------------------
    @action(detail=False, methods=["get"], url_path="my_access_requests")
    def my_access_requests(self, request):
        """GET /documents/my_access_requests/  我发起的访问申请"""
        qs = AccessApplication.objects.filter(applicant=request.user).order_by("-created_at")[:100]
        data = []
        for app in qs:
            data.append({
                "id": app.id,
                "target_type": app.target_type,
                "target_id": app.target_id,
                "action": app.action,
                "reason": app.reason,
                "status": app.status,
                "reviewer_comment": app.reviewer_comment,
                "created_at": app.created_at,
                "updated_at": app.updated_at,
            })
        return Response(data)

    @action(detail=False, methods=["get"], url_path="pending_access_requests")
    def pending_access_requests(self, request):
        """GET /documents/pending_access_requests/  待我（所有者/管理员）审批的申请"""
        user = request.user
        is_manager = (getattr(user, 'is_super_admin', False)
                       or getattr(user, 'is_kb_admin', False))
        qs = AccessApplication.objects.filter(status="pending").select_related("applicant")
        # 管理员看全部；非管理员仅看自己文档的申请
        if not is_manager:
            owned_doc_ids = list(
                Document.objects.filter(owner=user, is_deleted=False).values_list('id', flat=True)
            )
            qs = qs.filter(target_type='doc', target_id__in=owned_doc_ids)
        qs = qs.order_by("-created_at")[:200]
        data = []
        for app in qs:
            item = {
                "id": app.id,
                "applicant_id": app.applicant_id,
                "applicant_name": app.applicant.username,
                "target_type": app.target_type,
                "target_id": app.target_id,
                "action": app.action,
                "reason": app.reason,
                "status": app.status,
                "reviewer_comment": app.reviewer_comment,
                "created_at": app.created_at,
                "updated_at": app.updated_at,
            }
            data.append(item)
        return Response(data)

    @action(detail=False, methods=["post"], url_path="approve_access_request")
    def approve_access_request(self, request):
        """POST /documents/approve_access_request/  {request_id, comment?}  批准并创建授权
        
        双层审批逻辑：need_double_approval=True 的申请需两位不同管理员先后审批
        """
        req_id = request.data.get("request_id")
        try:
            app = AccessApplication.objects.get(id=req_id, status="pending")
        except AccessApplication.DoesNotExist:
            raise Http404("申请不存在或已处理")
        # 仅所有者/管理员可审批
        if app.target_type == 'doc' and app.target_id:
            doc = Document.objects.filter(id=app.target_id, is_deleted=False).first()
            a = resolve_doc_access(request.user, doc) if doc else None
            if not a or not (a["is_owner"] or a["is_manager"]):
                raise PermissionDenied("无权审批此申请")
        else:
            if not (getattr(request.user, 'is_super_admin', False)
                    or getattr(request.user, 'is_kb_admin', False)):
                raise PermissionDenied("只有管理员可以审批此类申请")

        reviewer = request.user
        comment = (request.data.get("comment") or "")[:1000]

        # 双层审批：需要两位不同管理员先后审批
        if app.need_double_approval and app.first_reviewed_by_id is None:
            # 一审：记录审批人，保持 pending
            app.first_reviewed_by = reviewer
            app.first_reviewed_at = timezone.now()
            app.reviewer_comment = comment
            app.save(update_fields=["first_reviewed_by", "first_reviewed_at", "reviewer_comment"])
            logger.info("[AccessRequest] first-approved id=%s by=%s (pending second review)",
                        app.id, reviewer.username)
            return Response({
                "id": app.id,
                "status": "pending",
                "message": "一审已通过，等待第二位管理员审批",
            })

        if app.need_double_approval and app.first_reviewed_by_id is not None:
            # 二审：不能与一审为同一人
            if app.first_reviewed_by_id == reviewer.id:
                raise PermissionDenied("双层审批不能由同一管理员完成，请另一位管理员审批")
            # 二审通过，最终生效
            app.status = "approved"
            app.reviewed_by = reviewer
            app.reviewed_at = timezone.now()
            # 追加二审意见
            if comment:
                app.reviewer_comment = (app.reviewer_comment + "\n[二审] " + comment)[:2000]
            app.save(update_fields=["status", "reviewed_by", "reviewed_at", "reviewer_comment"])
        else:
            # 普通单层审批
            app.status = "approved"
            app.reviewer_comment = comment
            app.reviewed_by = reviewer
            app.reviewed_at = timezone.now()
            app.save(update_fields=["status", "reviewer_comment", "reviewed_by", "reviewed_at"])

        # 创建授权：根据 action 类型处理
        if app.action == 'visibility_change':
            if doc:
                doc.visible_scope = app.new_visibility
                doc.save(update_fields=['visible_scope', 'updated_at'])
                _log_operation(request, 'doc_visibility_change', document=doc,
                               detail={'application_id': app.id, 'applicant': app.applicant.username,
                                       'new_visible_scope': app.new_visibility})
        elif app.target_type == 'doc' and app.target_id:
            DocAllowUser.objects.get_or_create(
                doc_id=app.target_id,
                uid=app.applicant_id,
                defaults={'create_by': request.user.id},
            )
            _log_operation(request, 'doc_grant', document=doc,
                           detail={'application_id': app.id, 'applicant': app.applicant.username,
                                   'action': app.action, 'type': 'allow_user'})
        logger.info("[AccessRequest] approved id=%s applicant=%s target=%s:%s",
                    app.id, app.applicant.username, app.target_type, app.target_id)
        return Response({
            "id": app.id,
            "status": app.status,
            "applicant_id": app.applicant_id,
            "target_type": app.target_type,
            "target_id": app.target_id,
            "action": app.action,
        })

    @action(detail=False, methods=["post"], url_path="reject_access_request")
    def reject_access_request(self, request):
        """POST /documents/reject_access_request/  {request_id, comment?}  驳回"""
        req_id = request.data.get("request_id")
        try:
            app = AccessApplication.objects.get(id=req_id, status="pending")
        except AccessApplication.DoesNotExist:
            raise Http404("申请不存在或已处理")
        if app.target_type == 'doc' and app.target_id:
            doc = Document.objects.filter(id=app.target_id, is_deleted=False).first()
            a = resolve_doc_access(request.user, doc) if doc else None
            if not a or not (a["is_owner"] or a["is_manager"]):
                raise PermissionDenied("无权审批此申请")
        else:
            if not (getattr(request.user, 'is_super_admin', False)
                    or getattr(request.user, 'is_kb_admin', False)):
                raise PermissionDenied("只有管理员可以审批此类申请")
        app.status = "rejected"
        app.reviewer_comment = (request.data.get("comment") or "")[:1000]
        app.reviewed_by = request.user
        app.reviewed_at = timezone.now()
        app.save(update_fields=["status", "reviewer_comment", "reviewed_by", "reviewed_at"])
        _log_operation(request, 'doc_grant_reject',
                       detail={'application_id': app.id, 'applicant': app.applicant.username,
                               'action': app.action, 'target_type': app.target_type, 'target_id': app.target_id})
        return Response({
            "id": app.id,
            "status": app.status,
        })


class DocumentUploadView(APIView):
    """
    POST /api/v1/knowledge/documents/upload/
    multipart/form-data: file, node_id, [title], [visible_scope], [force_upload]
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

        if not self._check_node_upload_permission(request.user, node):
            return Response({"detail": "无权限向该节点上传文档"}, status=403)

        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return Response({"detail": f"不支持的文件类型: {ext}"}, status=400)

        # 验证文件真实类型（防止文件伪装）
        try:
            # 读取文件开头部分进行类型检测
            file_content = f.read(2048)
            f.seek(0)  # 重置文件指针
            detected_mime = magic.from_buffer(file_content, mime=True)
            # 根据扩展名验证MIME类型
            ext_mime_map = {
                '.pdf': 'application/pdf',
                '.doc': 'application/msword',
                '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                '.md': ['text/markdown', 'text/plain'],
                '.markdown': ['text/markdown', 'text/plain'],
                '.txt': 'text/plain',
                '.rst': 'text/x-rst',
                '.py': 'text/x-python',
                '.java': 'text/x-java-source',
                '.go': 'text/x-go',
                '.js': 'application/javascript',
                '.ts': 'text/typescript',
                '.jsx': 'application/javascript',
                '.tsx': 'text/typescript',
                '.c': 'text/x-c',
                '.cpp': 'text/x-c++',
                '.h': 'text/x-c',
                '.rs': 'text/x-rust',
                '.yaml': ['text/yaml', 'text/x-yaml'],
                '.yml': ['text/yaml', 'text/x-yaml'],
                '.json': 'application/json',
                '.xml': 'application/xml',
                '.toml': 'text/toml',
                '.ini': 'text/x-ini',
                '.conf': 'text/plain',
                '.cfg': 'text/plain',
                '.sh': 'text/x-shellscript',
                '.bat': 'application/x-bat',
                '.ps1': 'text/x-powershell',
                '.css': 'text/css',
            }
            expected_mime = ext_mime_map.get(ext)
            if expected_mime:
                if isinstance(expected_mime, list):
                    if detected_mime not in expected_mime:
                        return Response({"detail": f"文件类型不匹配：扩展名显示为 {ext}，但实际文件类型为 {detected_mime}"}, status=400)
                else:
                    if detected_mime != expected_mime:
                        return Response({"detail": f"文件类型不匹配：扩展名显示为 {ext}，但实际文件类型为 {detected_mime}"}, status=400)
        except Exception as e:
            logger.error(f"文件类型检测失败: {e}")
            return Response({"detail": "文件类型检测失败，请上传合法文件"}, status=400)

        if f.size > MAX_FILE_SIZE:
            return Response({"detail": f"文件大小超过限制（最大 {MAX_FILE_SIZE//(1024*1024)} MB）"}, status=400)

        visible_scope = request.data.get("visible_scope") or 'team'
        if visible_scope not in ('team', 'dept', 'public'):
            return Response({"detail": "visible_scope 必须为 team/dept/public"}, status=400)

        # 上传时选择是否允许下载/分享（默认只读：仅预览/对话检索）
        allow_download = request.data.get("allow_download") in ("true", "True", "1", True)
        allow_share = request.data.get("allow_share") in ("true", "True", "1", True)

        # 验证用户是否有权限设置指定的可见范围
        is_valid, error_msg = _validate_visibility_scope(
            request.user, visible_scope
        )
        if not is_valid:
            return Response({"detail": error_msg}, status=403)

        visibility_depts = request.data.getlist("visibility_depts", [])
        visibility_teams = request.data.getlist("visibility_teams", [])

        h = hashlib.sha256()
        total = 0
        for c in f.chunks():
            h.update(c)
            total += len(c)
        file_hash = h.hexdigest()

        version_tag = request.data.get("version_tag", "").strip()

        if not version_tag:
            max_version = Document.objects.filter(
                node=node, file_name=f.name[:256], is_deleted=False
            ).aggregate(models.Max('version'))['version__max'] or 0
            version_tag = f'v{max_version + 1}'
            version = max_version + 1
        else:
            existing_with_tag = Document.objects.filter(
                node=node, file_name=f.name[:256], version_tag=version_tag, is_deleted=False
            ).first()
            if existing_with_tag:
                version = existing_with_tag.version
            else:
                max_version = Document.objects.filter(
                    node=node, file_name=f.name[:256], is_deleted=False
                ).aggregate(models.Max('version'))['version__max'] or 0
                version = max_version + 1

        exist = Document.objects.filter(
            node=node, file_name=f.name[:256], version_tag=version_tag, is_deleted=False
        ).first()

        title = request.data.get("title") or f.name
        file_type = _detect_file_type(f.name)
        
        file_path = None
        doc = None

        try:
            with transaction.atomic():
                file_path = self._save_file(f, node)
                if not file_path:
                    raise Exception("文件存储失败")

                if exist:
                    exist.is_deleted = True
                    exist.delete_time = timezone.now()
                    exist.save(update_fields=["is_deleted", "delete_time", "updated_at"])
                
                kb_node_id = node_id
                dept_node_id = None
                team_node_id = None
                category_node_id = node_id

                if node.node_level >= 2:
                    ancestors = []
                    current = node
                    while current:
                        ancestors.append(current)
                        current = current.parent
                    ancestors.reverse()
                    for n in ancestors:
                        if n.node_level == 1:
                            kb_node_id = n.id
                        elif n.node_level == 2:
                            dept_node_id = n.id
                        elif n.node_level == 3:
                            team_node_id = n.id
                        elif n.node_level >= 4:
                            category_node_id = n.id

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
                    kb_node_id=kb_node_id,
                    dept_node_id=dept_node_id,
                    team_node_id=team_node_id,
                    category_node_id=category_node_id,
                    version_tag=version_tag,
                    visible_scope=visible_scope,
                    allow_download=allow_download,
                    allow_share=allow_share,
                    root_type=node.root_type,
                    status="pending",
                    version=version,
                )

                if visibility_teams:
                    from apps.users.models import Team
                    team_codes = list(Team.objects.filter(
                        id__in=visibility_teams, is_deleted=False
                    ).values_list('code', flat=True))
                    for team_code in team_codes:
                        DocCrossTeam.objects.get_or_create(
                            doc_id=doc.id,
                            team_code=team_code,
                            defaults={"create_by": request.user.id},
                        )
                    doc.has_cross_team = True
                    doc.save(update_fields=["has_cross_team"])

                _log_operation(request, 'doc_upload', document=doc, node=node,
                               detail={'file_name': f.name, 'file_size': total, 'file_hash': file_hash,
                                       'visible_scope': visible_scope, 'version_tag': version_tag,
                                       'visibility_teams': visibility_teams, 'visibility_depts': visibility_depts})
        except Exception as e:
            if file_path:
                try:
                    storage = get_document_storage()
                    storage.delete(file_path)
                except Exception:
                    logger.exception("Failed to clean up orphan file: %s", file_path)
            return Response({"detail": str(e)[:200]}, status=500)

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

    def _check_node_upload_permission(self, user, node):
        if getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False):
            return True

        role, dept_id, team_ids = _get_user_role(user)

        if role == 'dept_manager' and dept_id:
            if node.node_level == 2 and node.ref_id == dept_id:
                return True
            if node.parent and node.parent.node_level == 2 and node.parent.ref_id == dept_id:
                return True

        if role in ('team_leader', 'employee') and team_ids:
            if node.node_level == 3 and node.ref_id in team_ids:
                return True
            if node.parent and node.parent.node_level == 3 and node.parent.ref_id in team_ids:
                return True

        return False

    def _save_file(self, f, node):
        from apps.knowledge.storage import get_document_storage, generate_node_storage_path
        storage = get_document_storage()
        # 使用 Django 的 get_valid_filename 处理文件名，移除危险字符
        safe_name = django_text.get_valid_filename(f.name)
        # 进一步清理：移除控制字符和其他危险字符
        safe_name = re.sub(r'[\x00-\x1f\x7f]', '', safe_name)
        safe_name = safe_name.replace("..", "_")
        if not safe_name:
            safe_name = "unnamed_file"
        fname = f"{uuid_lib.uuid4().hex}_{safe_name}"
        # 生成节点存储路径
        node_path = generate_node_storage_path(node)
        logger.info('[Upload] saving file to node_path=%s, filename=%s, node_id=%d, node_name=%s',
                    node_path, fname, node.id, node.name)
        file_path = storage.save(fname, f, node_path)
        logger.info('[Upload] file saved to: %s', file_path)
        return file_path


class DocumentChunksView(APIView):
    """GET /api/v1/knowledge/documents/{id}/chunks/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_id):
        # 调试日志：确认用户认证状态
        logger.info('[Chunks] request user: %s, is_authenticated: %s, is_super_admin: %s',
                    request.user, request.user.is_authenticated, 
                    getattr(request.user, 'is_super_admin', False))
        
        try:
            doc = Document.objects.get(id=doc_id, is_deleted=False)
        except Document.DoesNotExist:
            return Response({"detail": "文档不存在"}, status=404)
        
        if not resolve_doc_access(request.user, doc)["can_read"]:
            logger.warning('[Chunks] user %s has no permission for doc %d', request.user, doc_id)
            return Response({"detail": "无权限查看此文档"}, status=403)

        chunks = DocumentChunk.objects.filter(document_id=doc_id).order_by("chunk_index")[:500]
        chunk_list = list(chunks)
        return Response({
            "document_id": int(doc_id),
            "total": len(chunk_list),
            "chunks": DocumentChunkSerializer(chunk_list, many=True).data,
        })

    def _has_permission(self, user, doc):
        # 保留旧方法签名以兼容外部调用，统一改用 resolve_doc_access
        return resolve_doc_access(user, doc)["can_read"]


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
        # 返回所有进行中的文档（不仅是 pending，还包括 parsing, embedding 等）
        processing_statuses = ["pending", "parsing", "desensitizing", "chunking", "embedding", "embedding_failed"]
        pending_query = Document.objects.filter(
            is_deleted=False,
            status__in=processing_statuses,
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
        # 支持重新触发 pending 和 embedding_failed 的文档
        pending_query = Document.objects.filter(
            is_deleted=False,
            status__in=["pending", "embedding_failed"],
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
