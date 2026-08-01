"""
apps.users.audit_service - 统一权限审计日志写入服务

业务背景：
    所有权限相关操作（组织架构变更、知识节点操作、角色/范围授予与撤销、
    审批工单流转、文档/节点共享与黑名单、登录安全事件）必须全域留痕，统一写入
    PermissionAuditLog。审计日志是合规底线：只追加、永不删、只走 INSERT，且写入失败
    不得阻断主业务（审计可丢、业务不可丢）。

设计约束：
    - 同步写入：审计必须可靠，不走异步队列（避免丢日志）；用 try/except 兜底，失败仅记日志。
    - 不阻断主业务：write_audit 内部捕获一切异常，仅 logger.error，绝不向上抛出。
    - 只 INSERT：仅使用 objects.create，绝不 update/delete 审计记录。

适用场景：
    - 服务层在执行权限敏感操作前后调用 write_audit。
    - 用 audit_action 装饰器对纯服务函数一键留痕。
    - 用 AuditContext 上下文管理器对含多步分支的复杂逻辑块留痕，并支持运行时设置 after 快照。
"""
import functools
import inspect

from loguru import logger

from apps.users.models import (
    PermissionAuditLog, AuditTargetType, ScopeType, Role, User,
)


# ============================================================================
# 审计动作常量（对齐 PermissionAuditLog.action 清单）
# ----------------------------------------------------------------------------
# 这些常量是审计日志 action 字段的唯一合法取值来源，散落在各业务模块的硬编码字符串
# 统一收敛到这里，避免拼写不一致导致审计检索漏数据。新增 action 时务必同步本清单与
# PermissionAuditLog 模型 docstring。
# ============================================================================
class AuditAction:
    """权限审计动作枚举 —— 按 business domain 分组，覆盖全域权限敏感操作"""

    # ---- 组织架构：部门/团队/用户的增删改与归属变更 ----
    DEPT_CREATE = 'DEPT_CREATE'
    DEPT_UPDATE = 'DEPT_UPDATE'
    DEPT_DELETE = 'DEPT_DELETE'
    TEAM_CREATE = 'TEAM_CREATE'
    TEAM_UPDATE = 'TEAM_UPDATE'
    TEAM_DELETE = 'TEAM_DELETE'
    USER_INVITE = 'USER_INVITE'
    USER_TRANSFER = 'USER_TRANSFER'
    USER_LEAVE = 'USER_LEAVE'

    # ---- 知识节点：节点树结构变更（节点树前 3 层自动同步，此处记录业务层操作）----
    NODE_CREATE = 'NODE_CREATE'
    NODE_MOVE = 'NODE_MOVE'
    NODE_RENAME = 'NODE_RENAME'
    NODE_DELETE = 'NODE_DELETE'

    # ---- 权限配置：角色/范围授予撤销、有效期延长（含自动过期）----
    ROLE_GRANT = 'ROLE_GRANT'
    ROLE_REVOKE = 'ROLE_REVOKE'
    SCOPE_GRANT = 'SCOPE_GRANT'
    SCOPE_REVOKE = 'SCOPE_REVOKE'
    EXPIRE_EXTEND = 'EXPIRE_EXTEND'
    EXPIRE_AUTO = 'EXPIRE_AUTO'

    # ---- 审批流：工单全生命周期（工单本身永不删，状态流转全程留痕）----
    TICKET_CREATE = 'TICKET_CREATE'
    TICKET_APPROVE = 'TICKET_APPROVE'
    TICKET_REJECT = 'TICKET_REJECT'
    TICKET_CANCEL = 'TICKET_CANCEL'
    TICKET_EXECUTE = 'TICKET_EXECUTE'

    # ---- 资源授权：文档/节点共享授予撤销与到期（检索层过滤依赖这些事件）----
    DOC_SHARE_GRANT = 'DOC_SHARE_GRANT'
    DOC_SHARE_REVOKE = 'DOC_SHARE_REVOKE'
    DOC_SHARE_EXPIRE = 'DOC_SHARE_EXPIRE'
    NODE_SHARE_GRANT = 'NODE_SHARE_GRANT'
    NODE_SHARE_REVOKE = 'NODE_SHARE_REVOKE'

    # ---- 访问黑名单：文档/节点级封禁（敏感资源主动隔离）----
    DOC_BLOCK_ADD = 'DOC_BLOCK_ADD'
    DOC_BLOCK_REMOVE = 'DOC_BLOCK_REMOVE'
    DOC_BLOCK_EXPIRE = 'DOC_BLOCK_EXPIRE'
    NODE_BLOCK_ADD = 'NODE_BLOCK_ADD'
    NODE_BLOCK_REMOVE = 'NODE_BLOCK_REMOVE'

    # ---- 登录安全：登录成败、登出、改密、Token 刷新（安全审计与锁定策略依据）----
    LOGIN_SUCCESS = 'LOGIN_SUCCESS'
    LOGIN_FAIL = 'LOGIN_FAIL'
    LOGOUT = 'LOGOUT'
    PASSWORD_CHANGE = 'PASSWORD_CHANGE'
    TOKEN_REFRESH = 'TOKEN_REFRESH'


