"""
apps.users.services.ticket_security - 安全配置工单服务（IP 白名单/黑名单/敏感词变更）
"""
from django.db import transaction
from django.utils import timezone
from loguru import logger

from apps.users.models import (
    TicketList, TicketSecurityDetail, User, TicketStatus, TicketBizType,
    SecurityConfigType, SecurityOperation,
)
from apps.users.services.ticket_base import AuditAction, _gen_ticket_no, _log_flow, _write_audit


# 安全配置风险分级策略：
# - 低风险（直接生效）：黑名单新增、敏感词新增
# - 中风险（单审：user_admin 审核）：黑名单解封、敏感词删除/禁用
# - 高风险（双审：user_admin 审核 + super_admin 复核）：白名单新增/删除/编辑
SECURITY_RISK_LEVEL = {
    # IP 白名单：所有操作都是高风险（白名单 = 绕过所有风控）
    (SecurityConfigType.IP_WHITELIST, SecurityOperation.ADD): 'high',
    (SecurityConfigType.IP_WHITELIST, SecurityOperation.EDIT): 'high',
    (SecurityConfigType.IP_WHITELIST, SecurityOperation.DELETE): 'high',
    # IP 黑名单：新增低风险（防御性操作），解封/删除中风险
    (SecurityConfigType.IP_BLACKLIST, SecurityOperation.ADD): 'low',
    (SecurityConfigType.IP_BLACKLIST, SecurityOperation.DELETE): 'normal',
    # 敏感词：新增低风险，编辑/删除/禁用中风险
    (SecurityConfigType.SENSITIVE_WORD, SecurityOperation.ADD): 'low',
    (SecurityConfigType.SENSITIVE_WORD, SecurityOperation.EDIT): 'normal',
    (SecurityConfigType.SENSITIVE_WORD, SecurityOperation.DELETE): 'normal',
    (SecurityConfigType.SENSITIVE_WORD, SecurityOperation.DISABLE): 'normal',
}


def _get_security_risk_level(security_type: str, operation: str) -> str:
    """根据安全配置类型和操作类型返回风险等级

    返回 'low' / 'normal' / 'high'，未匹配时默认 'normal'（走单审）。
    """
    return SECURITY_RISK_LEVEL.get((security_type, operation), 'normal')


def _build_security_approval_chain(risk_level: str) -> list:
    """根据风险等级构建审批链

    - low：空链（直接生效）
    - normal：单审（user_admin 审核）
    - high：双审（user_admin 审核 + super_admin 复核）
    """
    if risk_level == 'low':
        return []
    if risk_level == 'normal':
        return [{'approver_role': 'USER_ADMIN', 'status': 'PENDING',
                 'approver_id': None, 'approved_at': None, 'comment': ''}]
    # high：双审
    return [
        {'approver_role': 'USER_ADMIN', 'status': 'PENDING',
         'approver_id': None, 'approved_at': None, 'comment': ''},
        {'approver_role': 'SUPER_ADMIN', 'status': 'PENDING',
         'approver_id': None, 'approved_at': None, 'comment': ''},
    ]


def create_security_ticket(
    actor: User,
    security_type: str,
    operation: str,
    target_data: dict,
    reason: str,
    old_data: dict = None,
    new_data: dict = None,
    ip_address: str = None,
    user_agent: str = None,
) -> TicketList:
    """创建安全配置变更工单

    根据 security_type + operation 自动判定风险等级：
    - 低风险：直接执行，工单状态设为 EXECUTED
    - 中/高风险：创建待审批工单，等待审批人处理

    返回创建的 TicketList 工单对象。
    """
    risk_level = _get_security_risk_level(security_type, operation)
    approval_chain = _build_security_approval_chain(risk_level)

    # 低风险直接执行
    if risk_level == 'low':
        now = timezone.now()
        ticket = TicketList.objects.create(
            ticket_no=_gen_ticket_no(TicketBizType.SECURITY),
            biz_type=TicketBizType.SECURITY,
            risk_level='low',
            applicant=actor,
            title=f'安全配置: {security_type}:{operation}',
            status=TicketStatus.EXECUTED,
            approval_chain=approval_chain,
            current_step=0,
            approved_at=now,
            executed_at=now,
        )
        # 创建详情记录
        TicketSecurityDetail.objects.create(
            ticket=ticket,
            security_type=security_type,
            operation=operation,
            target_data=target_data,
            old_data=old_data,
            new_data=new_data,
            reason=reason,
        )
        # 直接执行变更
        _execute_security_change(ticket)
        # 流转日志（与权限/组织工单一致：SUBMIT + EXECUTE，供工单中心时间线渲染）
        _log_flow(ticket, 'SUBMIT', actor=actor)
        _log_flow(ticket, 'EXECUTE', actor=actor)
        # 写审计日志
        _write_audit(ticket, actor, AuditAction.TICKET_CREATE, ip_address, user_agent)
        _write_audit(ticket, actor, AuditAction.TICKET_EXECUTE, ip_address, user_agent)
        logger.info(f'[SecurityTicket] 低风险直接执行: {security_type}:{operation} '
                    f'ticket={ticket.ticket_no} by={actor.id}')
        return ticket

    # 中/高风险走审批
    ticket = TicketList.objects.create(
        ticket_no=_gen_ticket_no(TicketBizType.SECURITY),
        biz_type=TicketBizType.SECURITY,
        risk_level=risk_level,
        applicant=actor,
        title=f'安全配置: {security_type}:{operation}',
        status=TicketStatus.PENDING,
        approval_chain=approval_chain,
        current_step=0,
    )
    # 创建详情记录
    TicketSecurityDetail.objects.create(
        ticket=ticket,
        security_type=security_type,
        operation=operation,
        target_data=target_data,
        old_data=old_data,
        new_data=new_data,
        reason=reason,
    )
    # 写流转日志（与权限/组织工单一致：SUBMIT，供工单中心时间线渲染）
    _log_flow(ticket, 'SUBMIT', actor=actor)
    # 写审计日志
    _write_audit(ticket, actor, AuditAction.TICKET_CREATE, ip_address, user_agent)
    logger.info(f'[SecurityTicket] 创建审批工单: {security_type}:{operation} '
                f'risk={risk_level} ticket={ticket.ticket_no} by={actor.id}')
    return ticket


