"""
system views
- GET  /api/v1/system/health/   健康检查（含 DB / Redis / LLM ping）
- GET  /api/v1/system/configs/  系统配置列表
- PUT  /api/v1/system/configs/<key>/  创建变更工单（不再直接改配置）
- GET  /api/v1/system/stats/    简易看板：文档数/QA数/用户数
- CRUD /api/v1/system/llm-models/  LLM/Embedding/Rerank 模型配置管理
- /api/v1/system/config-tickets/  配置变更工单（创建/审批/驳回/撤回）
- GET  /api/v1/system/tickets/  统一工单列表（合并 config/schedule/model 工单）
"""
import json
import time

from loguru import logger

from django.db import connection, transaction
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.system.models import SystemConfig, LLMModel, Ticket
from apps.system.scheduler_registry import (
    compute_schedule_change_summary,
    is_schedule_key,
    normalize_schedule_value,
)

User = get_user_model()

# 已废弃的配置 key 集合：这些配置项在 YAML 定义中已移除，
# 但 DB 中可能残留历史记录，需在 API 层过滤掉，避免前端展示无效项。
# 如需彻底清理，可在 Django shell 中执行:
#   SystemConfig.objects.filter(key__in=DEPRECATED_CONFIG_KEYS).delete()
DEPRECATED_CONFIG_KEYS = frozenset({
    'PRODUCTION_EVAL_HOURLY_GUARANTEE',  # 保底机制已废弃：无对话则无法评估
    'PRODUCTION_EVAL_DAILY_GUARANTEE',
})


class HealthView(APIView):
    """GET /api/v1/system/health/  组件健康检查"""
    permission_classes = [AllowAny]

    def get(self, request):
        result = {"service": "rag-agent-backend", "ok": True, "components": {}}

        # DB
        t0 = time.time()
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            result["components"]["db"] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            result["ok"] = False
            result["components"]["db"] = {"ok": False, "error": str(e)}

        # Redis
        try:
            import redis
            from django.conf import settings
            r = redis.Redis.from_url(getattr(settings, "REDIS_URL",
                                             "redis://localhost:6379/0"))
            t0 = time.time()
            r.ping()
            result["components"]["redis"] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            result["components"]["redis"] = {"ok": False, "error": str(e)[:120]}

        # LLM
        try:
            from apps.llm.factory import get_llm
            llm = get_llm()
            result["components"]["llm"] = {"ok": True, "provider": getattr(llm, "provider", "unknown")}
        except Exception as e:
            result["components"]["llm"] = {"ok": False, "error": str(e)[:120]}

        return Response(result)


