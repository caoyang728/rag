"""users app 公共工具函数（跨视图 / 服务模块共享）"""
import csv
import io

from django.http import HttpResponse


def _client_ip(request):
    """获取客户端真实 IP：优先取 X-Forwarded-For 首段，兜底 REMOTE_ADDR

    返回 None 表示未获取到 IP（对齐 GenericIPAddressField 可空语义，避免空字符串写入失败）。
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        ip = xff.split(",")[0].strip()
        if ip:
            return ip
    return request.META.get("REMOTE_ADDR") or None


# UA 截断上限（统一常量，与 PermissionAuditLog.user_agent max_length=512 对齐）
_MAX_UA_LENGTH = 512


def _client_ua(request):
    """获取客户端 User-Agent 并截断，防止超长 UA 写入数据库"""
    return request.META.get("HTTP_USER_AGENT", "")[:_MAX_UA_LENGTH]


def _first_serializer_error(errors):
    """从 DRF Serializer.errors 提取第一条错误信息（嵌套结构递归展平）

    返回 (field_name, detail_str)。视图据此返回 {'detail': ...}，
    保持接口"单错误 + 400"的响应契约不变（含 ListField 子项校验失败场景）。
    """
    def _flatten(e):
        # 递归展平嵌套 dict/list（如 {'permission_ids': [{0: [ErrorDetail]}]}）
        if isinstance(e, dict):
            for v in e.values():
                r = _flatten(v)
                if r:
                    return r
            return ''
        if isinstance(e, (list, tuple)):
            for v in e:
                r = _flatten(v)
                if r:
                    return r
            return ''
        return str(e)

    for field, err in errors.items():
        detail = _flatten(err)
        if detail:
            return field, detail
    return '', '参数校验失败'


def _serialize_chain_nodes(chain):
    """序列化审批链节点 —— 批量解析 approver_id → approver_name, 供前端展示"谁批准的"

    性能优化:收集所有 approver_id 后一次查询,避免 N+1。
    供权限申请列表与统一工单中心共用。
    """
    from apps.users.models import User
    ids = {n.get('approver_id') for n in chain if n.get('approver_id')}
    user_map = {}
    if ids:
        user_map = {
            u.id: (u.real_name or u.username)
            for u in User.objects.filter(id__in=ids).only('id', 'real_name', 'username')
        }
    return [
        {
            'approver_role': n.get('approver_role'),
            'approver_id': n.get('approver_id'),
            'approver_name': user_map.get(n.get('approver_id'), ''),
            'status': n.get('status'),
            'comment': n.get('comment', ''),
            'approved_at': n.get('approved_at', ''),
        } for n in chain
    ]


def _resolve_scope_name(scope_type, scope_id, dept_map=None, team_map=None):
    """根据 scope_type + scope_id 解析管辖范围的显示名称

    dept_map/team_map 为批量预加载的 {id: name} 字典（列表接口优化），
    未传入时回退到 DB 查询（详情/预览等低频场景）。
    供 views_tickets / views_permissions / access_service 共用。
    """
    from apps.users.models import ScopeType as _ST
    if scope_type == _ST.DEPT and scope_id:
        if dept_map:
            return dept_map.get(scope_id) or f'部门#{scope_id}'
        from apps.users.models import Department
        dept = Department.objects.filter(id=scope_id, is_deleted=False).only('name').first()
        return dept.name if dept else f'部门#{scope_id}'
    if scope_type == _ST.TEAM and scope_id:
        if team_map:
            return team_map.get(scope_id) or f'团队#{scope_id}'
        from apps.users.models import Team
        team = Team.objects.filter(id=scope_id, is_deleted=False).only('name').first()
        return team.name if team else f'团队#{scope_id}'
    if scope_type in (_ST.GLOBAL, _ST.NONE):
        return '全局'
    return ''


def _sanitize_csv_field(value):
    """防止 CSV 注入：如果字段值以公式触发字符开头，加单引号前缀

    Excel 等表格软件会将以 =、+、-、@、Tab、回车 开头的单元格解析为公式，
    攻击者可通过在用户名/姓名等字段注入恶意公式实现远程代码执行或信息窃取。
    """
    if isinstance(value, str) and value and value[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value


def _safe_filename(filename):
    """对文件名做安全处理，防止 HTTP 头注入（换行/双引号等特殊字符）"""
    from django.utils.encoding import smart_str
    safe = smart_str(filename).replace('"', '').replace('\n', '').replace('\r', '')
    return safe


def _export_users_csv(users_qs, filename="users_export.csv"):
    """将用户 QuerySet 导出为 UTF-8 BOM CSV（Excel 中文兼容）"""
    buf = io.StringIO()
    buf.write('\ufeff')  # BOM for Excel Chinese support
    writer = csv.writer(buf)
    writer.writerow(["用户名", "邮箱", "真实姓名", "部门", "团队", "状态", "最后登录", "创建时间"])
    for u in users_qs.select_related('department', 'team'):
        # 单团队 FK：user.team 指向唯一团队
        team_names = u.team.name if u.team and not u.team.is_deleted else ''
        writer.writerow([
            _sanitize_csv_field(u.username),
            _sanitize_csv_field(u.email),
            _sanitize_csv_field(u.real_name),
            _sanitize_csv_field(u.department.name if u.department else ""),
            _sanitize_csv_field(team_names),
            u.get_status_display() if hasattr(u, "get_status_display") else u.status,
            u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "",
            u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
        ])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{_safe_filename(filename)}"'
    return resp
