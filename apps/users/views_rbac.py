"""RBAC 视图：权限点 / 角色 CRUD（仅超级管理员可操作，角色变更一律走工单）

角色新增/编辑/删除/权限分配均走角色变更工单（ticket_service.create_role_ticket），
审批通过后由工单执行层落库，创建工单时只做预检不落库。
角色权限分配（assign_permissions）参数校验走 AssignPermissionsSerializer。
"""
from loguru import logger

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.users.models import Permission, Role, RolePermissionRel, UserRoleRel, RoleOperation
from apps.users.serializers import RoleSerializer, PermissionSerializer, AssignPermissionsSerializer
from apps.users.services.rbac_service import assign_permissions_to_role
from apps.users.ticket_service import create_role_ticket
from apps.users.utils import _first_serializer_error, _client_ip, _client_ua


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
    """角色 CRUD（仅超级管理员可操作，增删改/权限分配一律走角色变更工单）

    变更流程：提交时预检 + 创建工单 → 另一超管审批 → 审批通过后工单执行层落库。
    列表/详情仍返回当前生效的角色（不含已软删），审批中的变更不影响现有角色。
    """
    queryset = Role.objects.filter(is_deleted=False).order_by("id")
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
        """新增角色 → 创建"新增角色"工单，审批通过后落库"""
        self._check_super_admin()
        # 提交时用序列化器校验字段（编码格式/长度），不落库
        ser = RoleSerializer(data=request.data)
        if not ser.is_valid():
            _, detail = _first_serializer_error(ser.errors)
            return Response({"detail": detail}, status=400)
        data = ser.validated_data
        code = data['role_key']
        name = data.get('name', '')
        # 预检角色编码唯一性（排除已软删角色，同名软删走执行层恢复而非报错；
        # 审批期间冲突由执行层二次校验兜底）
        if Role.objects.filter(role_key=code, is_deleted=False).exists():
            return Response({"detail": f"角色编码已存在: {code}"}, status=400)

        try:
            ticket = create_role_ticket(
                actor=request.user,
                operation=RoleOperation.ADD,
                new_data={'code': code, 'name': name,
                          'description': data.get('description') or ''},
                reason=f'新增角色: {name}',
                ip_address=_client_ip(request),
                user_agent=_client_ua(request),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        logger.info(f"Role.create - user: {request.user.username}, code: {code}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "角色新增需审批，已创建工单",
        }, status=201)

    def update(self, request, *args, **kwargs):
        """编辑角色 → 创建"编辑角色"工单，审批通过后落库"""
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
        # 提交时用序列化器校验字段，不落库
        ser = self.get_serializer(role, data=request.data, partial=partial)
        if not ser.is_valid():
            _, detail = _first_serializer_error(ser.errors)
            return Response({"detail": detail}, status=400)
        data = ser.validated_data
        # 预检名称/编码唯一性（审批期间冲突由执行层二次校验兜底）
        new_name = data.get('name')
        new_code = data.get('role_key')
        if new_name is not None and Role.objects.filter(
                name=new_name, is_deleted=False).exclude(id=role.id).exists():
            return Response({"detail": f"角色名称已存在: {new_name}"}, status=400)
        if new_code is not None and Role.objects.filter(
                role_key=new_code, is_deleted=False).exclude(id=role.id).exists():
            return Response({"detail": f"角色编码已存在: {new_code}"}, status=400)

        try:
            ticket = create_role_ticket(
                actor=request.user,
                operation=RoleOperation.EDIT,
                target_role=role,
                old_data={'code': role.role_key, 'name': role.name,
                          'description': role.description or ''},
                new_data={'code': new_code or role.role_key,
                          'name': new_name or role.name,
                          'description': data.get('description', role.description or '')},
                reason=f'编辑角色: {role.name}',
                ip_address=_client_ip(request),
                user_agent=_client_ua(request),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        logger.info(f"Role.update - user: {request.user.username}, role: {role.role_key}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "角色编辑需审批，已创建工单",
        })

    def destroy(self, request, *args, **kwargs):
        """删除角色 → 创建"删除角色"工单（高风险双审），审批通过后软删"""
        self._check_super_admin()
        role = self.get_object()
        if role.is_builtin:
            logger.warning(f"Role.destroy - user {request.user.username} tried to delete builtin role {role.role_key}")
            return Response({"detail": "内置角色不可删除"}, status=400)
        user_count = UserRoleRel.objects.filter(role=role, status='ACTIVE').count()
        if user_count > 0:
            return Response({"detail": f"该角色被 {user_count} 个用户使用，请先解除用户关联"}, status=400)

        try:
            ticket = create_role_ticket(
                actor=request.user,
                operation=RoleOperation.DELETE,
                target_role=role,
                old_data={'code': role.role_key, 'name': role.name,
                          'description': role.description or ''},
                reason=f'删除角色: {role.name}',
                ip_address=_client_ip(request),
                user_agent=_client_ua(request),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        logger.info(f"Role.destroy - user: {request.user.username}, role: {role.role_key}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "角色删除为高风险操作，已创建工单，需双审后生效",
        })

    @action(detail=True, methods=["post"], url_path="assign-permissions")
    def assign_permissions(self, request, pk=None):
        """批量设置角色权限（全量覆盖）→ 创建"权限分配"工单，审批通过后生效"""
        self._check_super_admin()
        role = self.get_object()
        # 参数校验（协议层）：permission_ids 数组 + 正整数元素，校验失败返回首条错误
        ser = AssignPermissionsSerializer(data=request.data)
        if not ser.is_valid():
            _, detail = _first_serializer_error(ser.errors)
            return Response({"detail": detail}, status=400)
        permission_ids = ser.validated_data['permission_ids']
        # 提交时校验权限点存在性，过滤无效 ID 供响应提示（执行层仍会二次过滤）
        existing_ids = set(Permission.objects.filter(
            id__in=permission_ids).values_list("id", flat=True))
        valid_ids = [pid for pid in permission_ids if pid in existing_ids]
        invalid_count = len(permission_ids) - len(valid_ids)

        try:
            ticket = create_role_ticket(
                actor=request.user,
                operation=RoleOperation.ASSIGN_PERMS,
                target_role=role,
                old_data={'code': role.role_key, 'name': role.name},
                permission_ids=valid_ids,
                reason=f'角色权限分配: {role.name}',
                ip_address=_client_ip(request),
                user_agent=_client_ua(request),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        resp = {
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": f"已提交权限分配工单，审批通过后更新 {len(valid_ids)} 个权限",
        }
        if invalid_count > 0:
            resp["skipped"] = invalid_count
        return Response(resp)