class SystemConfigView(APIView):
    """GET/PUT /api/v1/system/configs/  or /api/v1/system/configs/<key>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, key=None):
        if key:
            # 废弃的配置项直接返回 404，不对外暴露
            if key in DEPRECATED_CONFIG_KEYS:
                return Response({"detail": "not found"}, status=404)
            try:
                c = SystemConfig.objects.get(key=key)
            except SystemConfig.DoesNotExist:
                return Response({"detail": "not found"}, status=404)
            return Response(self._ser(c))
        # 列表按 category 分组返回，方便前端按 tab 渲染
        # 调度类配置（SCHEDULE_*）由独立"定时任务"页面管理，不出现在通用配置列表，
        # 避免以 JSON 文本形式暴露在配置页造成重复入口与误编辑
        rows = [r for r in SystemConfig.objects.all().order_by("category", "key")
                if r.key not in DEPRECATED_CONFIG_KEYS and not is_schedule_key(r.key)]

        # 模型 options 一次查出全部启用模型，按 model_type 预分组，避免 5 次独立 DB 查询
        llm_options_map = self._get_llm_model_options_map()
        business_tables = None  # 懒加载：只在遇到 BUSINESS_DB_TABLES 项时查询一次

        groups = {}
        for c in rows:
            item = self._ser(c)
            # BUSINESS_DB_TABLES 动态填充业务数据库的表名列表作为 options
            if c.key == 'BUSINESS_DB_TABLES':
                if business_tables is None:
                    business_tables = self._get_business_tables()
                item['options'] = business_tables
            # 模型选择类配置项：直接用预分组的 options 映射，避免重复查库
            elif c.key in ('LLM_BASE_MODEL', 'LLM_ADVANCED_MODEL', 'EVAL_MODEL'):
                item['options'] = llm_options_map.get('llm', [])
                if c.value and not any(o['value'] == c.value for o in item['options']):
                    item['options'].insert(0, {"value": c.value, "label": f"{c.value} (未在模型管理中)"})
                item['description'] = '从模型管理中选择 LLM 模型'
            elif c.key == 'EMBEDDING_MODEL':
                item['options'] = llm_options_map.get('embedding', [])
                if c.value and not any(o['value'] == c.value for o in item['options']):
                    item['options'].insert(0, {"value": c.value, "label": f"{c.value} (未在模型管理中)"})
                item['description'] = '从模型管理中选择 Embedding 模型'
            elif c.key == 'RERANK_MODEL':
                item['options'] = llm_options_map.get('rerank', [])
                if c.value and not any(o['value'] == c.value for o in item['options']):
                    item['options'].insert(0, {"value": c.value, "label": f"{c.value} (未在模型管理中)"})
                item['description'] = '从模型管理中选择 Rerank 模型'
            groups.setdefault(c.category, []).append(item)
        return Response({"groups": groups, "total": len(rows)})

    def put(self, request, key=None):
        """PUT /api/v1/system/configs/<key>/  创建配置变更工单

        配置修改不再直接落库，而是创建一份 Ticket（ticket_type='config'）等待审批：
        - 普通项：审核通过后生效
        - 高风险项：审核 + 超管复核通过后生效
        这样可以避免单人误改造成线上故障，并保留完整审批链路用于审计追溯。
        """
        # 权限：超级管理员或持有 system.config.write 权限（维护管理员）
        if not request.user.has_perm('system.config.write'):
            return Response({"detail": "仅超级管理员或维护管理员可修改系统配置"}, status=403)
        # 废弃的配置项禁止修改
        if key and key in DEPRECATED_CONFIG_KEYS:
            return Response({"detail": f"配置项 {key} 已废弃，不可修改"}, status=400)
        if not key:
            return Response({"detail": "key required"}, status=400)
        try:
            obj = SystemConfig.objects.get(key=key)
        except SystemConfig.DoesNotExist:
            return Response({"detail": f"配置项 {key} 不存在"}, status=404)

        # 只读项禁止提交工单（如 EMBEDDING_DIM 改了需重建索引），只能改 .env 重启
        if obj.is_readonly:
            return Response({"detail": f"配置项 {key} 为只读项，需在 .env 中修改后重启生效"}, status=409)

        old_value = obj.value
        value = request.data.get("value", "")
        # value_type 是元数据，由开发者维护，前端不可修改（防止类型混乱）
        # 按类型规范化存储，避免前端传入不规范格式
        try:
            new_value = self._normalize_value(value, obj.value_type)
        except ValueError as e:
            logger.warning(f"SystemConfig.put normalize failed key={key}: {e}")
            return Response({"detail": str(e)}, status=400)
        # 调度类配置（SCHEDULE_*）：额外校验 cron 语法并规范化为统一存储格式，
        # 保证固定键序，避免同一调度因 JSON 键序差异被误判为新变更
        if is_schedule_key(obj.key):
            try:
                new_value = normalize_schedule_value(new_value)
            except ValueError as e:
                logger.warning(f"SystemConfig.put schedule validate failed key={key}: {e}")
                return Response({"detail": str(e)}, status=400)
        # 新值与原值一致则无需创建工单，避免无效审批占位
        if new_value == old_value:
            return Response({"detail": "新值与当前值一致，无需提交工单"}, status=400)

        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "请填写变更原因"}, status=400)

        # 创建工单（pending 状态），冗余 risk_level 避免后续 SystemConfig 改动影响本工单审批流程
        # 多值类配置（如 BUSINESS_DB_TABLES）额外计算差异摘要，便于审批人快速识别变更点
        change_summary = self._compute_change_summary(obj.key, old_value, new_value)
        # 类型特有数据（config_label/old_value/new_value/change_summary）统一存入 detail JSON
        ticket = Ticket.objects.create(
            ticket_type='config',
            operation='modify',
            status='pending',
            risk_level=obj.risk_level,
            reason=reason,
            creator=request.user if request.user.is_authenticated else None,
            config_key=obj.key,
            detail={
                'config_label': obj.label,
                'old_value': old_value,
                'new_value': new_value,
                'change_summary': change_summary,
            },
        )

        # 写审计日志：记录工单创建动作，secret 项的值掩码防止泄露
        self._write_ticket_audit(
            request, action='create_config_ticket', ticket=ticket,
            extra={'risk_level': ticket.risk_level, 'reason': reason},
        )
        logger.info(f"配置变更工单已创建: ticket={ticket.id} key={key} by {request.user.username}")

        return Response(self._serialize_ticket(ticket), status=status.HTTP_201_CREATED)

    def _compute_change_summary(self, config_key, old_value, new_value):
        """计算多值类配置的差异摘要（仅 BUSINESS_DB_TABLES / EVAL_DISPLAY_DIMENSIONS 等逗号分隔多值项）

        Returns: JSON 字符串 {added:[...], removed:[...]}；非多值项返回空串
        - added: 新值中存在但旧值中不存在的项
        - removed: 旧值中存在但新值中不存在的项
        审批人据此快速识别本次变更点，无需逐项对比新旧完整列表
        """
        # 调度类配置：单独计算 cron/启停的变更摘要，便于审批人识别改了什么
        if is_schedule_key(config_key):
            return compute_schedule_change_summary(old_value, new_value)
        # 仅多值类配置计算差异；单值配置（如 LLM_TIMEOUT）返回空，避免噪声
        # EVAL_DISPLAY_DIMENSIONS 也按多值处理，便于审批人快速识别新增/移除了哪些维度
        multi_value_keys = {'BUSINESS_DB_TABLES', 'EVAL_DISPLAY_DIMENSIONS'}
        if config_key not in multi_value_keys:
            return ''
        try:
            old_set = {x.strip() for x in (old_value or '').split(',') if x.strip()}
            new_set = {x.strip() for x in (new_value or '').split(',') if x.strip()}
            added = sorted(new_set - old_set)
            removed = sorted(old_set - new_set)
            return json.dumps({'added': added, 'removed': removed}, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"计算变更摘要失败 key={config_key}: {e}")
            return ''

    def _normalize_value(self, value, value_type):
        """按 value_type 规范化为字符串存储

        对 int / float 类型增加校验：
        - int：必须为整数，否则抛出 ValueError（前端虽有 min/step 限制，后端仍需兜底）
        - float：必须为数字，允许小数
        """
        if value_type == 'int':
            # 整数校验：拒绝小数和非数字
            try:
                s = str(value).strip()
                # 允许 "3.0" 这类格式，但拒绝 "3.5"
                f = float(s)
                if f != int(f):
                    raise ValueError(f'配置项需要整数，收到小数 {value}')
                return str(int(f))
            except (ValueError, TypeError) as e:
                if '配置项需要整数' in str(e):
                    raise
                raise ValueError(f'配置项需要整数类型，收到: {value}')
        if value_type == 'float':
            try:
                f = float(str(value).strip())
                return str(f)
            except (ValueError, TypeError):
                raise ValueError(f'配置项需要数字类型，收到: {value}')
        if value_type == 'bool':
            if isinstance(value, bool):
                return 'true' if value else 'false'
            return 'true' if str(value).lower() in ('1', 'true', 'yes', 'on') else 'false'
        if value_type == 'json':
            if isinstance(value, str):
                return value
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _write_ticket_audit(self, request, action, ticket, extra=None):
        """写工单相关审计日志：敏感项的 old/new 值掩码，避免明文落库

        action 取值：create_config_ticket / approve_config_ticket /
        reject_config_ticket / withdraw_config_ticket / apply_config_ticket
        """
        try:
            from apps.audit.models import AuditLog
            # 仅当配置项本身被标记为 secret 时才掩码（工单 old/new 可能含敏感值）
            cfg = SystemConfig.objects.filter(key=ticket.config_key).first()
            is_secret = bool(cfg and cfg.is_secret)
            # 新 Ticket 模型的 old/new 值存放在 detail JSON 中
            td = ticket.detail if isinstance(ticket.detail, dict) else {}
            display_old = '***' if is_secret else td.get('old_value', '')
            display_new = '***' if is_secret else td.get('new_value', '')
            detail = {
                'ticket_id': ticket.id,
                'config_key': ticket.config_key,
                'old': display_old,
                'new': display_new,
                'risk_level': ticket.risk_level,
                'status': ticket.status,
            }
            if extra:
                detail.update(extra)
            AuditLog.objects.create(
                actor=request.user,
                actor_username=request.user.username,
                action=action,
                action_category='config',
                target_type='config_ticket',
                target_id=str(ticket.id),
                result='success',
                detail=detail,
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method=request.method,
                path=request.path,
            )
        except Exception as e:
            # 审计日志写失败不阻断业务流程，仅记录警告便于运维排查
            logger.warning(f"写配置工单审计日志失败: {e}")

    def _serialize_ticket(self, t):
        """序列化工单：敏感项的 old/new 值掩码，避免泄露给前端

        新 Ticket 模型将 config_label/old_value/new_value/change_summary 存放在 detail JSON 中，
        公共字段（ticket_type/operation/status/risk_level/creator 等）直接作为模型字段。
        """
        cfg = SystemConfig.objects.filter(key=t.config_key).first()
        is_secret = bool(cfg and cfg.is_secret)
        # 从 detail JSON 中提取类型特有字段，兼容 detail 为空或非 dict 的情况
        td = t.detail if isinstance(t.detail, dict) else {}
        display_old = '***' if is_secret else td.get('old_value', '')
        display_new = '***' if is_secret else td.get('new_value', '')
        # change_summary 存储为 JSON 字符串，解析后返回前端便于直接渲染
        # 解析失败时返回 None，前端按"无差异摘要"处理（不影响审批流程）
        change_summary = None
        cs_raw = td.get('change_summary', '')
        if cs_raw:
            try:
                change_summary = json.loads(cs_raw) if isinstance(cs_raw, str) else cs_raw
            except (json.JSONDecodeError, TypeError):
                change_summary = None
        return {
            "id": t.id,
            "ticket_type": t.ticket_type,
            "operation": t.operation,
            "config_key": t.config_key,
            "config_label": td.get('config_label', ''),
            "old_value": display_old,
            "new_value": display_new,
            "risk_level": t.risk_level,
            "reason": t.reason,
            "change_summary": change_summary,
            "status": t.status,
            "creator": t.creator.username if t.creator else '',
            "auditor": t.auditor.username if t.auditor else '',
            "reviewer": t.reviewer.username if t.reviewer else '',
            "audit_comment": t.audit_comment,
            "review_comment": t.review_comment,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "audited_at": t.audited_at.isoformat() if t.audited_at else None,
            "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
            "applied_at": t.applied_at.isoformat() if t.applied_at else None,
        }

    def _write_audit(self, request, obj, old_value, new_value):
        """写审计日志：记录配置修改前后的值（secret 项掩码）"""
        try:
            from apps.audit.models import AuditLog
            display_old = '***' if obj.is_secret else old_value
            display_new = '***' if obj.is_secret else new_value
            AuditLog.objects.create(
                actor=request.user,
                actor_username=request.user.username,
                action='update_system_config',
                action_category='config',
                target_type='system_config',
                target_id=obj.key,
                result='success',
                detail={'old': display_old, 'new': display_new, 'value_type': obj.value_type},
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method='PUT',
                path=request.path,
            )
        except Exception as e:
            logger.warning(f"写系统配置审计日志失败: {e}")

    def _get_llm_model_options(self, model_type, current_value=''):
        """从 LLMModel 表读取指定类型的启用模型作为 select options

        Args:
            model_type: 'llm' / 'embedding' / 'rerank'
            current_value: 当前配置值，不在可选列表中时追加进去（保证下拉框能回显当前值）
        Returns:
            [{"value": "deepseek-chat", "label": "DeepSeek 对话 (deepseek-chat)"}, ...]
        """
        from .models import LLMModel
        try:
            models = LLMModel.objects.filter(model_type=model_type, is_active=True).order_by('name')
            options = [{"value": m.model_name, "label": f"{m.name} ({m.model_name})"} for m in models]
            # 当前值不在选项中时追加，保证下拉框能回显
            if current_value and not any(o['value'] == current_value for o in options):
                options.insert(0, {"value": current_value, "label": f"{current_value} (未在模型管理中)"})
            return options
        except Exception as e:
            logger.warning(f"读取模型配置失败: {e}")
            return []

    def _get_llm_model_options_map(self):
        """一次性读取全部启用模型并按 model_type 分组，用于列表页批量渲染 options

        与 _get_llm_model_options 行为等价但只做一次 DB 查询，
        避免 5 个模型选择配置项触发 5 次独立查询。
        """
        from .models import LLMModel
        try:
            models = LLMModel.objects.filter(is_active=True).order_by('model_type', 'name')
            result = {'llm': [], 'embedding': [], 'rerank': []}
            for m in models:
                bucket = result.setdefault(m.model_type, [])
                bucket.append({"value": m.model_name, "label": f"{m.name} ({m.model_name})"})
            return result
        except Exception as e:
            logger.warning(f"批量读取模型配置失败: {e}")
            return {'llm': [], 'embedding': [], 'rerank': []}

    def _get_business_tables(self):
        """从业务数据库读取 public schema 的表名列表

        优先用 BUSINESS_DB_DSN 直连（psycopg2），未配置时回退到 django 默认数据库。
        读取失败时返回空列表（前端降级为自由输入）。
        """
        from django.conf import settings
        try:
            dsn = getattr(settings, 'BUSINESS_DB_DSN', '') or ''
            if dsn:
                import psycopg2
                conn = psycopg2.connect(dsn)
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                    )
                    tables = [row[0] for row in cursor.fetchall()]
                finally:
                    conn.close()
            else:
                from django.db import connection
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                    )
                    tables = [row[0] for row in cursor.fetchall()]
            return [{"value": t, "label": t} for t in tables]
        except Exception as e:
            logger.warning(f"读取业务数据库表名失败: {e}")
            return []

    def _ser(self, c):
        # options 是 JSON 字符串，解析为数组返回给前端；解析失败返回空数组
        try:
            options = json.loads(c.options) if c.options else []
        except (json.JSONDecodeError, TypeError):
            options = []
        return {"key": c.key, "value": c.value if not c.is_secret else "***",
                "value_type": c.value_type, "label": c.label,
                "description": c.description, "unit": c.unit, "options": options,
                "is_secret": c.is_secret, "is_readonly": c.is_readonly,
                "risk_level": c.risk_level,
                "category": c.category,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None}


class SchedulerTaskView(APIView):
    """GET /api/v1/system/scheduler/tasks/  定时任务调度配置列表

    返回任务清单 + 当前调度值（cron 分字段 + 启停状态）+ 待审批工单数，
    供管理端"定时任务"页面渲染。
    修改调度时间 / 启停统一走 PUT /configs/<key>/ 工单审批流程（高风险需复核），
    审批通过后由 SystemConfigScheduler 热更新，无需重启 beat。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # 查看需 system.config.read 权限（维护管理员 / 超管），与配置页对齐
        if not request.user.has_perm('system.config.read'):
            return Response({"detail": "无权限查看定时任务配置"}, status=status.HTTP_403_FORBIDDEN)
        from .scheduler_registry import get_tasks_meta
        tasks = get_tasks_meta()
        return Response({"tasks": tasks, "total": len(tasks)})