# ============================================================================
# 核心写入函数
# ============================================================================
def write_audit(actor, action, target_type, target_id=None, target_user=None,
                role=None, scope_type=ScopeType.NONE, scope_id=None,
                before=None, after=None, result='SUCCESS',
                ip_address=None, user_agent=''):
    """同步写入一条权限审计日志（只 INSERT、不阻断主业务）

    功能：
        向 PermissionAuditLog 追加一条记录。所有权限敏感操作最终都应汇聚到这里，
        保证全域留痕口径一致。写入失败仅 logger.error，绝不向上抛异常——审计可丢，
        业务不可丢。

    输入：
        actor: 操作人 User 实例；可为 None（如系统自动任务/匿名登录失败事件）。
        action: AuditAction 常量字符串。
        target_type: AuditTargetType 成员或其字符串值，标识操作对象类别。
        target_id: 操作对象主键（BigInteger），无对象时为 None。
        target_user: 当对象是人时填目标用户，便于按人反查审计（检索高频路径）。
        role: 涉及的角色，权限配置类操作必填。
        scope_type: 管辖范围类型，默认 NONE（全局角色/无范围）。
        scope_id: 范围对象 ID（部门/团队 ID），scope_type 非 NONE 时填。
        before: 变更前快照 dict，写入 before_snapshot；无则 None。
        after: 变更后快照 dict，写入 after_snapshot；无则 None。
        result: 'SUCCESS' 或 'FAIL'（+ 失败码），默认 SUCCESS。
        ip_address: 客户端 IP；空值归一为 None 以适配 GenericIPAddressField。
        user_agent: 客户端 UA；空值归一为 ''。

    输出：
        成功返回 PermissionAuditLog 实例；失败返回 None（且不抛异常）。

    适用场景：
        服务层在权限敏感操作前后显式调用；audit_action 装饰器与 AuditContext
        内部也委托本函数落盘。
    """
    # GenericIPAddressField 不接受空字符串，falsy 归一为 None；UA 保空串。
    ip_address = ip_address or None
    user_agent = user_agent or ''

    try:
        log = PermissionAuditLog.objects.create(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_user=target_user,
            role=role,
            scope_type=scope_type,
            scope_id=scope_id,
            before_snapshot=before,
            after_snapshot=after,
            result=result,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return log
    except Exception as exc:  # noqa: BLE001 —— 审计兜底必须捕获一切异常
        # 审计写入失败不得阻断主业务：仅记日志便于运维排查，不向上抛出。
        # 常见失败原因：DB 连接抖动、IP/UA 格式异常、字段超长；均不应影响业务事务。
        logger.error(
            '[Audit] write_audit failed (不阻断主业务): action={} target_type={} '
            'target_id={} actor={} result={} err={}',
            action, target_type, target_id,
            getattr(actor, 'id', None), result, exc,
        )
        return None


# ============================================================================
# 装饰器：对服务函数一键留痕
# ============================================================================
def audit_action(action, target_type, target_id_arg=None, before_fn=None, after_fn=None):
    """装饰器：为服务函数自动写入权限审计日志

    业务背景：
        权限敏感的服务函数需要全程留痕，手工写审计易遗漏。本装饰器统一捕获
        操作人、目标对象、变更前后快照，成功写 SUCCESS、异常写 FAIL 并 re-raise
        （业务异常仍按原语义抛出，审计只做旁路记录）。

    约定：
        - 被装饰函数的第一位置参数为 actor（User 实例）或 request（含 .user）。
          匿名用户（AnonymousUser）不算合法 actor，记为 None。
        - target_id_arg 指定参数名，从 args/kwargs 中按签名绑定取值作为 target_id。

    输入：
        action: AuditAction 常量。
        target_type: AuditTargetType 成员或字符串值。
        target_id_arg: 用于取 target_id 的参数名；None 表示不记录 target_id。
        before_fn: 可选，签名 (args, kwargs) -> dict，在调用前捕获 before 快照。
        after_fn: 可选，签名 (args, kwargs) -> dict，仅在成功路径捕获 after 快照。

    行为：
        - 成功：执行函数 → 调用 after_fn → 写 SUCCESS（含 before/after）→ 返回原值。
        - 异常：写 FAIL（仅含 before，不调用 after_fn——失败态 after 不可信）→ re-raise。

    适用场景：
        纯服务函数（如 grant_role、move_node）的留痕；含复杂多步分支的逻辑请用
        AuditContext 手动控制快照时机。
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 捕获 actor：第一参数为 User 或 request（取 .user）。匿名用户不计。
            actor = None
            if args:
                first = args[0]
                if isinstance(first, User):
                    actor = first
                else:
                    maybe_user = getattr(first, 'user', None)
                    if isinstance(maybe_user, User):
                        actor = maybe_user

            # 按签名绑定取 target_id，兼容位置/关键字两种调用方式。
            target_id = None
            if target_id_arg:
                try:
                    bound = inspect.signature(func).bind(*args, **kwargs)
                    if target_id_arg in bound.arguments:
                        target_id = bound.arguments[target_id_arg]
                except TypeError:
                    # 调用参数不匹配签名，交给下方真实调用抛出原生异常；
                    # target_id 留空，FAIL 审计仍会写入。
                    target_id = None

            # before 快照在函数执行前捕获（此时数据尚未被改动）。
            before = _safe_snapshot(before_fn, args, kwargs)

            try:
                ret = func(*args, **kwargs)
            except Exception:
                # 失败路径：after 态不可信，仅用 before 写 FAIL，随后 re-raise
                # 保持业务异常原语义不变（审批/权限校验等依赖具体异常类型）。
                write_audit(
                    actor=actor, action=action, target_type=target_type,
                    target_id=target_id, before=before, result='FAIL',
                )
                raise

            # 成功路径：捕获 after 快照并写 SUCCESS。
            after = _safe_snapshot(after_fn, args, kwargs)
            write_audit(
                actor=actor, action=action, target_type=target_type,
                target_id=target_id, before=before, after=after, result='SUCCESS',
            )
            return ret

        return wrapper

    return decorator


def _safe_snapshot(fn, args, kwargs):
    """安全执行快照回调：返回 dict 或 None

    快照回调由调用方提供，可能因业务对象状态异常而抛错。为避免快照逻辑缺陷
    影响主业务或污染审计写入，此处统一兜底：失败仅记日志并返回 None。
    """
    if not callable(fn):
        return None
    try:
        snapshot = fn(args, kwargs)
        return snapshot if isinstance(snapshot, dict) else None
    except Exception as exc:  # noqa: BLE001 —— 快照回调兜底
        logger.warning('[Audit] snapshot fn failed (忽略，快照置空): err={}', exc)
        return None


# ============================================================================
# 上下文管理器：对复杂逻辑块留痕
# ============================================================================
class AuditContext:
    """权限审计上下文管理器：在 with 块内手动控制快照时机

    业务背景：
        部分权限操作含多步分支与条件赋值，无法用装饰器一刀切捕获快照
        （如工单执行：先写授权表、再发通知、再回写状态）。本上下文管理器允许
        在块内按需 set_before/set_after，退出时统一落盘，保证复杂流程同样留痕。

    用法：
        with AuditContext(actor, AuditAction.NODE_MOVE, AuditTargetType.KNOWLEDGE_NODE,
                          target_id=node_id) as ctx:
            ctx.set_before({'parent': old_parent})
            node.parent = new_parent
            node.save()
            ctx.set_after({'parent': new_parent})

    行为：
        - 正常退出：写 SUCCESS（含已设置的 before/after）。
        - 异常退出：写 FAIL（仅含已设置的 before）并 re-raise（return False 不吞异常）。

    适用场景：
        多步事务、条件分支、需运行时动态决定 after 内容的操作块。
    """

    def __init__(self, actor, action, target_type, target_id=None, target_user=None,
                 role=None, scope_type=ScopeType.NONE, scope_id=None,
                 ip_address=None, user_agent=''):
        # 仅暂存参数与快照，真正的写库延迟到 __exit__，确保块内异常也能被审计。
        self._actor = actor
        self._action = action
        self._target_type = target_type
        self._target_id = target_id
        self._target_user = target_user
        self._role = role
        self._scope_type = scope_type
        self._scope_id = scope_id
        self._ip_address = ip_address
        self._user_agent = user_agent
        self._before = None
        self._after = None

    def set_before(self, snapshot):
        """设置变更前快照；返回 self 以支持链式调用"""
        if isinstance(snapshot, dict):
            self._before = snapshot
        return self

    def set_after(self, snapshot):
        """设置变更后快照；返回 self 以支持链式调用"""
        if isinstance(snapshot, dict):
            self._after = snapshot
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # exc_type 为 None 表示正常退出→SUCCESS；否则异常退出→FAIL。
        result = 'SUCCESS' if exc_type is None else 'FAIL'
        write_audit(
            actor=self._actor, action=self._action, target_type=self._target_type,
            target_id=self._target_id, target_user=self._target_user, role=self._role,
            scope_type=self._scope_type, scope_id=self._scope_id,
            before=self._before, after=self._after, result=result,
            ip_address=self._ip_address, user_agent=self._user_agent,
        )
        # 返回 False 不吞异常：业务异常必须按原语义向上传播，审计只做旁路记录。
        return False


# ============================================================================
# 辅助函数：从 request 提取审计所需元数据
# ============================================================================
def extract_request_meta(request):
    """从 DRF/Django request 提取 (ip_address, user_agent)

    功能：
        统一解析客户端 IP 与 UA，供 write_audit/audit_action 使用，避免各视图
        重复实现且口径不一致。

    IP 解析规则：
        优先取 X-Forwarded-For 第一个值（反向代理场景下的真实客户端 IP）；
        无则回退到 REMOTE_ADDR。取不到返回 None（适配 GenericIPAddressField 可空）。

    UA 解析规则：
        取 HTTP_USER_AGENT 并截断至 512 字符（对齐 PermissionAuditLog.user_agent
        字段 max_length=512，防止超长 UA 写入失败）。

    输入：DRF Request 或 Django HttpRequest。

    输出：元组 (ip_address, user_agent)；ip_address 为 None 或合法 IP 串，
          user_agent 为 str（可能为空串）。
    """
    # X-Forwarded-For 形如 "client, proxy1, proxy2"，取首个即最原始客户端。
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        ip = xff.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR', '')
    # 空串归一为 None，避免 GenericIPAddressField 校验失败。
    ip = ip or None

    user_agent = request.META.get('HTTP_USER_AGENT', '')[:512]
    return ip, user_agent
