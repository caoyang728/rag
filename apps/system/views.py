"""
system views
- GET  /api/v1/system/health/   健康检查（含 DB / Redis / LLM ping）
- GET  /api/v1/system/configs/  系统配置列表
- PUT  /api/v1/system/configs/<key>/  创建变更工单（不再直接改配置）
- GET  /api/v1/system/stats/    简易看板：文档数/QA数/用户数
- CRUD /api/v1/system/llm-models/  LLM/Embedding/Rerank 模型配置管理
- /api/v1/system/config-tickets/  配置变更工单（创建/审批/驳回/撤回）
"""
from loguru import logger
import json
import time

from django.db import connection, transaction
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.system.models import SystemConfig, LLMModel, ConfigChangeTicket, ModelChangeTicket

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
        rows = [r for r in SystemConfig.objects.all().order_by("category", "key")
                if r.key not in DEPRECATED_CONFIG_KEYS]

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

        配置修改不再直接落库，而是创建一份 ConfigChangeTicket 等待审批：
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
        # 新值与原值一致则无需创建工单，避免无效审批占位
        if new_value == old_value:
            return Response({"detail": "新值与当前值一致，无需提交工单"}, status=400)

        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "请填写变更原因"}, status=400)

        # 创建工单（pending 状态），冗余 risk_level 避免后续 SystemConfig 改动影响本工单审批流程
        # 多值类配置（如 BUSINESS_DB_TABLES）额外计算差异摘要，便于审批人快速识别变更点
        change_summary = self._compute_change_summary(obj.key, old_value, new_value)
        ticket = ConfigChangeTicket.objects.create(
            config_key=obj.key,
            config_label=obj.label,
            old_value=old_value,
            new_value=new_value,
            risk_level=obj.risk_level,
            reason=reason,
            change_summary=change_summary,
            status='pending',
            creator=request.user if request.user.is_authenticated else None,
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
            display_old = '***' if is_secret else ticket.old_value
            display_new = '***' if is_secret else ticket.new_value
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
        """序列化工单：敏感项的 old/new 值掩码，避免泄露给前端"""
        cfg = SystemConfig.objects.filter(key=t.config_key).first()
        is_secret = bool(cfg and cfg.is_secret)
        display_old = '***' if is_secret else t.old_value
        display_new = '***' if is_secret else t.new_value
        # change_summary 是 JSON 字符串，解析后返回前端便于直接渲染
        # 解析失败时返回 None，前端按"无差异摘要"处理（不影响审批流程）
        change_summary = None
        if t.change_summary:
            try:
                change_summary = json.loads(t.change_summary)
            except (json.JSONDecodeError, TypeError):
                change_summary = None
        return {
            "id": t.id,
            "config_key": t.config_key,
            "config_label": t.config_label,
            "old_value": display_old,
            "new_value": display_new,
            "risk_level": t.risk_level,
            "reason": t.reason,
            "change_summary": change_summary,
            "status": t.status,
            "creator": t.creator.username if t.creator else '',
            "reviewer": t.reviewer.username if t.reviewer else '',
            "super_admin_reviewer": t.super_admin_reviewer.username if t.super_admin_reviewer else '',
            "review_comment": t.review_comment,
            "super_admin_comment": t.super_admin_comment,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
            "super_admin_reviewed_at": t.super_admin_reviewed_at.isoformat() if t.super_admin_reviewed_at else None,
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
            ticket_qs = ModelChangeTicket.objects.filter(
                target_model_id__in=model_ids,
                status__in=['pending', 'first_approved']
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
        existing = ModelChangeTicket.objects.filter(
            target_model=obj, status__in=['pending', 'first_approved']
        ).first()
        if existing:
            return Response(
                {"detail": f"该模型已有待审批工单(id={existing.id})，请等待审批完成"},
                status=status.HTTP_409_CONFLICT,
            )

        is_deactivate_op = 'is_active' in changed_fields and not changed_fields['is_active']['new']
        operation = 'deactivate' if is_deactivate_op else 'update_normal'
        risk_level = 'normal'  # 修改和停用都是普通审批

        ticket = ModelChangeTicket.objects.create(
            target_model=obj,
            target_model_snapshot=before,
            operation=operation,
            changed_fields=changed_fields,
            dependency_refs=self._check_model_dependency(obj.model_name) if is_deactivate_op else [],
            risk_level=risk_level,
            reason=(data.get('reason') or '').strip(),
            status='pending',
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
        existing = ModelChangeTicket.objects.filter(
            target_model=obj, status__in=['pending', 'first_approved']
        ).first()
        if existing:
            return Response(
                {"detail": f"该模型已有待审批工单(id={existing.id})，请等待审批完成"},
                status=status.HTTP_409_CONFLICT,
            )

        # 创建删除工单（超管复核）
        ticket = ModelChangeTicket.objects.create(
            target_model=obj,
            target_model_snapshot=before,
            operation='delete',
            changed_fields={},
            dependency_refs=refs,
            risk_level='high',
            reason=(request.data.get('reason') or '').strip(),
            status='pending',
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


class ConfigChangeTicketViewSet(viewsets.ViewSet):
    """配置变更工单管理

    工单流转：
    - POST /config-tickets/           创建工单（create_ticket）
    - GET  /config-tickets/           工单列表（list，支持 status 筛选）
    - GET  /config-tickets/<id>/      工单详情（retrieve）
    - POST /config-tickets/<id>/approve/  审批通过（普通项直接生效；高风险项进入待复核）
    - POST /config-tickets/<id>/reject/   驳回（含审核/超管复核两种场景）
    - POST /config-tickets/<id>/withdraw/ 创建人撤回

    权限模型：
    - list/retrieve：system.config.read（维护管理员/超管可看）
    - create_ticket/approve/reject：system.config.write，且审批人 ≠ 创建人（防自审）
    - 高风险项超管复核：仅 is_super_admin 可操作
    - withdraw：仅创建人本人可操作
    """
    permission_classes = [IsAuthenticated]

    def _check_perm(self, request, perm):
        """统一权限校验：复用 User.has_perm（super_admin 快路径 + RBAC 权限点判定）"""
        if not request.user.has_perm(perm):
            return Response(
                {"detail": "无权限执行此操作，需要 " + perm + " 权限"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _serialize_ticket(self, t):
        """序列化工单：敏感项的 old/new 值掩码，避免泄露给前端

        与 SystemConfigView._serialize_ticket 逻辑一致，单独复制避免跨类耦合。
        """
        cfg = SystemConfig.objects.filter(key=t.config_key).first()
        is_secret = bool(cfg and cfg.is_secret)
        display_old = '***' if is_secret else t.old_value
        display_new = '***' if is_secret else t.new_value
        # change_summary 解析为对象返回，便于前端直接渲染 added/removed 列表
        change_summary = None
        if t.change_summary:
            try:
                change_summary = json.loads(t.change_summary)
            except (json.JSONDecodeError, TypeError):
                change_summary = None
        return {
            "id": t.id,
            "config_key": t.config_key,
            "config_label": t.config_label,
            "old_value": display_old,
            "new_value": display_new,
            "risk_level": t.risk_level,
            "reason": t.reason,
            "change_summary": change_summary,
            "status": t.status,
            "creator": t.creator.username if t.creator else '',
            "reviewer": t.reviewer.username if t.reviewer else '',
            "super_admin_reviewer": t.super_admin_reviewer.username if t.super_admin_reviewer else '',
            "review_comment": t.review_comment,
            "super_admin_comment": t.super_admin_comment,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
            "super_admin_reviewed_at": t.super_admin_reviewed_at.isoformat() if t.super_admin_reviewed_at else None,
            "applied_at": t.applied_at.isoformat() if t.applied_at else None,
        }

    def _write_audit(self, request, action, ticket, extra=None):
        """写工单审计日志：敏感项 old/new 掩码，action 区分各操作类型

        action 取值：create_config_ticket / approve_config_ticket /
        reject_config_ticket / withdraw_config_ticket / apply_config_ticket
        """
        try:
            from apps.audit.models import AuditLog
            cfg = SystemConfig.objects.filter(key=ticket.config_key).first()
            is_secret = bool(cfg and cfg.is_secret)
            display_old = '***' if is_secret else ticket.old_value
            display_new = '***' if is_secret else ticket.new_value
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
            logger.warning(f"写配置工单审计日志失败: {e}")

    def list(self, request):
        """工单列表，支持 status 筛选（pending/first_approved/approved/rejected/withdrawn）

        支持多个状态筛选，用逗号分隔，如 ?status=pending,first_approved。
        支持 ?creator=me 筛选当前用户创建的工单（所有状态）。
        自动过滤：
        - 发起人自己创建的工单不展示在"待审核"列表中（避免自己审自己）
        - 对于 first_approved 状态，额外过滤掉审核人（防止同一个人完成审核+复核）
        权限：system.config.read，确保只有维护管理员/超管能看工单流转情况。
        """
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied
        qs = ConfigChangeTicket.objects.all()

        # "我的工单"：按创建人筛选，展示自己创建的工单（所有状态）
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
            # 过滤：创建人不能审批自己的工单
            qs = qs.exclude(creator=request.user)
            # 过滤：复核阶段的工单，审核人不可见（防止审核+复核由同一人完成）
            qs = qs.exclude(status='first_approved', reviewer=request.user)

        qs = qs.select_related('creator', 'reviewer', 'super_admin_reviewer')
        tickets = [self._serialize_ticket(t) for t in qs]
        return Response({"tickets": tickets, "total": len(tickets)})

    def retrieve(self, request, pk=None):
        """工单详情"""
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied
        try:
            t = ConfigChangeTicket.objects.get(pk=pk)
        except ConfigChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._serialize_ticket(t))

    def create_ticket(self, request):
        """POST /config-tickets/  创建配置变更工单

        与 SystemConfigView.put 行为一致，是工单提交的主入口：
        - 校验 config_key 存在且非只读
        - 校验新值与原值不同
        - 创建 pending 工单并写审计日志
        body: {config_key, new_value, reason}
        """
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        data = request.data or {}
        config_key = (data.get('config_key') or '').strip()
        new_value = data.get('new_value', '')
        reason = (data.get('reason') or '').strip()
        if not config_key:
            return Response({"detail": "config_key 必填"}, status=status.HTTP_400_BAD_REQUEST)
        if not reason:
            return Response({"detail": "请填写变更原因"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            obj = SystemConfig.objects.get(key=config_key)
        except SystemConfig.DoesNotExist:
            return Response({"detail": f"配置项 {config_key} 不存在"}, status=status.HTTP_404_NOT_FOUND)

        # 只读项禁止提交工单（改了需重建索引或影响路由），只能改 .env 重启
        if obj.is_readonly:
            return Response({"detail": f"配置项 {config_key} 为只读项，需在 .env 中修改后重启生效"},
                            status=status.HTTP_409_CONFLICT)

        # 按类型规范化存储，避免前端传入不规范格式
        try:
            normalized = SystemConfigView()._normalize_value(new_value, obj.value_type)
        except ValueError as e:
            logger.warning(f"ConfigTicketView.put normalize failed key={config_key}: {e}")
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if normalized == obj.value:
            return Response({"detail": "新值与当前值一致，无需提交工单"}, status=status.HTTP_400_BAD_REQUEST)

        # 冗余 risk_level，避免后续 SystemConfig.risk_level 变更影响本工单审批流程
        # 多值类配置（如 BUSINESS_DB_TABLES）额外计算差异摘要，便于审批人快速识别变更点
        change_summary = SystemConfigView()._compute_change_summary(obj.key, obj.value, normalized)
        ticket = ConfigChangeTicket.objects.create(
            config_key=obj.key,
            config_label=obj.label,
            old_value=obj.value,
            new_value=normalized,
            risk_level=obj.risk_level,
            reason=reason,
            change_summary=change_summary,
            status='pending',
            creator=request.user if request.user.is_authenticated else None,
        )
        self._write_audit(
            request, action='create_config_ticket', ticket=ticket,
            extra={'reason': reason},
        )
        logger.info(f"配置变更工单已创建: ticket={ticket.id} key={config_key} by {request.user.username}")
        return Response(self._serialize_ticket(ticket), status=status.HTTP_201_CREATED)

    def approve(self, request, pk=None):
        """POST /config-tickets/<id>/approve/  审批通过

        流程：
        - pending 状态：审核。普通项直接通过并生效；高风险项进入 first_approved 待超管复核
        - first_approved 状态：超管复核（仅 is_super_admin 可操作），通过后生效
        防自审：审批人不能是创建人自己（避免单人完成创建+审批）
        """
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        try:
            ticket = ConfigChangeTicket.objects.get(pk=pk)
        except ConfigChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        comment = (request.data.get('comment') or '').strip()

        if ticket.status == 'pending':
            # 审核：防自审，审批人不能是创建人
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能审批自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            # 审核通过：高风险项进入待复核，普通项直接生效
            if ticket.risk_level == 'high':
                ticket.status = 'first_approved'
                ticket.reviewer = request.user if request.user.is_authenticated else None
                ticket.review_comment = comment
                ticket.reviewed_at = timezone.now()
                ticket.save()
                self._write_audit(
                    request, action='approve_config_ticket', ticket=ticket,
                    extra={'stage': 'first_review', 'comment': comment},
                )
                logger.info(f"工单 {ticket.id} 审核通过，等待超管复核 by {request.user.username}")
                return Response(self._serialize_ticket(ticket))
            # 普通项审核通过即生效
            ticket.reviewer = request.user if request.user.is_authenticated else None
            ticket.review_comment = comment
            ticket.reviewed_at = timezone.now()
            self._apply_config(ticket, request)
            self._write_audit(
                request, action='approve_config_ticket', ticket=ticket,
                extra={'stage': 'first_review', 'comment': comment},
            )
            logger.info(f"工单 {ticket.id} 审核通过并已生效 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        if ticket.status == 'first_approved':
            # 超管复核：仅超管可操作，且不能与审核人/创建人同一人
            if not request.user.is_super_admin:
                return Response({"detail": "高风险项复核仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能复核自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.reviewer_id and ticket.reviewer_id == request.user.id:
                return Response({"detail": "复核人不能与审核人相同"}, status=status.HTTP_403_FORBIDDEN)
            ticket.super_admin_reviewer = request.user if request.user.is_authenticated else None
            ticket.super_admin_comment = comment
            ticket.super_admin_reviewed_at = timezone.now()
            self._apply_config(ticket, request)
            self._write_audit(
                request, action='approve_config_ticket', ticket=ticket,
                extra={'stage': 'super_admin_review', 'comment': comment},
            )
            logger.info(f"工单 {ticket.id} 超管复核通过并已生效 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        # 非 pending/first_approved 状态不可审批
        return Response({"detail": f"工单当前状态 {ticket.status} 不可审批"}, status=status.HTTP_409_CONFLICT)

    def reject(self, request, pk=None):
        """POST /config-tickets/<id>/reject/  驳回

        - pending：审核驳回（需 system.config.write，且审批人 ≠ 创建人）
        - first_approved：超管复核驳回（仅 is_super_admin）
        驳回需填写原因，便于创建人了解被驳回依据。
        """
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        try:
            ticket = ConfigChangeTicket.objects.get(pk=pk)
        except ConfigChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        comment = (request.data.get('comment') or '').strip()
        if not comment:
            return Response({"detail": "请填写驳回原因"}, status=status.HTTP_400_BAD_REQUEST)

        if ticket.status == 'pending':
            # 审核驳回：防自审
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能驳回自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            ticket.status = 'rejected'
            ticket.reviewer = request.user if request.user.is_authenticated else None
            ticket.review_comment = comment
            ticket.reviewed_at = timezone.now()
            ticket.save()
            self._write_audit(
                request, action='reject_config_ticket', ticket=ticket,
                extra={'stage': 'first_review', 'comment': comment},
            )
            logger.info(f"工单 {ticket.id} 审核驳回 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        if ticket.status == 'first_approved':
            # 超管复核驳回：仅超管可操作，且不能与审核人/创建人同一人
            if not request.user.is_super_admin:
                return Response({"detail": "高风险项复核仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能驳回自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.reviewer_id and ticket.reviewer_id == request.user.id:
                return Response({"detail": "复核驳回人与审核人不能相同"}, status=status.HTTP_403_FORBIDDEN)
            ticket.status = 'rejected'
            ticket.super_admin_reviewer = request.user if request.user.is_authenticated else None
            ticket.super_admin_comment = comment
            ticket.super_admin_reviewed_at = timezone.now()
            ticket.save()
            self._write_audit(
                request, action='reject_config_ticket', ticket=ticket,
                extra={'stage': 'super_admin_review', 'comment': comment},
            )
            logger.info(f"工单 {ticket.id} 超管复核驳回 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        return Response({"detail": f"工单当前状态 {ticket.status} 不可驳回"}, status=status.HTTP_409_CONFLICT)

    def withdraw(self, request, pk=None):
        """POST /config-tickets/<id>/withdraw/  创建人撤回

        仅创建人本人可操作，且仅 pending/first_approved 状态可撤回；
        已通过/已驳回的工单不可撤回（已生效或已终结）。
        """
        try:
            ticket = ConfigChangeTicket.objects.get(pk=pk)
        except ConfigChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        # 仅创建人可撤回，防止他人撤回非自己提交的工单
        if not ticket.creator_id or ticket.creator_id != request.user.id:
            return Response({"detail": "仅创建人可撤回工单"}, status=status.HTTP_403_FORBIDDEN)
        if ticket.status not in ('pending', 'first_approved'):
            return Response({"detail": f"工单当前状态 {ticket.status} 不可撤回"}, status=status.HTTP_409_CONFLICT)

        comment = (request.data.get('comment') or '').strip()
        ticket.status = 'withdrawn'
        # 撤回时把意见写到 review_comment，便于审计追溯撤回原因
        if comment:
            ticket.review_comment = comment
        ticket.save()
        self._write_audit(
            request, action='withdraw_config_ticket', ticket=ticket,
            extra={'comment': comment},
        )
        logger.info(f"工单 {ticket.id} 已撤回 by {request.user.username}")
        return Response(self._serialize_ticket(ticket))

    def _apply_config(self, ticket, request):
        """工单通过后写入 SystemConfig + 审计日志

        用事务保证工单状态与配置写入的一致性：要么都成功，要么都回滚，
        避免出现"工单已通过但配置没写进去"或"配置改了但工单状态没更新"的不一致。
        写库后立即失效 SystemConfig 缓存（延迟双删），保证业务侧下次读到新值。
        """
        with transaction.atomic():
            # 重新锁行查询，防止并发审批重复生效
            cfg = SystemConfig.objects.select_for_update().get(key=ticket.config_key)
            old_value = cfg.value
            cfg.value = ticket.new_value
            cfg.updated_by = ticket.creator
            cfg.save()
            # 工单置为 approved 并记录生效时间，与配置写入同事务保证一致
            ticket.status = 'approved'
            ticket.applied_at = timezone.now()
            ticket.save()
        # 事务提交后再清缓存，避免清缓存后另一线程读到旧值回填
        # 仅清当前 key，影响范围可控；延迟双删兜底并发读旧值窗口
        try:
            from .config_loader import invalidate_config_cache
            invalidate_config_cache(ticket.config_key)
        except Exception as e:
            # 缓存失效失败不阻断审批流程，5min TTL 兜底最终一致
            logger.warning(f"失效 SystemConfig 缓存失败 key={ticket.config_key}: {e}")
        # 单独写 apply 审计日志（事务外，避免审计哈希链计算拖长事务）
        self._write_audit(
            request, action='apply_config_ticket', ticket=ticket,
            extra={'applied_old': old_value, 'applied_new': ticket.new_value},
        )


class ModelChangeTicketViewSet(viewsets.ViewSet):
    """模型变更工单管理

    工单流转：
    - GET  /model-tickets/           工单列表（支持 status/operation 筛选）
    - GET  /model-tickets/<id>/      工单详情
    - POST /model-tickets/<id>/approve/  审批通过
    - POST /model-tickets/<id>/reject/   驳回
    - POST /model-tickets/<id>/withdraw/ 创建人撤回

    权限：
    - list/retrieve：system.config.read
    - approve/reject：system.config.write，且审批人 ≠ 创建人
    - delete 操作的超管复核：仅 is_super_admin 可操作
    - withdraw：仅创建人本人可操作
    """
    permission_classes = [IsAuthenticated]

    def _check_perm(self, request, perm):
        if not request.user.has_perm(perm):
            return Response(
                {"detail": "无权限执行此操作，需要 " + perm + " 权限"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _serialize_ticket(self, t):
        """序列化工单：供前端渲染审批详情"""
        model_name = ''
        model_id = t.target_model_id
        if t.target_model_id:
            m = LLMModel.objects.filter(id=t.target_model_id).first()
            if m:
                model_name = f'{m.name} ({m.model_name})'
        # changed_fields 存储格式: {field: {old, new}}，前端需要 list of field names + change_data
        changed_field_names = list(t.changed_fields.keys()) if isinstance(t.changed_fields, dict) else []
        # 非修改操作（删除/停用）时，从快照构造展示数据
        snapshot = t.target_model_snapshot if isinstance(t.target_model_snapshot, dict) else {}
        if t.operation == 'delete':
            # 删除：展示当前模型关键信息作为"原值"
            changed_field_names = ['name', 'model_name', 'model_type', 'base_url', 'timeout']
        elif t.operation == 'deactivate':
            changed_field_names = ['is_active']
        return {
            "id": t.id,
            "model_id": model_id,
            "model_name": model_name,
            "target_model_id": t.target_model_id,
            "target_model_name": model_name,
            "target_model_snapshot": snapshot,
            "action": t.operation,
            "operation": t.operation,
            "operation_display": dict(ModelChangeTicket.OPERATION_CHOICES).get(t.operation, t.operation),
            "changed_fields": changed_field_names,
            "change_data": t.changed_fields if isinstance(t.changed_fields, dict) else {},
            # 快照中的模型当前值，供前端展示删除/停用时的"原值"
            "snapshot_data": {
                'name': snapshot.get('name', ''),
                'model_name': snapshot.get('model_name', ''),
                'model_type': snapshot.get('model_type', ''),
                'provider': snapshot.get('provider', ''),
                'base_url': snapshot.get('base_url', ''),
                'timeout': snapshot.get('timeout', ''),
                'is_active': snapshot.get('is_active', True),
            } if snapshot else {},
            "dependency_refs": t.dependency_refs,
            "risk_level": t.risk_level,
            "reason": t.reason,
            "status": t.status,
            "status_display": dict(ModelChangeTicket.STATUS_CHOICES).get(t.status, t.status),
            "creator": t.creator.username if t.creator else '',
            "reviewer": t.reviewer.username if t.reviewer else '',
            "super_admin_reviewer": t.super_admin_reviewer.username if t.super_admin_reviewer else '',
            "review_comment": t.review_comment,
            "super_admin_comment": t.super_admin_comment,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
            "super_admin_reviewed_at": t.super_admin_reviewed_at.isoformat() if t.super_admin_reviewed_at else None,
            "applied_at": t.applied_at.isoformat() if t.applied_at else None,
        }

    def _write_audit(self, request, action, ticket, extra=None):
        try:
            from apps.audit.models import AuditLog
            detail = {
                'ticket_id': ticket.id,
                'operation': ticket.operation,
                'model_id': ticket.target_model_id,
                'status': ticket.status,
            }
            if extra:
                detail.update(extra)
            AuditLog.objects.create(
                actor=request.user,
                actor_username=request.user.username,
                action=action,
                action_category='config',
                target_type='model_ticket',
                target_id=str(ticket.id),
                result='success',
                detail=detail,
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:256],
                method=request.method,
                path=request.path,
            )
        except Exception as e:
            logger.warning(f"写模型工单审计日志失败: {e}")

    def list(self, request):
        """工单列表，支持 status 和 operation 筛选

        支持多个状态筛选，用逗号分隔，如 ?status=pending,first_approved。
        支持 ?creator=me 筛选当前用户创建的工单（所有状态）。
        自动过滤：
        - 发起人自己创建的工单不展示在"待审核"列表中（避免自己审自己）
        - 对于 first_approved 状态，额外过滤掉审核人（防止同一人完成审核+复核）。
        """
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied
        qs = ModelChangeTicket.objects.all()

        # "我的工单"：按创建人筛选，展示自己创建的工单（所有状态）
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
            op_filter = (request.query_params.get('operation') or '').strip()
            if op_filter and op_filter != 'all':
                qs = qs.filter(operation=op_filter)
            # 过滤：创建人不能审批自己的工单
            qs = qs.exclude(creator=request.user)
            # 过滤：复核阶段的工单，审核人不可见（防止审核+复核由同一人完成）
            qs = qs.exclude(status='first_approved', reviewer=request.user)

        qs = qs.select_related('creator', 'reviewer', 'super_admin_reviewer', 'target_model')
        tickets = [self._serialize_ticket(t) for t in qs]
        return Response({"tickets": tickets, "total": len(tickets)})

    def retrieve(self, request, pk=None):
        """工单详情"""
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied
        try:
            t = ModelChangeTicket.objects.get(pk=pk)
        except ModelChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._serialize_ticket(t))

    def approve(self, request, pk=None):
        """审批通过

        - pending：审核。普通项直接生效；delete(high) 进入 first_approved 待超管复核
        - first_approved：超管复核（仅 is_super_admin 可操作），通过后执行删除
        """
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        try:
            ticket = ModelChangeTicket.objects.get(pk=pk)
        except ModelChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        comment = (request.data.get('comment') or '').strip()

        if ticket.status == 'pending':
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能审批自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)

            if ticket.risk_level == 'high':
                # 高风险（删除）：审核通过后进入待超管复核
                # 双保险：审核时也重新检查依赖，防止创建后到审批前被引用
                if ticket.operation == 'delete' and ticket.target_model_id:
                    refs = self._check_model_dependency_for_id(ticket.target_model_id)
                    if refs:
                        return Response(
                            {"detail": f"模型已被 {', '.join(refs)} 引用，无法进入复核"},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                ticket.status = 'first_approved'
                ticket.reviewer = request.user if request.user.is_authenticated else None
                ticket.review_comment = comment
                ticket.reviewed_at = timezone.now()
                # 更新依赖快照（审批时重新检查，更新到工单上）
                if ticket.operation in ('delete', 'deactivate') and ticket.target_model_id:
                    ticket.dependency_refs = self._check_model_dependency_for_id(ticket.target_model_id)
                ticket.save()
                self._write_audit(request, 'approve_model_ticket', ticket,
                                  extra={'stage': 'first_review', 'comment': comment})
                logger.info(f"模型工单 {ticket.id} 审核通过，等待超管复核 by {request.user.username}")
                return Response(self._serialize_ticket(ticket))

            # 普通项（修改/停用）：审核通过即生效
            # 停用操作：审核时也先快速检查依赖，防止创建后到审批前被引用
            if ticket.operation == 'deactivate' and ticket.target_model_id:
                refs = self._check_model_dependency_for_id(ticket.target_model_id)
                if refs:
                    return Response(
                        {"detail": f"模型已被 {', '.join(refs)} 引用，无法停用"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                # 更新依赖快照
                ticket.dependency_refs = refs
            ticket.reviewer = request.user if request.user.is_authenticated else None
            ticket.review_comment = comment
            ticket.reviewed_at = timezone.now()
            result = self._apply_ticket(ticket, request)
            if isinstance(result, Response):
                return result
            self._write_audit(request, 'approve_model_ticket', ticket,
                              extra={'stage': 'first_review', 'comment': comment})
            logger.info(f"模型工单 {ticket.id} 审核通过并已生效 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        if ticket.status == 'first_approved':
            # 超管复核：仅超管可操作，且不能与审核人/创建人同一人
            if not request.user.is_super_admin:
                return Response({"detail": "删除操作复核仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能复核自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.reviewer_id and ticket.reviewer_id == request.user.id:
                return Response({"detail": "复核人不能与审核人相同"}, status=status.HTTP_403_FORBIDDEN)
            ticket.super_admin_reviewer = request.user if request.user.is_authenticated else None
            ticket.super_admin_comment = comment
            ticket.super_admin_reviewed_at = timezone.now()
            result = self._apply_ticket(ticket, request)
            if isinstance(result, Response):
                return result
            self._write_audit(request, 'approve_model_ticket', ticket,
                              extra={'stage': 'super_admin_review', 'comment': comment})
            logger.info(f"模型工单 {ticket.id} 超管复核通过并已生效 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        return Response({"detail": f"工单当前状态 {ticket.status} 不可审批"}, status=status.HTTP_409_CONFLICT)

    def reject(self, request, pk=None):
        """驳回"""
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        try:
            ticket = ModelChangeTicket.objects.get(pk=pk)
        except ModelChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        comment = (request.data.get('comment') or '').strip()
        if not comment:
            return Response({"detail": "请填写驳回原因"}, status=status.HTTP_400_BAD_REQUEST)

        if ticket.status == 'pending':
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能驳回自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            ticket.status = 'rejected'
            ticket.reviewer = request.user if request.user.is_authenticated else None
            ticket.review_comment = comment
            ticket.reviewed_at = timezone.now()
            ticket.save()
            self._write_audit(request, 'reject_model_ticket', ticket,
                              extra={'stage': 'first_review', 'comment': comment})
            logger.info(f"模型工单 {ticket.id} 审核驳回 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        if ticket.status == 'first_approved':
            # 超管复核驳回：仅超管可操作，且不能与审核人/创建人同一人
            if not request.user.is_super_admin:
                return Response({"detail": "删除操作复核仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能驳回自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            if ticket.reviewer_id and ticket.reviewer_id == request.user.id:
                return Response({"detail": "复核驳回人与审核人不能相同"}, status=status.HTTP_403_FORBIDDEN)
            ticket.status = 'rejected'
            ticket.super_admin_reviewer = request.user if request.user.is_authenticated else None
            ticket.super_admin_comment = comment
            ticket.super_admin_reviewed_at = timezone.now()
            ticket.save()
            self._write_audit(request, 'reject_model_ticket', ticket,
                              extra={'stage': 'super_admin_review', 'comment': comment})
            logger.info(f"模型工单 {ticket.id} 超管复核驳回 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        return Response({"detail": f"工单当前状态 {ticket.status} 不可驳回"}, status=status.HTTP_409_CONFLICT)

    def withdraw(self, request, pk=None):
        """创建人撤回"""
        try:
            ticket = ModelChangeTicket.objects.get(pk=pk)
        except ModelChangeTicket.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=status.HTTP_404_NOT_FOUND)

        if not ticket.creator_id or ticket.creator_id != request.user.id:
            return Response({"detail": "仅创建人可撤回工单"}, status=status.HTTP_403_FORBIDDEN)
        if ticket.status not in ('pending', 'first_approved'):
            return Response({"detail": f"工单当前状态 {ticket.status} 不可撤回"}, status=status.HTTP_409_CONFLICT)

        comment = (request.data.get('comment') or '').strip()
        ticket.status = 'withdrawn'
        if comment:
            ticket.review_comment = comment
        ticket.save()
        self._write_audit(request, 'withdraw_model_ticket', ticket,
                          extra={'comment': comment})
        logger.info(f"模型工单 {ticket.id} 已撤回 by {request.user.username}")
        return Response(self._serialize_ticket(ticket))

    def _apply_ticket(self, ticket, request):
        """工单通过后执行模型变更

        操作类型：
        - update_normal：修改字段（base_url/model_name/provider/timeout/model_type）
        - deactivate：设置 is_active=False
        - delete：删除模型
        用事务保证工单状态与模型变更的一致性。
        审批时重新检查依赖，防止工单创建后到审批前模型被其他配置引用。
        """
        with transaction.atomic():
            ticket.status = 'approved'
            ticket.applied_at = timezone.now()

            if ticket.operation == 'delete':
                # 删除模型：先检查依赖（双保险，创建时+审核时均已检查）
                if ticket.target_model_id:
                    model = LLMModel.objects.select_for_update().filter(id=ticket.target_model_id).first()
                    if model:
                        refs = self._check_model_dependency_for_id(model.id)
                        if refs:
                            # 有依赖则回滚工单状态，拒绝生效
                            ticket.status = 'pending'
                            ticket.applied_at = None
                            ticket.save()
                            return Response(
                                {"detail": f"审批期间模型被 {', '.join(refs)} 引用，操作已回滚"},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        # 先断开所有工单与模型的关联，避免外键约束阻止删除
                        ModelChangeTicket.objects.filter(target_model=model).update(target_model=None)
                        model.delete()
                ticket.target_model = None
                ticket.save()
            elif ticket.operation == 'deactivate':
                # 停用：重新检查依赖，防止审批期间被引用
                if ticket.target_model_id:
                    model = LLMModel.objects.select_for_update().filter(id=ticket.target_model_id).first()
                    if model:
                        refs = self._check_model_dependency_for_id(model.id)
                        if refs:
                            ticket.status = 'pending'
                            ticket.applied_at = None
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
                    if model:
                        for field, changes in ticket.changed_fields.items():
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
        self._write_audit(request, 'apply_model_ticket', ticket)

    def _check_model_dependency_for_id(self, model_id):
        """按模型 ID 检查依赖"""
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
