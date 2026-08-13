"""组织视图：部门 / 团队（CRUD 均走组织变更工单，审批通过后由执行层落库）

编码生成与唯一性保障下沉至 services/org_service；
组织变更工单创建复用 ticket_service.create_org_ticket。
"""
from django.db import models
from loguru import logger

from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.users.models import (
    Department, Team, User,
    has_permission, get_user_data_scope_level, get_user_managed_depts, DataScope,
    OrgChangeType, OrgOperation,
)
from apps.users.serializers import (
    DepartmentSerializer, DepartmentWriteSerializer,
    TeamSerializer, TeamWriteSerializer,
)
from apps.users.services.org_service import _auto_code, _ensure_unique_code
from apps.users.ticket_service import create_org_ticket
from apps.users.utils import _client_ip, _client_ua


class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.filter(is_deleted=False).order_by("id")\
        .select_related('leader')
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from django.db.models import Prefetch
        # teams 的 annotate(user_count) 供 DepartmentSerializer.get_teams 直接读取，
        # 避免嵌套团队逐个统计成员数的 N+1 查询
        return super().get_queryset().prefetch_related(
            Prefetch('teams', queryset=Team.objects.filter(is_deleted=False).select_related('leader')
                     .annotate(user_count=models.Count('members', filter=models.Q(members__is_deleted=False))))
        ).annotate(user_count=models.Count('users', filter=models.Q(users__is_deleted=False)))

    def _check_can_manage_dept(self):
        """检查是否有部门管理权限（RBAC：user.manage_all，通过 is_user_admin 属性判定）"""
        u = self.request.user
        if u.is_user_admin:
            return True
        raise PermissionDenied("无部门管理权限")

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return DepartmentWriteSerializer
        return DepartmentSerializer

    def create(self, request, *args, **kwargs):
        self._check_can_manage_dept()
        data = request.data.copy()
        name = data.get("name", "").strip()
        # leader_id 不再直接写库:部门经理通过任命工单(GRANT dept_manager)设置,
        # 审批通过后由工单执行同步 Department.leader_id
        data.pop("leader_id", None)
        if not data.get("code", "").strip():
            data["code"] = _auto_code(name)
        data["code"] = _ensure_unique_code(data["code"], Department)

        if Department.objects.filter(name=name, is_deleted=False).exists():
            return Response({"detail": f"部门“{name}”已存在"}, status=400)

        # 组织变更一律走工单:创建时预检,审批通过后由 _execute_org_change 落库
        # (软删同名行恢复语义也在执行层处理,保证 KnowledgeNode ref_id 身份)
        ticket = create_org_ticket(
            actor=request.user,
            org_type=OrgChangeType.DEPT,
            operation=OrgOperation.ADD,
            target_data={"name": name, "code": data["code"]},
            reason=f'新增部门: {name}',
            new_data={"name": name, "code": data["code"]},
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )
        logger.info(f"Department.create - user: {request.user.username}, name: {name}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "部门新增需审批，已创建工单",
        }, status=201)

    def update(self, request, *args, **kwargs):
        self._check_can_manage_dept()
        dept = self.get_object()
        # leader_id 不再直接写库(同 create),部门经理通过任命工单设置
        data = {k: v for k, v in request.data.items() if k != 'leader_id'}
        name = data.get("name")
        code = data.get("code")
        # 创建工单前预检唯一性(审批期间变化由执行层二次校验兜底)
        if name is not None:
            name = str(name).strip()
            if Department.objects.filter(name=name, is_deleted=False).exclude(id=dept.id).exists():
                return Response({"detail": f"部门“{name}”已存在"}, status=400)
        if code is not None:
            code = str(code).strip()
            if Department.objects.filter(code=code, is_deleted=False).exclude(id=dept.id).exists():
                return Response({"detail": "部门编码冲突"}, status=400)

        new_data = {
            "name": name if name is not None else dept.name,
            "code": (code if code is not None else (dept.code or '')) or '',
        }
        ticket = create_org_ticket(
            actor=request.user,
            org_type=OrgChangeType.DEPT,
            operation=OrgOperation.EDIT,
            target_data={"id": dept.id, "name": dept.name, "code": dept.code or ''},
            reason=f'编辑部门: {dept.name}',
            old_data={"name": dept.name, "code": dept.code or ''},
            new_data=new_data,
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )
        logger.info(f"Department.update - user: {request.user.username}, id: {dept.id}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "部门编辑需审批，已创建工单",
        })

    def destroy(self, request, *args, **kwargs):
        self._check_can_manage_dept()
        dept = self.get_object()
        # 创建工单前预检(审批期间变化由执行层二次校验兜底)
        user_count = User.objects.filter(department=dept, is_deleted=False).count()
        if user_count > 0:
            return Response({"detail": f"该部门下还有 {user_count} 个用户，无法删除"}, status=400)
        team_count = Team.objects.filter(department=dept, is_deleted=False).count()
        if team_count > 0:
            return Response({"detail": f"该部门下还有 {team_count} 个团队，请先删除或迁移团队"}, status=400)

        ticket = create_org_ticket(
            actor=request.user,
            org_type=OrgChangeType.DEPT,
            operation=OrgOperation.DELETE,
            target_data={"id": dept.id, "name": dept.name},
            reason=f'删除部门: {dept.name}',
            old_data={"name": dept.name, "code": dept.code or ''},
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )
        logger.info(f"Department.destroy - user: {request.user.username}, id: {dept.id}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "部门删除为高风险操作，已创建工单，需双审后生效",
        })


