"""
apps.users.ticket_service - 权限配置审批工单服务（门面模块）

审批规则（对齐 RAG_RBAC_权限架构设计.md 最终计划）：
- 同部门授权（GRANT team_leader/employee，目标用户与申请人同团队）：团队组长单审即可
- 跨部门/跨团队/全局角色：双轨审核（审核 + 复核）
- super_admin 新增/撤销：强制另一个 super_admin 双人复核
- 降级/撤销（REVOKE）：团队组长可直接执行，无需审批（但记审计）
- 任一节点 REJECTED → 工单终态 REJECTED，不执行授权表写入
- 审批工单永不删除，只改状态

工单流转状态机：
  PENDING --approve(末节点)--> APPROVED --execute(异步/同步)--> EXECUTED
  PENDING --reject--> REJECTED（终态）
  PENDING --cancel(发起人)--> CANCELLED（终态）

审批链 approval_chain 结构（JSONField，顺序执行，共享审批池模式）：
  [
    {"approver_role": "TEAM_LEADER", "status": "PENDING",
     "approver_id": null, "approved_at": null, "comment": ""},
    ...
  ]
  - approver_role：审批人角色定位（TEAM_LEADER / DEPT_LEADER / SUPER_ADMIN）
    创建时锁定角色类型，不锁定具体审批人（共享审批池 + 先到先得）
  - approver_id：审批时回填（谁先处理就锁定谁，防止并发审批）
  - status：PENDING / APPROVED / REJECTED
  - 顺序执行：current_step 指向待审批节点，前一节点 APPROVED 才到下一节点

本模块同时承载权限域与系统域共用的工单纯函数（审批链解析/双人独立性判定），
供 apps.users.views 与 apps.system.views 共用，避免序列化与审批判定逻辑两处漂移。

注意：本文件为纯门面（facade），实际实现已拆分为 services 子包：
- services/ticket_base.py：常量/枚举 + 工单号生成 + 流转日志 + 审计写入
- services/approval_chain.py：审批人角色匹配 + 审批链构造
- services/ticket_permission.py：权限工单创建/流转/授权执行
- services/ticket_security.py：安全配置工单（IP 白名单/黑名单/敏感词）
- services/ticket_org.py：组织变更工单（部门/团队增删改）
此处仅做显式 import 重新导出，保持原有对外接口（函数签名与行为）不变。
"""
# 原模块顶层 import 的模型名保持为模块属性（外部测试/代码可能按
# apps.users.ticket_service.<Model> 方式引用或 patch，故原样保留）
from apps.users.models import (
    TicketList, TicketPermissionDetail, TicketFlowLog, PermissionAuditLog,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    Role, User, Department, Team,
    TicketStatus, TicketChangeType, ScopeType, RoleType, GrantStatus,
    AuditTargetType, RoleConflictRule, TicketBizType,
    TicketSecurityDetail, SecurityConfigType, SecurityOperation,
    TicketOrgDetail, OrgChangeType, OrgOperation,
    TicketRoleDetail, RoleOperation,
)

from apps.users.services.ticket_base import (
    TICKET_TYPE_PREFIX,
    TEAM_ROLE_RANK, TEAM_ROLE_KEYS,
    ApproverRole, ApproveStepStatus, AuditAction,
    parse_change_summary, get_approved_approver_ids,
    _gen_ticket_no, _create_ticket_with_retry, _log_flow, _write_audit,
)
from apps.users.services.approval_chain import (
    GLOBAL_HIGH_PRIVILEGE_KEYS,
    _can_approve_for_role,
    _get_team_leader_id, _get_dept_leader_id, _get_super_admin_ids,
    _check_sod_conflict, _detect_team_role_in_service, _detect_dept_role_in_service,
    _check_super_admin_quota, _resolve_team_leader, _resolve_dept_leader,
    _build_chain_node, _build_super_admin_chain_2step, _build_user_admin_then_super_chain,
    build_approval_chain, _build_grant_chain_for_team_role, _build_revoke_chain_for_team_role,
)
from apps.users.services.ticket_permission import (
    _create_permission_ticket, create_ticket, approve_ticket, reject_ticket, cancel_ticket,
    _execute_grant_or_revoke, _sync_leader_for_role, _apply_role_change,
    _apply_grant, _apply_revoke, _apply_extend,
)
from apps.users.services.ticket_security import (
    SECURITY_RISK_LEVEL, _get_security_risk_level, _build_security_approval_chain,
    create_security_ticket, _execute_security_change,
    _execute_ip_whitelist, _execute_ip_blacklist, _execute_sensitive_word,
)
from apps.users.services.ticket_org import (
    ORG_RISK_LEVEL, _get_org_risk_level, _build_org_approval_chain, create_org_ticket,
    _execute_org_change, _execute_dept_change, _create_dept, _update_dept, _delete_dept,
    _execute_team_change, _create_team, _update_team, _delete_team,
)
from apps.users.services.ticket_role import (
    ROLE_RISK_LEVEL, _get_role_risk_level, _build_role_approval_chain,
    create_role_ticket, _execute_role_change,
    _apply_role_add, _apply_role_edit, _apply_role_delete, _apply_role_assign_perms,
)
