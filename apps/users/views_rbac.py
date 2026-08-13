"""RBAC 视图：权限点 / 角色 CRUD（仅超级管理员可操作）

角色权限分配（assign_permissions）参数校验走 AssignPermissionsSerializer，
落库逻辑下沉 services/rbac_service.assign_permissions_to_role。
"""
from loguru import logger

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.users.models import Permission, Role, RolePermissionRel, UserRoleRel
from apps.users.serializers import RoleSerializer, PermissionSerializer, AssignPermissionsSerializer
from apps.users.services.rbac_service import assign_permissions_to_role
from apps.users.utils import _first_serializer_error


class PermissionViewSet(viewsets.ModelViewSet):
    """权限点 CRUD（仅超级管理员可操作）"""
    # Permission 使用 permission_key 三段式（module.resource.action）标识权限点
    queryset = Permission.objects.all().order_by("module", "permission_key")
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticated]

    def _check_super_admin(self):
        if not self.request.user.is_super_admin:
            raise PermissionDenied("仅超级管理员可操作")

    def list(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        self._check_super_admin()
        # 只记录关键业务字段，避免日志泄露敏感信息
        logger.info(f"Permission.create - user: {request.user.username}, key: {request.data.get('code')}")
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._check_super_admin()
        perm = self.get_object()
        # 内置权限点（is_builtin=True）核心字段不可修改
        if perm.is_builtin:
            forbidden = [k for k in request.data if k not in ('description', 'name')]
            if forbidden:
                logger.warning(f"Permission.update - user {request.user.username} tried to modify builtin fields of {perm.permission_key}")
                return Response({"detail": "内置系统权限不允许修改核心字段"}, status=403)
        logger.info(f"Permission.update - user: {request.user.username}, perm: {perm.permission_key}, fields: {list(request.data.keys())}")
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._check_super_admin()
        perm = self.get_object()
        if perm.is_builtin:
            logger.warning(f"Permission.destroy - user {request.user.username} tried to delete builtin perm {perm.permission_key}")
            return Response({"detail": "内置系统权限不允许删除"}, status=403)
        ref_count = RolePermissionRel.objects.filter(permission=perm, is_active=True).count()
        if ref_count > 0:
            return Response({"detail": f"该权限点被 {ref_count} 个角色引用，请先解除角色关联"}, status=400)
        logger.info(f"Permission.destroy - user: {request.user.username}, perm: {perm.permission_key}")
        perm.delete()
        return Response(status=204)


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all().order_by("id")
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated]

    def _check_super_admin(self):
        if not self.request.user.is_super_admin:
            raise PermissionDenied("仅超级管理员可操作")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action in ('list', 'retrieve'):
            qs = qs.prefetch_related('role_permissions')
        return qs

    def list(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        self._check_super_admin()
        # 只记录关键业务字段，避免日志泄露敏感信息
        logger.info(f"Role.create - user: {request.user.username}, code: {request.data.get('code')}")
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._check_super_admin()
        partial = kwargs.get('partial', False)
        role = self.get_object()
        # 内置角色的 role_key 和 is_builtin 不可修改（API 字段名仍为 code，内部映射 role_key）
        if role.is_builtin:
            if 'code' in request.data and request.data['code'] != role.role_key:
                logger.warning(f"Role.update - user {request.user.username} tried to change code of builtin role {role.role_key}")
                return Response({"detail": "内置角色编码不可修改"}, status=400)
            if 'is_builtin' in request.data and request.data['is_builtin'] != role.is_builtin:
                logger.warning(f"Role.update - user {request.user.username} tried to change is_builtin of role {role.role_key}")
                return Response({"detail": "内置角色标记不可修改"}, status=400)
        if 'is_builtin' in request.data:
            logger.warning(f"Role.update - user {request.user.username} tried to set is_builtin on role {role.role_key}")
            return Response({"detail": "is_builtin 字段不可通过API修改"}, status=400)
        serializer = self.get_serializer(role, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        logger.info(f"Role.update - user: {request.user.username}, role: {role.role_key}, fields: {list(request.data.keys())}")
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        self._check_super_admin()
        role = self.get_object()
        if role.is_builtin:
            logger.warning(f"Role.destroy - user {request.user.username} tried to delete builtin role {role.role_key}")
            return Response({"detail": "内置角色不可删除"}, status=400)
        user_count = UserRoleRel.objects.filter(role=role, status='ACTIVE').count()
        if user_count > 0:
            return Response({"detail": f"该角色被 {user_count} 个用户使用，请先解除用户关联"}, status=400)
        logger.info(f"Role.destroy - user: {request.user.username}, role: {role.role_key}")
        role.delete()
        return Response(status=204)

    @action(detail=True, methods=["post"], url_path="assign-permissions")
    def assign_permissions(self, request, pk=None):
        """批量设置角色权限（全量覆盖）"""
        self._check_super_admin()
        role = self.get_object()
        # 参数校验（协议层）：permission_ids 数组 + 正整数元素，校验失败返回首条错误
        ser = AssignPermissionsSerializer(data=request.data)
        if not ser.is_valid():
            _, detail = _first_serializer_error(ser.errors)
            return Response({"detail": detail}, status=400)
        # 落库逻辑下沉服务层：事务内撤销未保留项 + 批量写入新项
        valid_ids, invalid_count = assign_permissions_to_role(
            role, ser.validated_data['permission_ids'], request.user,
        )
        resp = {"detail": f"已更新 {len(valid_ids)} 个权限"}
        if invalid_count > 0:
            resp["skipped"] = invalid_count
        return Response(resp)