class LLMModelViewSet(viewsets.ModelViewSet):
    """LLM/Embedding/Rerank 模型配置 CRUD

    权限模型与 SystemConfigView 对齐：
    - 查看（list/retrieve）需 system.config.read
    - 修改（create/update/destroy）需 system.config.write
    super_admin 在 User.has_perm 中走快路径直接放行，无需逐角色判断。

    审计：create/update/destroy 均写 AuditLog（action=manage_llm_model，
    action_category=config），便于在审计页面按 config 类目追溯模型变更。
    """
    queryset = LLMModel.objects.all().order_by('model_type', 'name')
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        # 查看走读权限、写操作走写权限；与 SystemConfigView 的权限切分保持一致，
        # 让维护管理员既能浏览配置也能管理模型，普通管理员无写权限
        return [IsAuthenticated()]

    def _check_perm(self, request, perm):
        """统一权限校验：复用 User.has_perm（super_admin 快路径 + RBAC 权限点判定）"""
        if not request.user.has_perm(perm):
            return Response(
                {"detail": "无权限执行此操作，需要 " + perm + " 权限"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _ser(self, m):
        """单条模型序列化：时间字段做空值保护，避免前端渲染 NaN"""
        return {
            'id': m.id,
            'name': m.name,
            'provider': m.provider,
            'model_type': m.model_type,
            'base_url': m.base_url,
            'model_name': m.model_name,
            'timeout': m.timeout,
            'is_active': m.is_active,
            'created_at': m.created_at.isoformat() if m.created_at else None,
            'updated_at': m.updated_at.isoformat() if m.updated_at else None,
        }

    def list(self, request, *args, **kwargs):
        # 权限：查看需要读权限（system_maintainer / super_admin）
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied
        # 按 model_type 分组返回，前端按 LLM / Embedding / Rerank 三个 tab 渲染
        rows = list(self.get_queryset())
        # 批量查询每个模型的待审批工单数和依赖引用
        model_ids = [m.id for m in rows]
        pending_tickets = {}
        if model_ids:
            from django.db.models import Count
            ticket_qs = Ticket.objects.filter(
                ticket_type='model',
                target_model_id__in=model_ids,
                status__in=['pending', 'pending_review']
            ).values('target_model_id').annotate(cnt=Count('id'))
            pending_tickets = {t['target_model_id']: t['cnt'] for t in ticket_qs}

        SYSTEM_CONFIG_KEYS_REFERENCING_MODELS = [
            'LLM_BASE_MODEL', 'LLM_ADVANCED_MODEL', 'EVAL_MODEL',
            'EMBEDDING_MODEL', 'RERANK_MODEL',
        ]
        model_names = [m.model_name for m in rows]
        dep_counts = {}
        if model_names:
            from django.db.models import Count
            dep_qs = SystemConfig.objects.filter(
                key__in=SYSTEM_CONFIG_KEYS_REFERENCING_MODELS,
                value__in=model_names,
            ).values('value').annotate(cnt=Count('id'))
            dep_counts = {d['value']: d['cnt'] for d in dep_qs}

        groups = {}
        for m in rows:
            item = self._ser(m)
            item['pending_ticket_count'] = pending_tickets.get(m.id, 0)
            item['dependency_count'] = dep_counts.get(m.model_name, 0)
            groups.setdefault(m.model_type, []).append(item)
        return Response({'groups': groups, 'total': len(rows)})

    def retrieve(self, request, *args, **kwargs):
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied
        obj = self.get_object()
        return Response(self._ser(obj))

    def _validate_model_payload(self, data: dict, is_create: bool):
        """LLMModel 创建/更新的统一字段校验

        返回: (error_response, normalized_dict)；前者为 None 表示通过
        - base_url：非空时必须以 http:// 或 https:// 开头，禁止 file:// 等危险协议
        - timeout：非空时必须为 >= 1 的正整数（0 或负数语义上无意义）
        - 字符串字段：去前后空白，避免同值因空格不同触发唯一冲突或比较不一致
        """
        errors = {}
        out = {}

        required = ('name', 'provider', 'model_type', 'model_name') if is_create else ()
        for f in required:
            if not data.get(f):
                errors[f] = '必填'

        if 'model_type' in data and data.get('model_type') not in dict(LLMModel.MODEL_TYPE_CHOICES):
            errors['model_type'] = '取值非法'

        # base_url 协议白名单：允许空（表示走默认）或 http/https，防止 SSRF 指向本地协议
        base_url = str(data.get('base_url') or '').strip()
        if base_url:
            lowered = base_url.lower()
            if not (lowered.startswith('http://') or lowered.startswith('https://')):
                errors['base_url'] = '必须以 http:// 或 https:// 开头'
            elif len(base_url) > 1000:
                errors['base_url'] = '长度不能超过 1000 字符'
        out['base_url'] = base_url

        # timeout：None 表示用全局默认，数值则必须是正整数（>=1 秒）
        timeout_raw = data.get('timeout')
        if timeout_raw in (None, '', 'null'):
            out['timeout'] = None
        else:
            try:
                timeout_int = int(timeout_raw)
                if timeout_int < 1:
                    errors['timeout'] = '必须 >= 1 秒'
                elif timeout_int > 86400:
                    errors['timeout'] = '不能超过 86400 秒（1 天）'
                else:
                    out['timeout'] = timeout_int
            except (ValueError, TypeError):
                errors['timeout'] = '必须是整数'

        for str_field in ('name', 'provider', 'model_name'):
            if str_field in data:
                val = str(data[str_field] or '').strip()
                if len(val) > 255:
                    errors[str_field] = '长度不能超过 255'
                out[str_field] = val

        if 'is_active' in data:
            out['is_active'] = bool(data.get('is_active', True))

        if errors:
            return Response({'detail': '字段校验失败', 'errors': errors},
                            status=status.HTTP_400_BAD_REQUEST), None
        return None, out

    def create(self, request, *args, **kwargs):
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        data = request.data or {}
        # 统一字段校验 + 规范化（去空白、协议限制、数值范围）
        err, norm = self._validate_model_payload(data, is_create=True)
        if err:
            return err
        try:
            obj = LLMModel.objects.create(
                name=norm['name'],
                provider=norm['provider'],
                model_type=data['model_type'],
                base_url=norm['base_url'],
                model_name=norm['model_name'],
                timeout=norm['timeout'],
                is_active=norm.get('is_active', True),
            )
        except Exception as e:
            # 记录完整错误日志便于排查，前端只返回简要原因
            logger.error(f"LLMModel.create failed by {request.user.username}: {e}")
            return Response({"detail": "创建失败，请检查模型名称是否已存在"}, status=status.HTTP_400_BAD_REQUEST)
        # 新增模型可能被同名调用方命中，全清 LLMModel 缓存避免脏读
        self._invalidate_llm_cache()
        self._write_audit(request, 'create', obj, None, self._ser(obj))
        logger.info(f"LLMModel.create by {request.user.username}: {obj}")
        return Response(self._ser(obj), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """模型更新：分级审批

        - 修改 name(显示名)：无需审批，直接生效（仅影响前端展示）
        - 修改其他字段(base_url/model_name/provider/timeout/model_type)：创建工单走普通审批
        - 停用(is_active=False)：创建工单走普通审批 + 检查依赖
        """
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        obj = self.get_object()
        data = request.data or {}
        before = self._ser(obj)
        # 统一字段校验 + 规范化
        err, norm = self._validate_model_payload(data, is_create=False)
        if err:
            return err

        # 判断是否仅修改 name 字段
        only_name_changed = (
            'name' in norm and len(norm) == 1 and
            all(k not in norm for k in ('provider', 'model_type', 'base_url', 'model_name', 'timeout', 'is_active'))
        )

        if only_name_changed:
            # 修改显示名：无需审批，直接生效
            obj.name = norm['name']
            try:
                obj.save()
            except Exception as e:
                logger.error(f"LLMModel.update(name) failed id={obj.id} by {request.user.username}: {e}")
                return Response({"detail": "更新失败，请检查模型名称是否已存在"}, status=status.HTTP_400_BAD_REQUEST)
            self._invalidate_llm_cache()
            self._write_audit(request, 'update_name', obj, before, self._ser(obj))
            logger.info(f"LLMModel.update(name) by {request.user.username}: {obj}")
            return Response(self._ser(obj))

        # 停用场景：检查依赖
        deactivating = 'is_active' in norm and not norm['is_active']
        if deactivating:
            refs = self._check_model_dependency(obj.model_name)
            if refs:
                return Response(
                    {"detail": f"模型正在被 {', '.join(refs)} 引用，禁止停用"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 其他修改：创建工单走审批
        changed_fields = {}
        if 'name' in norm:
            changed_fields['name'] = {'old': obj.name, 'new': norm['name']}
        if 'model_type' in data:
            changed_fields['model_type'] = {'old': obj.model_type, 'new': data['model_type']}
        if 'base_url' in norm:
            changed_fields['base_url'] = {'old': obj.base_url, 'new': norm['base_url']}
        if 'model_name' in norm:
            changed_fields['model_name'] = {'old': obj.model_name, 'new': norm['model_name']}
        if 'timeout' in norm:
            changed_fields['timeout'] = {'old': obj.timeout, 'new': norm['timeout']}
        if 'is_active' in norm:
            changed_fields['is_active'] = {'old': obj.is_active, 'new': norm['is_active']}

        if not changed_fields:
            return Response({"detail": "未检测到字段变更"}, status=status.HTTP_400_BAD_REQUEST)

        # 检查是否已有待审批的工单，避免重复提交
        existing = Ticket.objects.filter(
            ticket_type='model', target_model_id=obj.id,
            status__in=['pending', 'pending_review']
        ).first()
        if existing:
            return Response(
                {"detail": f"该模型已有待审批工单(id={existing.id})，请等待审批完成"},
                status=status.HTTP_409_CONFLICT,
            )

        is_deactivate_op = 'is_active' in changed_fields and not changed_fields['is_active']['new']
        operation = 'deactivate' if is_deactivate_op else 'update_normal'
        risk_level = 'normal'  # 修改和停用都是普通审批

        ticket = Ticket.objects.create(
            ticket_type='model',
            operation=operation,
            status='pending',
            risk_level=risk_level,
            reason=(data.get('reason') or '').strip(),
            target_model_id=obj.id,
            detail={
                'target_model_snapshot': before,
                'changed_fields': changed_fields,
                'dependency_refs': self._check_model_dependency(obj.model_name) if is_deactivate_op else [],
            },
            creator=request.user if request.user.is_authenticated else None,
        )
        # 写审计日志
        try:
            from apps.audit.models import AuditLog
            AuditLog.objects.create(
                actor=request.user,
                actor_username=request.user.username,
                action='create_model_ticket',
                action_category='config',
                target_type='model_ticket',
                target_id=str(ticket.id),
                result='success',
                detail={
                    'ticket_id': ticket.id,
                    'operation': operation,
                    'model_id': obj.id,
                    'changed_fields': changed_fields,
                    'reason': ticket.reason,
                },
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method=request.method,
                path=request.path,
            )
        except Exception as e:
            logger.warning(f"写模型工单审计日志失败: {e}")

        logger.info(f"模型变更工单已创建: ticket={ticket.id} model_id={obj.id} by {request.user.username}")
        return Response({
            "detail": "已提交审批",
            "ticket_id": ticket.id,
            "operation": operation,
        }, status=status.HTTP_202_ACCEPTED)

    def partial_update(self, request, *args, **kwargs):
        """PATCH 部分更新，复用 update 逻辑"""
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """模型删除：超管复核 + 检查依赖

        删除操作风险最高：物理删除不可恢复，必须经过超管复核。
        同时检查是否被配置项引用，有依赖则禁止删除。
        """
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        obj = self.get_object()
        before = self._ser(obj)

        # 检查依赖
        refs = self._check_model_dependency(obj.model_name)
        if refs:
            return Response(
                {"detail": f"模型正在被 {', '.join(refs)} 引用，禁止删除"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 检查是否已有待审批的工单
        existing = Ticket.objects.filter(
            ticket_type='model', target_model_id=obj.id,
            status__in=['pending', 'pending_review']
        ).first()
        if existing:
            return Response(
                {"detail": f"该模型已有待审批工单(id={existing.id})，请等待审批完成"},
                status=status.HTTP_409_CONFLICT,
            )

        # 创建删除工单（超管复核）
        ticket = Ticket.objects.create(
            ticket_type='model',
            operation='delete',
            status='pending',
            risk_level='high',
            reason=(request.data.get('reason') or '').strip(),
            target_model_id=obj.id,
            detail={
                'target_model_snapshot': before,
                'changed_fields': {},
                'dependency_refs': refs,
            },
            creator=request.user if request.user.is_authenticated else None,
        )
        # 写审计日志
        try:
            from apps.audit.models import AuditLog
            AuditLog.objects.create(
                actor=request.user,
                actor_username=request.user.username,
                action='create_model_ticket',
                action_category='config',
                target_type='model_ticket',
                target_id=str(ticket.id),
                result='success',
                detail={
                    'ticket_id': ticket.id,
                    'operation': 'delete',
                    'model_id': obj.id,
                    'reason': ticket.reason,
                },
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method=request.method,
                path=request.path,
            )
        except Exception as e:
            logger.warning(f"写模型工单审计日志失败: {e}")

        logger.info(f"模型删除工单已创建: ticket={ticket.id} model_id={obj.id} by {request.user.username}")
        return Response({
            "detail": "删除已提交审批（超管复核）",
            "ticket_id": ticket.id,
        }, status=status.HTTP_202_ACCEPTED)

    def _check_model_dependency(self, model_name):
        """检查模型是否被 SystemConfig 中的配置项引用

        引用模型的配置 key 列表：LLM 模型用于对话/评估，Embedding/Rerank 用于向量化/重排序。
        若模型被引用，停用或删除会导致引用方找不到模型，直接报错。
        """
        SYSTEM_CONFIG_KEYS_REFERENCING_MODELS = [
            'LLM_BASE_MODEL', 'LLM_ADVANCED_MODEL', 'EVAL_MODEL',
            'EMBEDDING_MODEL', 'RERANK_MODEL',
        ]
        refs = SystemConfig.objects.filter(
            key__in=SYSTEM_CONFIG_KEYS_REFERENCING_MODELS,
            value=model_name,
        ).values_list('key', flat=True)
        return list(refs)

    def _invalidate_llm_cache(self):
        """失效 LLMModel 相关缓存

        LLMModel 改动会影响 config_loader.get_llm_model_config 的返回值，
        写操作后必须清缓存，否则业务侧仍读到旧值。
        全清而非按 model_name 精准清：避免 model_name 字段被改后旧 key 残留。
        """
        try:
            from .config_loader import invalidate_llm_model_cache
            invalidate_llm_model_cache()
        except Exception as e:
            # 缓存失效失败不阻断业务，仅记录警告；5min TTL 兜底最终一致
            logger.warning(f"失效 LLMModel 缓存失败: {e}")

    def _write_audit(self, request, op, obj, before, after):
        """写审计日志：记录模型变更前后快照，便于事后追溯谁改了哪个模型配置

        与 SystemConfigView._write_audit 一致，action_category 用 config，
        这样审计页面按 config 类目筛选时可同时看到 KV 配置与模型配置的变更。
        """
        try:
            from apps.audit.models import AuditLog
            AuditLog.objects.create(
                actor=request.user,
                actor_username=request.user.username,
                action='manage_llm_model',
                action_category='config',
                target_type='llm_model',
                target_id=str(obj.id) if obj else '',
                result='success',
                detail={'op': op, 'before': before, 'after': after},
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method=request.method,
                path=request.path,
            )
        except Exception as e:
            # 审计日志写失败不应阻断业务流程，仅记录警告便于运维排查
            logger.warning(f"写模型管理审计日志失败: {e}")



class _TicketMixin:
    """工单公共方法 mixin：权限校验、审计日志、模型依赖检查

    TicketViewSet 和 _TicketOperationBase 共用这些逻辑，避免重复代码。
    """

    def _check_perm(self, request, perm):
        """统一权限校验：复用 User.has_perm（super_admin 快路径 + RBAC 权限点判定）"""
        if not request.user.has_perm(perm):
            return Response(
                {"detail": "无权限执行此操作，需要 " + perm + " 权限"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _write_audit(self, request, action, ticket, extra=None):
        """写工单审计日志：敏感项 old/new 掾码，失败不阻断业务"""
        try:
            from apps.audit.models import AuditLog
            detail = {
                'ticket_id': ticket.id,
                'ticket_type': ticket.ticket_type,
                'operation': ticket.operation,
                'status': ticket.status,
            }
            if ticket.config_key:
                cfg = SystemConfig.objects.filter(key=ticket.config_key).first()
                is_secret = bool(cfg and cfg.is_secret)
                td = ticket.detail if isinstance(ticket.detail, dict) else {}
                detail['config_key'] = ticket.config_key
                detail['old'] = '***' if is_secret else td.get('old_value', '')
                detail['new'] = '***' if is_secret else td.get('new_value', '')
            if ticket.target_model_id:
                detail['target_model_id'] = ticket.target_model_id
            if extra:
                detail.update(extra)
            AuditLog.objects.create(
                actor=request.user,
                actor_username=request.user.username,
                action=action,
                action_category='config' if ticket.ticket_type in ('config', 'schedule') else 'model',
                target_type='ticket',
                target_id=str(ticket.id),
                result='success',
                detail=detail,
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method=request.method,
                path=request.path,
            )
        except Exception as e:
            # 审计可丢、业务不可丢
            logger.warning(f"写工单审计日志失败: {e}")

    def _check_model_dependency(self, model_id):
        """按模型 ID 检查是否被 SystemConfig 配置项引用

        引用模型的配置 key 列表：LLM 模型用于对话/评估，Embedding/Rerank 用于向量化/重排序。
        若模型被引用，停用或删除会导致引用方找不到模型，直接报错。
        """
        model = LLMModel.objects.filter(id=model_id).first()
        if not model:
            return []
        SYSTEM_CONFIG_KEYS = [
            'LLM_BASE_MODEL', 'LLM_ADVANCED_MODEL', 'EVAL_MODEL',
            'EMBEDDING_MODEL', 'RERANK_MODEL',
        ]
        refs = SystemConfig.objects.filter(
            key__in=SYSTEM_CONFIG_KEYS,
            value=model.model_name,
        ).values_list('key', flat=True)
        return list(refs)


class TicketViewSet(_TicketMixin, APIView):
    """统一工单列表接口（GET /api/v1/system/tickets/）

    合并原 ConfigChangeTicket.list() 和 ModelChangeTicket.list()，查询统一 Ticket 表。
    支持查询参数：
    - ticket_type: 按工单类型过滤（config/schedule/model），可选
    - status: 按状态过滤，支持逗号分隔多值（如 pending,pending_review），可选
    - creator: 'me' 表示只看当前用户的工单，可选
    - search: 搜索关键词（最多 100 字符），数字匹配 id/target_model_id，
      同时匹配 creator username（精确）、config_key（包含）、detail.config_label（包含）
    - page / page_size: 分页（page 默认 1，page_size 默认 50，最大 200）

    返回格式：{"tickets": [...], "total": N}
    """
    permission_classes = [IsAuthenticated]

    def _serialize_ticket(self, t, config_map=None, model_map=None):
        """序列化单条工单：包含公共字段 + 类型特有字段

        config/schedule 类型：从 detail JSON 取 config_label/old_value/new_value/change_summary，
        并根据 SystemConfig.is_secret 判断是否掩码 old_value/new_value。
        model 类型：从 detail JSON 取 target_model_snapshot/changed_fields/dependency_refs，
        并查 LLMModel 获取模型显示名。

        config_map/model_map 为批量预加载的缓存，避免 N+1 查询。
        """
        config_map = config_map or {}
        model_map = model_map or {}
        detail = t.detail if isinstance(t.detail, dict) else {}

        # --- 公共字段 ---
        result = {
            "id": t.id,
            "ticket_type": t.ticket_type,
            "operation": t.operation,
            "operation_display": dict(Ticket.OPERATION_CHOICES).get(t.operation, t.operation),
            "status": t.status,
            "status_display": dict(Ticket.STATUS_CHOICES).get(t.status, t.status),
            "risk_level": t.risk_level,
            "reason": t.reason,
            # 人员
            "creator": t.creator.username if t.creator else '',
            "auditor": t.auditor.username if t.auditor else '',
            "reviewer": t.reviewer.username if t.reviewer else '',
            # 意见
            "audit_comment": t.audit_comment,
            "review_comment": t.review_comment,
            # 时间
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "audited_at": t.audited_at.isoformat() if t.audited_at else None,
            "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
            "applied_at": t.applied_at.isoformat() if t.applied_at else None,
        }

        # --- config/schedule 类型特有字段 ---
        if t.ticket_type in ('config', 'schedule'):
            # 判断配置项是否为敏感项，敏感项的 old/new 值掩码
            cfg = config_map.get(t.config_key) if t.config_key else None
            is_secret = bool(cfg and cfg.is_secret)
            old_value = detail.get('old_value', '')
            new_value = detail.get('new_value', '')
            if is_secret:
                old_value = '***'
                new_value = '***'
            # change_summary 存储为 JSON 字符串，解析后返回便于前端渲染
            change_summary = None
            cs_raw = detail.get('change_summary', '')
            if cs_raw:
                try:
                    change_summary = json.loads(cs_raw) if isinstance(cs_raw, str) else cs_raw
                except (json.JSONDecodeError, TypeError):
                    change_summary = None
            result.update({
                "config_key": t.config_key,
                "config_label": detail.get('config_label', ''),
                "old_value": old_value,
                "new_value": new_value,
                "change_summary": change_summary,
            })
        else:
            # 非 config/schedule 类型也补全这些字段为空值，保证前端 schema 统一
            result.update({
                "config_key": t.config_key,
                "config_label": '',
                "old_value": '',
                "new_value": '',
                "change_summary": None,
            })

        # --- model 类型特有字段 ---
        if t.ticket_type == 'model':
            target_model_name = ''
            if t.target_model_id:
                m = model_map.get(t.target_model_id)
                if m:
                    target_model_name = f'{m.name} ({m.model_name})'
            snapshot = detail.get('target_model_snapshot', {})
            changed_fields_raw = detail.get('changed_fields', {})
            # changed_fields 存储 {field: {old, new}}，提取字段名列表
            changed_field_names = list(changed_fields_raw.keys()) if isinstance(changed_fields_raw, dict) else []
            # 非修改操作时根据操作类型构造展示字段
            if t.operation == 'delete':
                changed_field_names = ['name', 'model_name', 'model_type', 'base_url', 'timeout']
            elif t.operation == 'deactivate':
                changed_field_names = ['is_active']
            result.update({
                "action": t.operation,
                "model_id": t.target_model_id,
                "model_name": target_model_name,
                "target_model_id": t.target_model_id,
                "target_model_name": target_model_name,
                "target_model_snapshot": snapshot,
                "changed_fields": changed_field_names,
                "change_data": changed_fields_raw if isinstance(changed_fields_raw, dict) else {},
                "snapshot_data": {
                    'name': snapshot.get('name', ''),
                    'model_name': snapshot.get('model_name', ''),
                    'model_type': snapshot.get('model_type', ''),
                    'provider': snapshot.get('provider', ''),
                    'base_url': snapshot.get('base_url', ''),
                    'timeout': snapshot.get('timeout', ''),
                    'is_active': snapshot.get('is_active', True),
                } if snapshot else {},
                "dependency_refs": detail.get('dependency_refs', []),
            })
        else:
            # 非 model 类型也补全这些字段为空值，保证前端 schema 统一
            result.update({
                "target_model_id": t.target_model_id,
                "target_model_name": '',
                "target_model_snapshot": {},
                "changed_fields": [],
                "change_data": {},
                "snapshot_data": {},
                "dependency_refs": [],
            })

        return result

    def get(self, request):
        """统一工单列表：支持按类型/状态/创建人/关键词过滤 + 分页"""
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied

        qs = Ticket.objects.all()

        # 按工单类型过滤（config/schedule/model）
        ticket_type = (request.query_params.get('ticket_type') or '').strip()
        if ticket_type:
            qs = qs.filter(ticket_type=ticket_type)

        # 全局搜索：id 精确匹配、target_model_id 精确匹配、creator username 精确匹配、
        # config_key 包含匹配、detail.config_label 包含匹配
        search_query = (request.query_params.get('search') or '').strip()[:100]
        if search_query:
            from django.db.models import Q
            q = Q()
            if search_query.isdigit():
                q |= Q(id=int(search_query))
                q |= Q(target_model_id=int(search_query))
            q |= Q(creator__username__iexact=search_query)
            q |= Q(config_key__icontains=search_query)
            q |= Q(detail__config_label__icontains=search_query)
            qs = qs.filter(q)

        # "我的工单"：按创建人筛选，展示自己创建的工单（所有状态，不排除任何状态）
        creator_filter = (request.query_params.get('creator') or '').strip()
        if creator_filter == 'me':
            qs = qs.filter(creator=request.user)
        else:
            # 非"我的工单"时，按状态筛选
            status_filter = (request.query_params.get('status') or '').strip()
            if status_filter and status_filter != 'all':
                statuses = [s.strip() for s in status_filter.split(',') if s.strip()]
                if len(statuses) == 1:
                    qs = qs.filter(status=statuses[0])
                elif len(statuses) > 1:
                    qs = qs.filter(status__in=statuses)
                # 仅在指定了具体状态时排除：创建人不能审批自己的工单
                qs = qs.exclude(status__in=['pending', 'pending_review'], creator=request.user)
                # 待复核阶段：审核人不可见（防止审核+复核由同一人完成）
                qs = qs.exclude(status='pending_review', auditor=request.user)

        qs = qs.select_related('creator', 'auditor', 'reviewer')
        # 分页返回
        total = qs.count()
        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = max(1, min(int(request.query_params.get('page_size', 50)), 200))
        except (ValueError, TypeError):
            return Response({"detail": "page/page_size 参数无效"}, status=400)
        start = (page - 1) * page_size
        page_tickets = list(qs[start:start + page_size])

        # 批量预加载关联数据，避免 N+1 查询
        config_keys = {t.config_key for t in page_tickets if t.config_key}
        model_ids = {t.target_model_id for t in page_tickets if t.target_model_id}
        config_map = {c.key: c for c in SystemConfig.objects.filter(key__in=config_keys)} if config_keys else {}
        model_map = {m.id: m for m in LLMModel.objects.filter(id__in=model_ids)} if model_ids else {}

        tickets = [self._serialize_ticket(t, config_map, model_map) for t in page_tickets]
        return Response({"tickets": tickets, "total": total})

    def post(self, request):
        """POST /api/v1/system/tickets/  统一工单创建入口

        支持三种工单类型：
        - config：配置变更工单
        - schedule：定时任务变更工单（与 config 共享配置校验逻辑）
        - model：模型变更工单

        请求体字段：
        - ticket_type (必填): 'config' / 'schedule' / 'model'
        - reason (可选): 变更原因
        - config/schedule 类型：
          - config_key (必填): 配置项 key
          - new_value (必填): 新值
        - model 类型：
          - target_model_id (必填): 目标模型 ID
          - operation (必填): 'update_normal' / 'deactivate' / 'delete'
          - changed_fields (update_normal 时必填): {field: {old, new}}
          - dependency_refs (可选): 依赖引用列表

        业务规则：
        - config/schedule 工单：校验配置项存在性、是否只读、值是否有变化，
          风险等级取自 SystemConfig.risk_level，operation 固定为 'modify'
        - model 工单：校验模型存在性、是否已有待审批工单、delete/deactivate 时检查依赖，
          风险等级固定为 'high'（模型操作均视为高风险）
        - 创建后写审计日志，审计失败不阻断主业务
        """
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied

        data = request.data or {}
        ticket_type = (data.get('ticket_type') or '').strip()
        reason = (data.get('reason') or '').strip()

        # 校验 ticket_type 必填且取值合法
        valid_types = {c[0] for c in Ticket.TYPE_CHOICES}
        if ticket_type not in valid_types:
            return Response(
                {"detail": f"ticket_type 必填且取值为 {'/'.join(valid_types)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 按类型分派创建逻辑
        if ticket_type in ('config', 'schedule'):
            return self._create_config_ticket(request, ticket_type, data, reason)
        else:
            return self._create_model_ticket(request, data, reason)

    def _create_config_ticket(self, request, ticket_type, data, reason):
        """创建配置/定时任务变更工单

        校验流程：
        1. config_key 必填且对应的 SystemConfig 存在
        2. 配置项不能是只读项（只读项需改 .env 后重启）
        3. new_value 必填，按 value_type 规范化后与当前值比较
        4. 调度类配置额外校验 cron 语法
        5. 计算变更摘要（多值类配置）
        6. 创建 Ticket 写入统一工单表
        """
        config_key = (data.get('config_key') or '').strip()
        if not config_key:
            return Response({"detail": "config_key 必填"}, status=status.HTTP_400_BAD_REQUEST)

        # 查询配置项
        try:
            cfg = SystemConfig.objects.get(key=config_key)
        except SystemConfig.DoesNotExist:
            return Response({"detail": f"配置项 {config_key} 不存在"}, status=status.HTTP_404_NOT_FOUND)

        # 只读项禁止提交工单
        if cfg.is_readonly:
            return Response(
                {"detail": f"配置项 {config_key} 为只读项，需在 .env 中修改后重启生效"},
                status=status.HTTP_409_CONFLICT,
            )

        new_value = data.get('new_value', '')
        if new_value == '' and data.get('new_value') is None:
            return Response({"detail": "new_value 必填"}, status=status.HTTP_400_BAD_REQUEST)

        # 按类型规范化存储，避免前端传入不规范格式
        try:
            normalized = SystemConfigView()._normalize_value(new_value, cfg.value_type)
        except ValueError as e:
            logger.warning(f"TicketViewSet.post normalize failed key={config_key}: {e}")
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # 调度类配置：校验 cron 语法并规范化为统一存储格式
        if is_schedule_key(cfg.key):
            try:
                normalized = normalize_schedule_value(normalized)
            except ValueError as e:
                logger.warning(f"TicketViewSet.post schedule validate failed key={config_key}: {e}")
                return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # 值无变化则拒绝提交
        if normalized == cfg.value:
            return Response({"detail": "新值与当前值一致，无需提交工单"}, status=status.HTTP_400_BAD_REQUEST)

        # 计算多值类配置的变更摘要（如 BUSINESS_DB_TABLES 的 added/removed）
        change_summary = SystemConfigView()._compute_change_summary(cfg.key, cfg.value, normalized)

        # 构造 detail JSON：config/schedule 工单的业务详情
        detail = {
            'config_label': cfg.label,
            'old_value': cfg.value,
            'new_value': normalized,
            'change_summary': change_summary,
        }

        # 冗余 risk_level，避免后续 SystemConfig.risk_level 变更影响本工单审批流程
        ticket = Ticket.objects.create(
            ticket_type=ticket_type,
            operation='modify',
            status='pending',
            risk_level=cfg.risk_level,
            reason=reason,
            creator=request.user if request.user.is_authenticated else None,
            config_key=cfg.key,
            detail=detail,
        )

        # 写审计日志：审计可丢、业务不可丢
        self._write_audit(request, action='create_ticket', ticket=ticket,
                          extra={'reason': reason})

        logger.info(f"配置变更工单已创建: ticket={ticket.id} key={config_key} type={ticket_type} by {request.user.username}")
        return Response(self._serialize_ticket(ticket), status=status.HTTP_201_CREATED)

    def _create_model_ticket(self, request, data, reason):
        """创建模型变更工单

        校验流程：
        1. target_model_id 必填且对应的 LLMModel 存在
        2. operation 必填且取值合法
        3. update_normal 时 changed_fields 必填（至少一个字段变更）
        4. delete/deactivate 时检查依赖引用
        5. 检查是否已有待审批/待复核的工单，避免重复提交
        6. 创建 Ticket 写入统一工单表，风险等级固定为 'high'
        """
        target_model_id = data.get('target_model_id')
        if not target_model_id:
            return Response({"detail": "target_model_id 必填"}, status=status.HTTP_400_BAD_REQUEST)

        # 校验模型存在性
        try:
            target_model_id = int(target_model_id)
        except (ValueError, TypeError):
            return Response({"detail": "target_model_id 必须为整数"}, status=status.HTTP_400_BAD_REQUEST)

        model = LLMModel.objects.filter(id=target_model_id).first()
        if not model:
            return Response({"detail": f"模型 id={target_model_id} 不存在"}, status=status.HTTP_404_NOT_FOUND)

        # 校验 operation
        operation = (data.get('operation') or '').strip()
        valid_ops = {'update_normal', 'deactivate', 'delete'}
        if operation not in valid_ops:
            return Response(
                {"detail": f"operation 必填且取值为 {'/'.join(sorted(valid_ops))}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # update_normal 时校验 changed_fields
        changed_fields = data.get('changed_fields', {})
        if operation == 'update_normal':
            if not changed_fields or not isinstance(changed_fields, dict):
                return Response(
                    {"detail": "update_normal 操作需要 changed_fields 字段（{field: {old, new}}）"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 检查是否已有待审批/待复核的工单，避免重复提交
        existing = Ticket.objects.filter(
            ticket_type='model', target_model_id=target_model_id,
            status__in=['pending', 'pending_review'],
        ).first()
        if existing:
            return Response(
                {"detail": f"该模型已有待审批工单(id={existing.id})，请等待审批完成"},
                status=status.HTTP_409_CONFLICT,
            )

        # delete/deactivate 时检查依赖引用
        dependency_refs = data.get('dependency_refs', [])
        if operation in ('delete', 'deactivate'):
            refs = self._check_model_dependency(target_model_id)
            if refs:
                # 删除时有依赖则禁止；停用时有依赖则禁止
                return Response(
                    {"detail": f"模型正在被 {', '.join(refs)} 引用，禁止{dict(Ticket.OPERATION_CHOICES).get(operation, operation)}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            dependency_refs = refs

        # 捕获模型当前状态快照，防止审批时模型已被他人修改
        model_snapshot = {
            'id': model.id,
            'name': model.name,
            'provider': model.provider,
            'model_type': model.model_type,
            'base_url': model.base_url,
            'model_name': model.model_name,
            'timeout': model.timeout,
            'is_active': model.is_active,
            'created_at': model.created_at.isoformat() if model.created_at else None,
            'updated_at': model.updated_at.isoformat() if model.updated_at else None,
        }

        # 构造 detail JSON：模型工单的业务详情
        detail = {
            'target_model_snapshot': model_snapshot,
            'changed_fields': changed_fields if operation == 'update_normal' else {},
            'dependency_refs': dependency_refs,
        }

        # 模型操作风险等级固定为 high（模型变更影响面大，均需复核）
        ticket = Ticket.objects.create(
            ticket_type='model',
            operation=operation,
            status='pending',
            risk_level='high',
            reason=reason,
            creator=request.user if request.user.is_authenticated else None,
            target_model_id=target_model_id,
            detail=detail,
        )

        # 写审计日志
        self._write_audit(request, action='create_ticket', ticket=ticket,
                          extra={'reason': reason, 'operation': operation,
                                 'model_id': target_model_id,
                                 'changed_fields': changed_fields if operation == 'update_normal' else {}})

        logger.info(f"模型变更工单已创建: ticket={ticket.id} model_id={target_model_id} operation={operation} by {request.user.username}")
        return Response(self._serialize_ticket(ticket), status=status.HTTP_201_CREATED)


class _TicketOperationBase(_TicketMixin, APIView):
    """工单操作基类：提供公共的工单查询方法

    公共的权限校验、审计写入和依赖检查方法由 _TicketMixin 提供。
    approve/reject/withdraw 三个子类共用这些逻辑，避免重复代码。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        """显式拒绝 GET 请求，返回 405 而非 401，避免日志中出现 Unauthorized 告警"""
        return Response({"detail": "Method not allowed"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def _get_ticket(self, pk):
        """按主键查询工单，不存在返回 404 Response；成功返回 Ticket 实例"""
        try:
            return Ticket.objects.get(pk=pk)
        except Ticket.DoesNotExist:
            return None

    def _apply_config(self, ticket, request):
        """配置工单通过后写入 SystemConfig + 审计日志

        用事务保证工单状态与配置写入的一致性：要么都成功，要么都回滚，
        避免出现"工单已通过但配置没写进去"或"配置改了但工单状态没更新"的不一致。
        写库后立即失效 SystemConfig 缓存（延迟双删），保证业务侧下次读到新值。
        """
        detail = ticket.detail if isinstance(ticket.detail, dict) else {}
        new_value = detail.get('new_value', '')
        with transaction.atomic():
            # 重新锁行查询，防止并发审批重复生效
            cfg = SystemConfig.objects.select_for_update().get(key=ticket.config_key)
            old_value = cfg.value
            cfg.value = new_value
            cfg.updated_by = ticket.creator
            cfg.save()
            # 工单置为 approved 并记录生效时间，与配置写入同事务保证一致
            ticket.status = 'approved'
            ticket.applied_at = timezone.now()
            ticket.save()
        # 事务提交后再清缓存，避免清缓存后另一线程读到旧值回填
        try:
            from .config_loader import invalidate_config_cache
            invalidate_config_cache(ticket.config_key)
        except Exception as e:
            # 缓存失效失败不阻断审批流程，5min TTL 兜底最终一致
            logger.warning(f"失效 SystemConfig 缓存失败 key={ticket.config_key}: {e}")
        # 单独写 apply 审计日志（事务外，避免审计哈希链计算拖长事务）
        self._write_audit(
            request, action='apply_ticket', ticket=ticket,
            extra={'applied_old': old_value, 'applied_new': new_value},
        )

    def _apply_model_ticket(self, ticket, request):
        """模型工单通过后执行模型变更

        操作类型：
        - update_normal：修改字段（base_url/model_name/provider/timeout/model_type）
        - deactivate：设置 is_active=False
        - delete：删除模型
        用事务保证工单状态与模型变更的一致性。
        审批时重新检查依赖，防止工单创建后到审批前模型被其他配置引用。
        """
        detail = ticket.detail if isinstance(ticket.detail, dict) else {}
        changed_fields = detail.get('changed_fields', {})

        with transaction.atomic():
            ticket.status = 'approved'
            ticket.applied_at = timezone.now()

            if ticket.operation == 'delete':
                # 删除模型：先检查依赖（双保险，创建时+审核时均已检查）
                if ticket.target_model_id:
                    model = LLMModel.objects.select_for_update().filter(id=ticket.target_model_id).first()
                    if model:
                        refs = self._check_model_dependency(model.id)
                        if refs:
                            # 有依赖则回滚工单状态，拒绝生效
                            # 清除调用方已设置的审批人信息，避免状态不一致
                            ticket.status = 'pending'
                            ticket.applied_at = None
                            ticket.auditor = None
                            ticket.audit_comment = ''
                            ticket.audited_at = None
                            ticket.reviewer = None
                            ticket.review_comment = ''
                            ticket.reviewed_at = None
                            ticket.save()
                            return Response(
                                {"detail": f"审批期间模型被 {', '.join(refs)} 引用，操作已回滚"},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        # 先断开所有工单与模型的关联，避免外键约束阻止删除
                        Ticket.objects.filter(
                            ticket_type='model', target_model_id=model.id
                        ).update(target_model_id=None)
                        model.delete()
                ticket.target_model_id = None
                ticket.save()
            elif ticket.operation == 'deactivate':
                # 停用：重新检查依赖，防止审批期间被引用
                if ticket.target_model_id:
                    model = LLMModel.objects.select_for_update().filter(id=ticket.target_model_id).first()
                    if model:
                        refs = self._check_model_dependency(model.id)
                        if refs:
                            ticket.status = 'pending'
                            ticket.applied_at = None
                            ticket.auditor = None
                            ticket.audit_comment = ''
                            ticket.audited_at = None
                            ticket.reviewer = None
                            ticket.review_comment = ''
                            ticket.reviewed_at = None
                            ticket.save()
                            return Response(
                                {"detail": f"审批期间模型被 {', '.join(refs)} 引用，停用操作已回滚"},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        model.is_active = False
                        model.save()
                ticket.save()
            elif ticket.operation == 'update_normal':
                # 修改：应用变更字段
                if ticket.target_model_id:
                    model = LLMModel.objects.select_for_update().filter(id=ticket.target_model_id).first()
                    if model and isinstance(changed_fields, dict):
                        for field, changes in changed_fields.items():
                            if isinstance(changes, dict) and 'new' in changes:
                                setattr(model, field, changes['new'])
                        model.save()
                ticket.save()

        # 清缓存
        try:
            from .config_loader import invalidate_llm_model_cache
            invalidate_llm_model_cache()
        except Exception as e:
            logger.warning(f"失效 LLMModel 缓存失败: {e}")

        # 写 apply 审计日志
        self._write_audit(request, 'apply_ticket', ticket)


class ApproveTicketView(_TicketOperationBase):
    """POST /api/v1/system/tickets/{id}/approve/  统一审批通过

    流程：
    - pending 状态：审核。普通项直接通过并生效；高风险项进入 pending_review 待复核
    - pending_review 状态：复核（仅 is_super_admin 可操作），通过后生效
    防自审：审批人不能是创建人自己（避免单人完成创建+审批）
    """

    def post(self, request, pk):
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        ticket = self._get_ticket(pk)
        if not ticket:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        comment = (request.data.get('comment') or '').strip()

        if ticket.status == 'pending':
            # 审核：防自审，审批人不能是创建人
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能审批自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)

            if ticket.risk_level == 'high':
                # 高风险：审核通过后进入待复核
                # 对模型删除操作，双保险：审核时也重新检查依赖，防止创建后到审批前被引用
                if ticket.operation == 'delete' and ticket.target_model_id:
                    refs = self._check_model_dependency(ticket.target_model_id)
                    if refs:
                        return Response(
                            {"detail": f"模型已被 {', '.join(refs)} 引用，无法进入复核"},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                ticket.status = 'pending_review'
                ticket.auditor = request.user if request.user.is_authenticated else None
                ticket.audit_comment = comment
                ticket.audited_at = timezone.now()
                # 更新依赖快照（审批时重新检查，更新到工单上）
                if ticket.operation in ('delete', 'deactivate') and ticket.target_model_id:
                    detail = ticket.detail if isinstance(ticket.detail, dict) else {}
                    detail['dependency_refs'] = self._check_model_dependency(ticket.target_model_id)
                    ticket.detail = detail
                ticket.save()
                self._write_audit(request, action='approve_ticket', ticket=ticket,
                                  extra={'stage': 'first_review', 'comment': comment})
                logger.info(f"工单 {ticket.id} 审核通过，等待复核 by {request.user.username}")
                return Response({'detail': '审核通过，等待复核', 'ticket_id': ticket.id,
                                 'status': ticket.status})

            # 普通项审核通过即生效：按工单类型分派执行
            ticket.auditor = request.user if request.user.is_authenticated else None
            ticket.audit_comment = comment
            ticket.audited_at = timezone.now()

            # 对模型停用操作，审核时也先快速检查依赖
            if ticket.operation == 'deactivate' and ticket.target_model_id:
                refs = self._check_model_dependency(ticket.target_model_id)
                if refs:
                    return Response(
                        {"detail": f"模型已被 {', '.join(refs)} 引用，无法停用"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                detail = ticket.detail if isinstance(ticket.detail, dict) else {}
                detail['dependency_refs'] = refs
                ticket.detail = detail

            # 按工单类型执行生效逻辑
            if ticket.ticket_type in ('config', 'schedule'):
                self._apply_config(ticket, request)
            elif ticket.ticket_type == 'model':
                result = self._apply_model_ticket(ticket, request)
                if isinstance(result, Response):
                    return result

            self._write_audit(request, action='approve_ticket', ticket=ticket,
                              extra={'stage': 'first_review', 'comment': comment})
            logger.info(f"工单 {ticket.id} 审核通过并已生效 by {request.user.username}")
            return Response({'detail': '审批通过并已生效', 'ticket_id': ticket.id,
                             'status': ticket.status})

        if ticket.status == 'pending_review':
            # 复核：仅超管可操作，且不能与审核人/创建人同一人
            if not request.user.is_super_admin:
                return Response({"detail": "高风险项复核仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能复核自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.auditor_id and ticket.auditor_id == request.user.id:
                return Response({"detail": "复核人不能与审核人相同"}, status=status.HTTP_403_FORBIDDEN)
            ticket.reviewer = request.user if request.user.is_authenticated else None
            ticket.review_comment = comment
            ticket.reviewed_at = timezone.now()

            # 按工单类型执行生效逻辑
            if ticket.ticket_type in ('config', 'schedule'):
                self._apply_config(ticket, request)
            elif ticket.ticket_type == 'model':
                result = self._apply_model_ticket(ticket, request)
                if isinstance(result, Response):
                    return result

            self._write_audit(request, action='approve_ticket', ticket=ticket,
                              extra={'stage': 'super_admin_review', 'comment': comment})
            logger.info(f"工单 {ticket.id} 复核通过并已生效 by {request.user.username}")
            return Response({'detail': '复核通过并已生效', 'ticket_id': ticket.id,
                             'status': ticket.status})

        # 非 pending/pending_review 状态不可审批
        return Response({"detail": f"工单当前状态 {ticket.status} 不可审批"}, status=status.HTTP_409_CONFLICT)


class RejectTicketView(_TicketOperationBase):
    """POST /api/v1/system/tickets/{id}/reject/  统一驳回

    - pending：审核驳回（需 system.config.write，且审批人 ≠ 创建人）
    - pending_review：超管复核驳回（仅 is_super_admin）
    驳回需填写原因，便于创建人了解被驳回依据。
    """

    def post(self, request, pk):
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        ticket = self._get_ticket(pk)
        if not ticket:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        comment = (request.data.get('comment') or '').strip()
        if not comment:
            return Response({"detail": "请填写驳回原因"}, status=status.HTTP_400_BAD_REQUEST)

        if ticket.status == 'pending':
            # 审核驳回：防自审
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能驳回自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            ticket.status = 'rejected'
            ticket.auditor = request.user if request.user.is_authenticated else None
            ticket.audit_comment = comment
            ticket.audited_at = timezone.now()
            ticket.save()
            self._write_audit(request, action='reject_ticket', ticket=ticket,
                              extra={'stage': 'first_review', 'comment': comment})
            logger.info(f"工单 {ticket.id} 审核驳回 by {request.user.username}")
            return Response({'detail': '已驳回', 'ticket_id': ticket.id, 'status': ticket.status})

        if ticket.status == 'pending_review':
            # 复核驳回：仅超管可操作，且不能与审核人/创建人同一人
            if not request.user.is_super_admin:
                return Response({"detail": "高风险项复核仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能驳回自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.auditor_id and ticket.auditor_id == request.user.id:
                return Response({"detail": "复核驳回人与审核人不能相同"}, status=status.HTTP_403_FORBIDDEN)
            ticket.status = 'rejected'
            ticket.reviewer = request.user if request.user.is_authenticated else None
            ticket.review_comment = comment
            ticket.reviewed_at = timezone.now()
            ticket.save()
            self._write_audit(request, action='reject_ticket', ticket=ticket,
                              extra={'stage': 'super_admin_review', 'comment': comment})
            logger.info(f"工单 {ticket.id} 复核驳回 by {request.user.username}")
            return Response({'detail': '复核驳回', 'ticket_id': ticket.id, 'status': ticket.status})

        return Response({"detail": f"工单当前状态 {ticket.status} 不可驳回"}, status=status.HTTP_409_CONFLICT)


class WithdrawTicketView(_TicketOperationBase):
    """POST /api/v1/system/tickets/{id}/withdraw/  创建人撤回

    仅创建人本人可操作，且仅 pending/pending_review 状态可撤回；
    已通过/已驳回的工单不可撤回（已生效或已终结）。
    """

    def post(self, request, pk):
        ticket = self._get_ticket(pk)
        if not ticket:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        # 仅创建人可撤回，防止他人撤回非自己提交的工单
        if not ticket.creator_id or ticket.creator_id != request.user.id:
            return Response({"detail": "仅创建人可撤回工单"}, status=status.HTTP_403_FORBIDDEN)
        if ticket.status not in ('pending', 'pending_review'):
            return Response({"detail": f"工单当前状态 {ticket.status} 不可撤回"}, status=status.HTTP_409_CONFLICT)

        comment = (request.data.get('comment') or '').strip()
        ticket.status = 'withdrawn'
        # 撤回时把意见写到 review_comment，便于审计追溯撤回原因
        if comment:
            ticket.review_comment = comment
        ticket.save()
        self._write_audit(request, action='withdraw_ticket', ticket=ticket,
                          extra={'comment': comment})
        logger.info(f"工单 {ticket.id} 已撤回 by {request.user.username}")
        return Response({'detail': '已撤回', 'ticket_id': ticket.id, 'status': ticket.status})


class StatsView(APIView):
    """GET /api/v1/system/stats/  首页看板"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.knowledge.models import Document, KnowledgeNode
        from apps.chat.models import QaRecord
        stats = {
            "users": User.objects.filter(is_deleted=False).count(),
            "nodes": KnowledgeNode.objects.filter(is_deleted=False).count(),
            "documents": Document.objects.filter(is_deleted=False).count(),
            "documents_ready": Document.objects.filter(is_deleted=False, status="done").count(),
            "qa_records": QaRecord.objects.count(),
            "my_qa_records": QaRecord.objects.filter(user=request.user).count(),
        }
        return Response(stats)


class GlobalSearchView(APIView):
    """GET /api/v1/system/search/?q=keyword
    跨域搜索：文档、聊天会话、知识节点（按用户权限过滤）
    返回分组结果，最多各 10 条。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.knowledge.models import Document, KnowledgeNode
        from apps.chat.models import Session
        from apps.retrieval.permission import build_permission_q

        q = (request.query_params.get("q") or "").strip()
        if not q:
            return Response({"query": "", "groups": {}})
        if len(q) > 64:
            return Response({"detail": "搜索关键词最多 64 个字符"}, status=400)

        # 文档（受权限过滤）
        doc_qs = Document.objects.filter(is_deleted=False, title__icontains=q)
        try:
            perm_q = build_permission_q(request.user)
            if perm_q:
                doc_qs = doc_qs.filter(perm_q)
        except Exception as e:
            logger.warning(f"build_permission_q failed: {e}")
        doc_qs = doc_qs.order_by("-created_at")[:10]
        docs = [
            {
                "id": d.id,
                "type": "document",
                "title": d.title,
                "subtitle": d.file_name or "",
                "url": "/upload/",
                "icon": "📄",
                "created_at": d.created_at.isoformat() if d.created_at else "",
            }
            for d in doc_qs
        ]

        # 聊天会话（仅本人）
        sess_qs = Session.objects.filter(
            is_deleted=False, user=request.user, title__icontains=q
        ).order_by("-last_active_at")[:10]
        sessions = [
            {
                "id": s.id,
                "type": "session",
                "title": s.title,
                "subtitle": "会话记录",
                "url": "/chat/",
                "icon": "💬",
                "created_at": (s.last_active_at or s.created_at).isoformat() if (s.last_active_at or s.created_at) else "",
            }
            for s in sess_qs
        ]

        # 知识节点
        node_qs = KnowledgeNode.objects.filter(is_deleted=False, name__icontains=q)[:10]
        nodes = [
            {
                "id": n.id,
                "type": "node",
                "title": n.name,
                "subtitle": "知识库节点",
                "url": "/upload/",
                "icon": "🗂️",
                "created_at": "",
            }
            for n in node_qs
        ]

        return Response({
            "query": q,
            "groups": {
                "documents": docs,
                "sessions": sessions,
                "nodes": nodes,
            },
            "total": len(docs) + len(sessions) + len(nodes),
        })
