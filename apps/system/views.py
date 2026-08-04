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

from apps.system.models import SystemConfig, LLMModel, ConfigChangeTicket

User = get_user_model()


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
            try:
                c = SystemConfig.objects.get(key=key)
            except SystemConfig.DoesNotExist:
                return Response({"detail": "not found"}, status=404)
            return Response(self._ser(c))
        # 列表按 category 分组返回，方便前端按 tab 渲染
        rows = list(SystemConfig.objects.all().order_by("category", "key"))

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
        - 普通项：一审通过后生效
        - 高风险项：一审 + 超管终审通过后生效
        这样可以避免单人误改造成线上故障，并保留完整审批链路用于审计追溯。
        """
        # 权限：超级管理员或持有 system.config.write 权限（维护管理员）
        if not request.user.has_perm('system.config.write'):
            return Response({"detail": "仅超级管理员或维护管理员可修改系统配置"}, status=403)
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
        new_value = self._normalize_value(value, obj.value_type)
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
        """计算多值类配置的差异摘要（仅 BUSINESS_DB_TABLES 等逗号分隔多值项）

        Returns: JSON 字符串 {added:[...], removed:[...]}；非多值项返回空串
        - added: 新值中存在但旧值中不存在的项
        - removed: 旧值中存在但新值中不存在的项
        审批人据此快速识别本次变更点，无需逐项对比新旧完整列表
        """
        # 仅多值类配置计算差异；单值配置（如 LLM_TIMEOUT）返回空，避免噪声
        multi_value_keys = {'BUSINESS_DB_TABLES'}
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
        """按 value_type 规范化为字符串存储"""
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
                "category": c.category, "updated_at": c.updated_at.isoformat()}


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
        groups = {}
        for m in rows:
            groups.setdefault(m.model_type, []).append(self._ser(m))
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
            # 唯一约束冲突等数据库错误，统一转 400 让前端可友好提示
            return Response({"detail": f"创建失败：{e}"}, status=status.HTTP_400_BAD_REQUEST)
        # 新增模型可能被同名调用方命中，全清 LLMModel 缓存避免脏读
        self._invalidate_llm_cache()
        self._write_audit(request, 'create', obj, None, self._ser(obj))
        logger.info(f"LLMModel.create by {request.user.username}: {obj}")
        return Response(self._ser(obj), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        obj = self.get_object()
        data = request.data or {}
        before = self._ser(obj)
        # 统一字段校验 + 规范化；is_create=False 表示不校验必填，只校验传入字段的合法性
        err, norm = self._validate_model_payload(data, is_create=False)
        if err:
            return err
        # 仅当字段在请求中显式传入才更新（区分"未传"和"传空字符串"两种意图）
        if 'name' in norm:
            obj.name = norm['name']
        if 'provider' in norm:
            obj.provider = norm['provider']
        if 'model_type' in data:
            obj.model_type = data['model_type']
        if 'base_url' in norm:
            obj.base_url = norm['base_url']
        if 'model_name' in norm:
            obj.model_name = norm['model_name']
        if 'timeout' in norm:
            obj.timeout = norm['timeout']
        if 'is_active' in norm:
            obj.is_active = norm['is_active']
        try:
            obj.save()
        except Exception as e:
            return Response({"detail": f"更新失败：{e}"}, status=status.HTTP_400_BAD_REQUEST)
        # 更新可能影响 base_url/timeout/model_name/is_active，全清 LLMModel 缓存
        # model_name 改动会让旧缓存 key 残留，全清更安全
        self._invalidate_llm_cache()
        self._write_audit(request, 'update', obj, before, self._ser(obj))
        logger.info(f"LLMModel.update by {request.user.username}: {obj}")
        return Response(self._ser(obj))

    def partial_update(self, request, *args, **kwargs):
        """PATCH 部分更新，复用 update 逻辑"""
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = self._check_perm(request, 'system.config.write')
        if denied:
            return denied
        obj = self.get_object()
        before = self._ser(obj)
        try:
            obj.delete()
        except Exception as e:
            return Response({"detail": f"删除失败：{e}"}, status=status.HTTP_400_BAD_REQUEST)
        # 删除后业务侧调用应回退到 env，全清 LLMModel 缓存
        self._invalidate_llm_cache()
        self._write_audit(request, 'delete', obj, before, None)
        logger.info(f"LLMModel.delete by {request.user.username}: {before}")
        return Response(status=status.HTTP_204_NO_CONTENT)

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
    - POST /config-tickets/<id>/approve/  审批通过（普通项直接生效；高风险项进入待终审）
    - POST /config-tickets/<id>/reject/   驳回（含一审/超管终审两种场景）
    - POST /config-tickets/<id>/withdraw/ 创建人撤回

    权限模型：
    - list/retrieve：system.config.read（维护管理员/超管可看）
    - create_ticket/approve/reject：system.config.write，且审批人 ≠ 创建人（防自审）
    - 高风险项超管终审：仅 is_super_admin 可操作
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

        权限：system.config.read，确保只有维护管理员/超管能看工单流转情况。
        """
        denied = self._check_perm(request, 'system.config.read')
        if denied:
            return denied
        qs = ConfigChangeTicket.objects.all()
        # status 筛选：不传则返回全部，传 all 也返回全部（前端"全部"选项）
        status_filter = (request.query_params.get('status') or '').strip()
        if status_filter and status_filter != 'all':
            qs = qs.filter(status=status_filter)
        # 仅取必要字段，避免序列化时反复查 creator/reviewer
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
        normalized = SystemConfigView()._normalize_value(new_value, obj.value_type)
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
        - pending 状态：一审。普通项直接通过并生效；高风险项进入 first_approved 待超管终审
        - first_approved 状态：超管终审（仅 is_super_admin 可操作），通过后生效
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
            # 一审：防自审，审批人不能是创建人
            if ticket.creator_id and ticket.creator_id == request.user.id:
                return Response({"detail": "不能审批自己创建的工单"}, status=status.HTTP_403_FORBIDDEN)
            # 一审通过：高风险项进入待终审，普通项直接生效
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
                logger.info(f"工单 {ticket.id} 一审通过，等待超管终审 by {request.user.username}")
                return Response(self._serialize_ticket(ticket))
            # 普通项一审通过即生效
            ticket.reviewer = request.user if request.user.is_authenticated else None
            ticket.review_comment = comment
            ticket.reviewed_at = timezone.now()
            self._apply_config(ticket, request)
            self._write_audit(
                request, action='approve_config_ticket', ticket=ticket,
                extra={'stage': 'first_review', 'comment': comment},
            )
            logger.info(f"工单 {ticket.id} 一审通过并已生效 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        if ticket.status == 'first_approved':
            # 超管终审：仅超管可操作，确保高风险项有二次把关
            if not request.user.is_super_admin:
                return Response({"detail": "高风险项终审仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            ticket.super_admin_reviewer = request.user if request.user.is_authenticated else None
            ticket.super_admin_comment = comment
            ticket.super_admin_reviewed_at = timezone.now()
            self._apply_config(ticket, request)
            self._write_audit(
                request, action='approve_config_ticket', ticket=ticket,
                extra={'stage': 'super_admin_review', 'comment': comment},
            )
            logger.info(f"工单 {ticket.id} 超管终审通过并已生效 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        # 非 pending/first_approved 状态不可审批
        return Response({"detail": f"工单当前状态 {ticket.status} 不可审批"}, status=status.HTTP_409_CONFLICT)

    def reject(self, request, pk=None):
        """POST /config-tickets/<id>/reject/  驳回

        - pending：一审驳回（需 system.config.write，且审批人 ≠ 创建人）
        - first_approved：超管终审驳回（仅 is_super_admin）
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
            # 一审驳回：防自审
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
            logger.info(f"工单 {ticket.id} 一审驳回 by {request.user.username}")
            return Response(self._serialize_ticket(ticket))

        if ticket.status == 'first_approved':
            # 超管终审驳回：仅超管可操作
            if not request.user.is_super_admin:
                return Response({"detail": "高风险项终审仅超级管理员可操作"}, status=status.HTTP_403_FORBIDDEN)
            ticket.status = 'rejected'
            ticket.super_admin_reviewer = request.user if request.user.is_authenticated else None
            ticket.super_admin_comment = comment
            ticket.super_admin_reviewed_at = timezone.now()
            ticket.save()
            self._write_audit(
                request, action='reject_config_ticket', ticket=ticket,
                extra={'stage': 'super_admin_review', 'comment': comment},
            )
            logger.info(f"工单 {ticket.id} 超管终审驳回 by {request.user.username}")
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