class TeamViewSet(viewsets.ModelViewSet):
    queryset = Team.objects.filter(is_deleted=False).order_by("id")\
        .select_related('leader', 'department')
    serializer_class = TeamSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # 支持 ?department_id=xxx 按部门过滤团队(用于申请权限时部门→团队级联选择)
        qs = super().get_queryset()
        dept_id = self.request.query_params.get("department_id")
        if dept_id:
            try:
                qs = qs.filter(department_id=int(dept_id))
            except (TypeError, ValueError):
                pass
        return qs

    def _check_can_manage_team(self, dept_id=None):
        """检查是否有团队管理权限（RBAC：user.manage_all 或 user.manage + DEPT scope）"""
        u = self.request.user
        # is_user_admin 判定 user.manage_all 权限（全局用户管理）
        if u.is_user_admin:
            return True
        # 部门经理只能管理自己部门的团队（user.manage + DEPT scope）
        if has_permission(u, 'user.manage') and get_user_data_scope_level(u) == DataScope.DEPT:
            if dept_id is None:
                raise PermissionDenied("部门经理仅可操作本部门团队")
            # get_user_managed_depts（含属地授权部门）
            manager_depts = get_user_managed_depts(u)
            if dept_id in manager_depts:
                return True
        raise PermissionDenied("无权限操作团队")

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return TeamWriteSerializer
        return TeamSerializer

    def create(self, request, *args, **kwargs):
        logger.info(f"Team.create - request user: {request.user.username}, data: {request.data}")

        data = dict(request.data)
        name = data.get("name", "").strip()
        dept_id = data.get("department_id")
        # leader_id 不再直接写库:团队组长通过任命工单(GRANT team_leader)设置,
        # 审批通过后由工单执行同步 Team.leader_id
        data.pop("leader_id", None)

        if not dept_id:
            logger.error(f"Team.create - department_id is required but got: {dept_id}")
            return Response({"detail": "部门ID不能为空"}, status=400)

        if isinstance(dept_id, list):
            dept_id = dept_id[0]

        dept_id = int(dept_id)
        self._check_can_manage_team(dept_id)

        dept = Department.objects.filter(id=dept_id).first()
        if not dept:
            logger.error(f"Team.create - department_id {dept_id} does not exist")
            return Response({"detail": "指定的部门不存在"}, status=400)

        # 组织变更一律走工单:创建时预检,审批通过后由 _execute_org_change 落库
        # (软删同名行恢复语义也在执行层处理,保证 KnowledgeNode ref_id 身份)
        if Team.objects.filter(name=name, department_id=dept_id, is_deleted=False).exists():
            return Response({"detail": f"部门“{dept.name}”下已存在团队“{name}”"}, status=400)

        if not data.get("code", "").strip():
            logger.info(f"Team.create - auto generating code, department_id: {dept_id}")
            prefix = dept.code or _auto_code(dept.name)
            data["code"] = _auto_code(name, prefix)
        data["code"] = _ensure_unique_code(data["code"], Team)

        ticket = create_org_ticket(
            actor=request.user,
            org_type=OrgChangeType.TEAM,
            operation=OrgOperation.ADD,
            target_data={"name": name, "code": data["code"], "department_id": dept_id},
            reason=f'新增团队: {name}(部门: {dept.name})',
            new_data={"name": name, "code": data["code"], "department_id": dept_id,
                      "description": data.get("description", "")},
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )
        logger.info(f"Team.create - user: {request.user.username}, name: {name}, department_id: {dept_id}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "团队新增需审批，已创建工单",
        }, status=201)

    def update(self, request, *args, **kwargs):
        team = self.get_object()
        self._check_can_manage_team(team.department_id)
        # leader_id 不再直接写库(同 create),团队组长通过任命工单设置
        data = {k: v for k, v in request.data.items() if k != 'leader_id'}
        new_name = data.get("name")
        new_code = data.get("code")
        new_dept_id = data.get("department_id")
        # 创建工单前预检唯一性(审批期间变化由执行层二次校验兜底)
        if new_dept_id is not None:
            try:
                new_dept_id = int(new_dept_id)
            except (TypeError, ValueError):
                return Response({"detail": "部门ID不合法"}, status=400)
            self._check_can_manage_team(new_dept_id)
            if not Department.objects.filter(id=new_dept_id, is_deleted=False).exists():
                return Response({"detail": "指定的部门不存在"}, status=400)
        if new_name is not None:
            new_name = str(new_name).strip()
            if Team.objects.filter(name=new_name, department_id=new_dept_id or team.department_id,
                                  is_deleted=False).exclude(id=team.id).exists():
                return Response({"detail": f"部门下已存在团队“{new_name}”"}, status=400)
        if new_code is not None:
            new_code = str(new_code).strip()
            if Team.objects.filter(code=new_code, is_deleted=False).exclude(id=team.id).exists():
                return Response({"detail": "团队编码冲突"}, status=400)

        old_dept = team.department
        new_data = {
            "name": new_name if new_name is not None else team.name,
            "code": (new_code if new_code is not None else (team.code or '')) or '',
            "department_id": new_dept_id if new_dept_id is not None else team.department_id,
            "description": data.get("description", team.description or ''),
        }
        ticket = create_org_ticket(
            actor=request.user,
            org_type=OrgChangeType.TEAM,
            operation=OrgOperation.EDIT,
            target_data={"id": team.id, "name": team.name,
                         "department_id": team.department_id, "department_name": old_dept.name if old_dept else ''},
            reason=f'编辑团队: {team.name}',
            old_data={"name": team.name, "code": team.code or '',
                      "department_id": team.department_id,
                      "department_name": old_dept.name if old_dept else '',
                      "description": team.description or ''},
            new_data=new_data,
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )
        logger.info(f"Team.update - user: {request.user.username}, id: {team.id}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "团队编辑需审批，已创建工单",
        })

    def destroy(self, request, *args, **kwargs):
        team = self.get_object()
        self._check_can_manage_team(team.department_id)
        # 创建工单前预检(审批期间变化由执行层二次校验兜底)
        # 单团队 FK：统计 User.team 指向该团队的用户
        user_count = User.objects.filter(team=team, is_deleted=False).count()
        if user_count > 0:
            return Response({"detail": f"该团队下还有 {user_count} 个成员，无法删除"}, status=400)

        # 检查团队节点及子孙分类节点下是否有文档
        from apps.knowledge.models import KnowledgeNode
        from apps.knowledge.node_sync import count_docs_in_subtree
        team_node = KnowledgeNode.objects.filter(
            node_level=3, ref_id=team.id, is_deleted=False
        ).first()
        if team_node:
            doc_count = count_docs_in_subtree(team_node.id)
            if doc_count > 0:
                return Response(
                    {"detail": f"该团队下有 {doc_count} 个文档，请先迁移或删除后再操作"},
                    status=400
                )

        ticket = create_org_ticket(
            actor=request.user,
            org_type=OrgChangeType.TEAM,
            operation=OrgOperation.DELETE,
            target_data={"id": team.id, "name": team.name, "department_id": team.department_id},
            reason=f'删除团队: {team.name}',
            old_data={"name": team.name, "code": team.code or '', "department_id": team.department_id},
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )
        logger.info(f"Team.destroy - user: {request.user.username}, id: {team.id}, ticket: {ticket.ticket_no}")
        return Response({
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "risk_level": ticket.risk_level,
            "detail": "团队删除为高风险操作，已创建工单，需双审后生效",
        })