@transaction.atomic
def _execute_security_change(ticket: TicketList):
    """执行安全配置变更 —— 根据工单详情中的 security_type + operation 路由到对应 Service

    由 create_security_ticket（低风险直接执行）和 approve_ticket（审批通过后执行）调用。
    """
    from apps.security.models import IpWhitelist, IpBlacklist, SensitiveWord

    detail = ticket.security_detail
    if not detail:
        logger.error(f'[SecurityTicket] 工单 {ticket.ticket_no} 缺少 security_detail')
        return

    security_type = detail.security_type
    operation = detail.operation
    target = detail.target_data
    new_data = detail.new_data or {}

    if security_type == SecurityConfigType.IP_WHITELIST:
        _execute_ip_whitelist(operation, target, new_data, ticket)
    elif security_type == SecurityConfigType.IP_BLACKLIST:
        _execute_ip_blacklist(operation, target, new_data, ticket)
    elif security_type == SecurityConfigType.SENSITIVE_WORD:
        _execute_sensitive_word(operation, target, new_data, ticket)
    else:
        logger.error(f'[SecurityTicket] 未知的安全配置类型: {security_type}')


def _execute_ip_whitelist(operation: str, target: dict, new_data: dict, ticket: TicketList):
    """执行 IP 白名单变更"""
    from apps.security.models import IpWhitelist

    if operation == SecurityOperation.ADD:
        ip = target.get('ip_pattern', '') or target.get('ip_or_cidr', '')
        IpWhitelist.objects.create(
            ip_or_cidr=ip,
            description=target.get('description', ''),
            is_enabled=True,
        )
    elif operation == SecurityOperation.EDIT:
        obj = IpWhitelist.objects.filter(id=target.get('id')).first()
        if obj:
            # 仅允许更新白名单业务字段，防止 new_data 中的非法 key 污染模型其他字段
            _ALLOWED_FIELDS = {'ip_or_cidr', 'description'}
            for key, val in new_data.items():
                if key in _ALLOWED_FIELDS:
                    setattr(obj, key, val)
            obj.save()
    elif operation == SecurityOperation.DELETE:
        IpWhitelist.objects.filter(id=target.get('id')).delete()

    logger.info(f'[SecurityTicket] IP白名单变更执行: op={operation} target={target}')


def _execute_ip_blacklist(operation: str, target: dict, new_data: dict, ticket: TicketList):
    """执行 IP 黑名单变更"""
    from apps.security.models import IpBlacklist

    if operation == SecurityOperation.ADD:
        ip = target.get('ip_pattern', '') or target.get('ip', '')
        IpBlacklist.objects.create(
            ip=ip,
            reason=target.get('reason', ''),
            detail=target.get('detail', ''),
            is_active=True,
        )
    elif operation == SecurityOperation.DELETE:
        # 解封：标记为非活跃
        obj = IpBlacklist.objects.filter(id=target.get('id')).first()
        if obj:
            obj.is_active = False
            obj.save()

    logger.info(f'[SecurityTicket] IP黑名单变更执行: op={operation} target={target}')


def _execute_sensitive_word(operation: str, target: dict, new_data: dict, ticket: TicketList):
    """执行敏感词变更"""
    from apps.security.models import SensitiveWord
    from django.db import IntegrityError

    if operation == SecurityOperation.ADD:
        try:
            SensitiveWord.objects.create(
                word=target.get('word', ''),
                category=target.get('category', 'custom'),
                action=target.get('action', 'mask'),
                is_regex=target.get('is_regex', False),
                is_enabled=True,
            )
        except IntegrityError:
            # 并发竞态：exists() 检查与 create 之间另一请求已创建相同 word
            logger.warning(f'[SecurityTicket] 敏感词已存在(竞态): word={target.get("word")}')
    elif operation == SecurityOperation.EDIT:
        obj = SensitiveWord.objects.filter(id=target.get('id')).first()
        if obj:
            # 仅允许更新敏感词业务字段，防止 new_data 中的非法 key 污染模型其他字段
            _ALLOWED_FIELDS = {'word', 'category', 'action', 'is_regex'}
            for key, val in new_data.items():
                if key in _ALLOWED_FIELDS:
                    setattr(obj, key, val)
            obj.save()
    elif operation == SecurityOperation.DELETE:
        SensitiveWord.objects.filter(id=target.get('id')).delete()
    elif operation == SecurityOperation.DISABLE:
        obj = SensitiveWord.objects.filter(id=target.get('id')).first()
        if obj:
            obj.is_enabled = False
            obj.save()

    logger.info(f'[SecurityTicket] 敏感词变更执行: op={operation} target={target}')
