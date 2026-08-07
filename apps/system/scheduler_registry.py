"""定时任务注册表 —— 管理端可配置的 Beat 任务清单（单一数据源）

背景：celery beat 的调度时间此前写死在 rag_project/celery.py，调整调度需改代码并重启，
且无审批与审计留痕。本模块把"哪些任务可配置 + 默认调度时间"收敛为唯一来源，供四处消费：

- init_system：将每个任务写入 SystemConfig（key 形如 SCHEDULE_<NAME>，值为 JSON
  {"cron": "分 时 日 月 周", "enabled": bool}），并标记为高风险（变更需审核 + 超管复核），
  默认值与历史 celery.py 硬编码完全一致，保证"页面不调整时行为不变"。
- rag_project/celery.py：用注册表默认值构建 beat_schedule（DB 不可用时兜底）。
- SystemConfigScheduler：运行期每个 tick 读取 SystemConfig 最新值动态重建调度，
  实现"工单审批通过后无需重启 beat 即生效"。
- 管理端定时任务页 API：返回任务清单 + 当前值，前端据此渲染与提交变更工单。

cron 格式：5 段"分 时 日 月 周"，与 celery crontab 语义一致；
day_of_week 取值 0-6，0 表示周日（celery 的 day_of_week=1 即周一）。
"""
import json
from typing import Dict, List

from celery.schedules import crontab
from loguru import logger


# SystemConfig key 前缀：所有可配置调度任务统一以 SCHEDULE_ 开头，
# 便于 API 层识别并过滤出独立管理页面，同时让 PUT 校验走调度专属逻辑
SCHEDULE_KEY_PREFIX = 'SCHEDULE_'

# 调度类配置的风险等级：定时任务影响生产批量作业与成本（如 LLM 评估任务），
# 变更统一走"审核 + 超管复核"，与存储模式/邮件开关等高危项同等对待
SCHEDULE_RISK_LEVEL = 'high'

# 调度类配置在管理页的分组（与前端 tab 对应）
SCHEDULE_CATEGORY = 'schedule'


def schedule_key(name: str) -> str:
    """任务名 → SystemConfig key（SCHEDULE_<NAME>）

    name 为 beat_schedule 条目名（含连字符），转大写后作为 key 的一部分，
    保证 key 全局唯一且与任务一一对应。
    """
    return SCHEDULE_KEY_PREFIX + name.upper()


def is_schedule_key(key: str) -> bool:
    """判断是否为调度类配置 key（以 SCHEDULE_ 前缀开头）"""
    return bool(key) and key.startswith(SCHEDULE_KEY_PREFIX)


# ---------------------------------------------------------------------------
# 任务注册表：name（beat 条目名）/ task（celery 任务路径）/ cron（默认 5 段表达式）
#            / estimated_minutes（预估工时，用于管理页忙闲视图，含 20% 缓冲展示）
# TODO: 忙闲视图后续按近一周/一个月实际执行耗时均值 + 10% 余量动态估算，
#       替代当前静态的 estimated_minutes（当前仅作展示用估算，不参与调度）。
# 默认 cron 与历史 celery.py 硬编码一致，禁止在无评审的情况下随意调整
# ---------------------------------------------------------------------------
SCHEDULED_TASKS: List[dict] = [
    {
        'name': 'system-metrics-daily',
        'task': 'apps.analytics.tasks.compute_system_metrics_daily',
        'cron': '0 2 * * *',
        'enabled': True,
        'label': '系统指标日聚合',
        'description': '聚合前一天系统指标（P50/P95/P99、缓存命中率、错误率等）',
    },
    {
        'name': 'org-usage-daily',
        'task': 'apps.analytics.tasks.compute_org_usage_daily',
        'cron': '10 2 * * *',
        'enabled': True,
        'estimated_minutes': 10,
        'label': '组织使用数据日聚合',
        'description': '聚合前一天部门/团队对话、Token、费用（UPSERT）',
    },
    {
        'name': 'queue-depth-snapshot',
        'task': 'apps.analytics.tasks.update_queue_depth_snapshot',
        'cron': '*/5 * * * *',
        'enabled': True,
        'label': '队列深度快照',
        'description': '更新 Celery 队列深度快照（PG 历史 + Redis 实时）',
    },
    {
        'name': 'realtime-metrics-flush',
        'task': 'apps.analytics.tasks.flush_realtime_metrics_task',
        'cron': '*/5 * * * *',
        'enabled': True,
        'estimated_minutes': 1,
        'label': '实时指标刷新',
        'description': '刷新实时指标时间戳',
    },
    {
        'name': 'expire-ip-blacklist',
        'task': 'apps.security.tasks.expire_ip_blacklist',
        'cron': '*/5 * * * *',
        'enabled': True,
        'label': 'IP 封禁过期清理',
        'description': '清理过期临时 IP 封禁',
    },
    {
        'name': 'refine-user-memory',
        'task': 'apps.memory.tasks.refine_user_memory',
        'cron': '30 2 * * *',
        'enabled': True,
        'label': '用户长期记忆提炼',
        'description': '提炼稳定的用户偏好到长期记忆',
    },
    {
        'name': 'handle-feedback',
        'task': 'apps.chat.tasks.handle_feedback',
        'cron': '15 * * * *',
        'enabled': True,
        'estimated_minutes': 5,
        'label': '差评反馈处理',
        'description': '处理未处理的差评反馈',
    },
    {
        'name': 'cleanup-old-analytics-data',
        'task': 'apps.analytics.tasks.cleanup_old_data',
        'cron': '30 3 * * *',
        'enabled': True,
        'label': '监控数据清理',
        'description': '清理过期监控数据（低峰期）',
    },
    {
        'name': 'doc-quality-daily',
        'task': 'apps.analytics.tasks.batch_evaluate_document_quality',
        'cron': '0 4 * * *',
        'enabled': True,
        'estimated_minutes': 60,
        'label': '文档质量日评估',
        'description': '批量评估文档质量（解析/切分/向量化，LLM-as-Judge 成本高）',
    },
    {
        'name': 'coverage-report-daily',
        'task': 'apps.analytics.tasks.generate_coverage_report_daily',
        'cron': '30 4 * * *',
        'enabled': True,
        'estimated_minutes': 30,
        'label': '知识库覆盖率报告',
        'description': '生成知识库覆盖率报告',
    },
    {
        'name': 'multi-dim-evaluation',
        'task': 'apps.analytics.tasks.run_multi_dimension_evaluation',
        'cron': '30 */2 * * *',
        'enabled': True,
        'estimated_minutes': 30,
        'label': '多维度回答质量评估',
        'description': '多维度回答质量评估（DeepEval 12 维，回扫未覆盖项）',
    },
    {
        'name': 'periodic-retrieval-eval',
        'task': 'apps.analytics.tasks.periodic_retrieval_evaluation',
        'cron': '0 5 * * 1',
        'enabled': True,
        'label': '离线检索回归评估',
        'description': '离线检索评估（黄金测试集回归测试）',
    },
    {
        'name': 'siphon-low-score-regression',
        'task': 'apps.analytics.tasks.siphon_low_score_regression',
        'cron': '30 5 * * *',
        'enabled': True,
        'estimated_minutes': 20,
        'label': '低分对话沉淀',
        'description': '从生产低分对话沉淀到回归测试集（低峰期）',
    },
    {
        'name': 'run-regression-evaluation',
        'task': 'apps.analytics.tasks.run_regression_evaluation_task',
        'cron': '0 6 * * 1',
        'enabled': True,
        'label': '回归测试集全链路评估',
        'description': '对低分回归测试集执行全链路评估（成本高，与检索评估错开 1h）',
    },
    {
        'name': 'graph-community-detection',
        'task': 'apps.graph.tasks.community_detection_task',
        'cron': '0 3 * * *',
        'enabled': True,
        'estimated_minutes': 60,
        'label': '图谱社区检测',
        'description': '社区检测 + 摘要生成（低峰期，图谱增量后整体重建社区）',
    },
    {
        'name': 'route-analysis-daily',
        'task': 'apps.analytics.tasks.aggregate_route_analysis_daily',
        'cron': '50 2 * * *',
        'enabled': True,
        'estimated_minutes': 10,
        'label': '路由决策分析日聚合',
        'description': '聚合前一天路由决策分析（四层命中率/置信度/延迟，依赖 QaRecord 落库）',
    },
    {
        'name': 'wiki-quality-daily',
        'task': 'apps.analytics.tasks.batch_evaluate_wiki_quality',
        'cron': '45 4 * * *',
        'enabled': True,
        'estimated_minutes': 60,
        'label': 'Wiki 页面质量评估',
        'description': '批量评估 Wiki 页面质量（忠实度/完整性，LLM-as-Judge 成本高）',
    },
    {
        'name': 'wiki-refresh-expired',
        'task': 'apps.wiki.tasks.refresh_expired_wiki_pages',
        'cron': '0 4 * * *',
        'enabled': True,
        'label': '过期 Wiki 刷新',
        'description': '刷新过期的 Wiki 页面（文档变更后被标记 expired，重新生成）',
    },
]

# ---------------------------------------------------------------------------
# cron 解析与校验（5 段：分 时 日 月 周）
# ---------------------------------------------------------------------------
# 各段取值区间：分 0-59 / 时 0-23 / 日 1-31 / 月 1-12 / 周 0-6（0=周日）
_CRON_RANGES = {
    'minute': (0, 59),
    'hour': (0, 23),
    'day_of_month': (1, 31),
    'month': (1, 12),
    'day_of_week': (0, 6),
}


def validate_cron(cron: str) -> None:
    """校验 5 段 cron 表达式的语法与取值范围，非法时抛出 ValueError

    支持 '*'、固定值、区间（a-b）、步长（*/n、a/n、a-b/n）及逗号组合，
    与 celery crontab 的解析能力保持一致；周取值范围 0-6（0=周日）。
    """
    fields = str(cron or '').strip().split()
    if len(fields) != 5:
        raise ValueError(f'cron 表达式需为 5 段"分 时 日 月 周"，当前为 {len(fields)} 段: {cron!r}')
    names = ['minute', 'hour', 'day_of_month', 'month', 'day_of_week']
    for name, value in zip(names, fields):
        _validate_field(value, *_CRON_RANGES[name], name)


def _validate_field(value: str, lo: int, hi: int, name: str) -> None:
    """校验 cron 单段字段：* / 固定值 / 区间 / 步长 / 逗号组合"""
    for part in value.split(','):
        part = part.strip()
        if not part:
            raise ValueError(f'{name} 存在空段: {value!r}')
        if '/' in part:
            base, step = part.split('/', 1)
            if not step.isdigit() or int(step) < 1:
                raise ValueError(f'{name} 步长非法（需为正整数）: {part!r}')
        else:
            base = part
        if base == '*':
            continue
        if '-' in base:
            start_s, end_s = base.split('-', 1)
            if not (start_s.lstrip('-').isdigit() and end_s.lstrip('-').isdigit()):
                raise ValueError(f'{name} 区间非法: {part!r}')
            start, end = int(start_s), int(end_s)
            if not (lo <= start <= end <= hi):
                raise ValueError(f'{name} 区间超出范围 [{lo}-{hi}]: {part!r}')
        else:
            if not base.lstrip('-').isdigit() or not (lo <= int(base) <= hi):
                raise ValueError(f'{name} 取值超出范围 [{lo}-{hi}]: {part!r}')


def parse_cron_fields(cron: str) -> Dict[str, str]:
    """把 5 段 cron 拆分为字段字典，供前端表单回显

    Returns: {'minute','hour','day_of_month','month','day_of_week'}
    """
    validate_cron(cron)
    fields = cron.strip().split()
    return {
        'minute': fields[0],
        'hour': fields[1],
        'day_of_month': fields[2],
        'month': fields[3],
        'day_of_week': fields[4],
    }


# ---------------------------------------------------------------------------
# cron 中文解释（人性化展示）
# ---------------------------------------------------------------------------
# 星期中文名：cron 中 0=周日，1~6=周一~周六
_WEEKDAY_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']


def _is_fixed_field(value: str) -> bool:
    """判断 cron 单段是否为固定值（纯数字，如 '5'）"""
    return value.isdigit()


def _is_step_field(value: str) -> bool:
    """判断 cron 单段是否为步长形式（*/N 或 a/N），返回 True"""
    return '/' in value


def _step_value(value: str) -> int:
    """提取步长形式的 N（如 '*/2' → 2）"""
    return int(value.split('/', 1)[1])


def _fmt_hhmm(hour: str, minute: str) -> str:
    """固定小时/分钟格式化为 HH:MM（如 '2','5' → '02:05'）"""
    return f'{int(hour):02d}:{int(minute):02d}'


def _fmt_weekdays(dow: str) -> str:
    """把周字段翻译为中文星期列表（支持固定值/区间/逗号列表）

    如 '1' → '周一'；'1,3,5' → '周一、周三、周五'；'1-5' → '周一、周二、周三、周四、周五'
    """
    names = []
    for item in dow.split(','):
        item = item.strip()
        if '-' in item:
            start_s, end_s = item.split('-', 1)
            for i in range(int(start_s), int(end_s) + 1):
                names.append(_WEEKDAY_NAMES[i % 7])
        elif item != '*':
            names.append(_WEEKDAY_NAMES[int(item) % 7])
    return '、'.join(names) if names else dow


def humanize_cron(cron: str) -> str:
    """把 5 段 cron 表达式翻译为中文可读描述，供页面展示与审批摘要

    覆盖常见模式（固定时间/步长/每周/每月/每年）：
    - '0 2 * * *'   → '每天 02:00 执行一次'
    - '*/5 * * * *' → '每 5 分钟执行一次'
    - '*/5 1 * * *' → '每天 01 点内每 5 分钟执行一次'
    - '15 * * * *'  → '每小时的第 15 分钟执行一次'
    - '0 */2 * * *' → '每 2 小时执行一次'
    - '30 */2 * * *' → '每 2 小时的第 30 分钟执行一次'
    - '0 5 * * 1'   → '每周一 05:00 执行一次'
    - '0 12 1 * *'  → '每月 1 日 12:00 执行一次'
    - '0 2 1 6 *'   → '每年 6 月 1 日 02:00 执行一次'
    - '0 2 1 1 1'   → '每年 1 月 1 日 且为周一 02:00 执行一次'
    无法归类的复杂表达式（多区间/混合步长等）原样返回 cron，保证展示不丢失信息。
    """
    fields = str(cron or '').strip().split()
    if len(fields) != 5:
        return str(cron or '')
    minute, hour, dom, month, dow = fields

    # 每 N 分钟：*/N * * * *
    if _is_step_field(minute) and hour == '*' and dom == '*' and month == '*' and dow == '*':
        return f'每 {_step_value(minute)} 分钟执行一次'
    # 每天 H 点内每 N 分钟：*/N H * * *
    if (_is_step_field(minute) and _is_fixed_field(hour) and dom == '*' and month == '*' and dow == '*'):
        return f'每天 {int(hour):02d} 点内每 {_step_value(minute)} 分钟执行一次'
    # 每周 X 点内每 N 分钟：*/N H * * DOW
    if (_is_step_field(minute) and _is_fixed_field(hour) and dom == '*' and month == '*' and dow != '*'):
        return f'每周{_fmt_weekdays(dow)} {int(hour):02d} 点内每 {_step_value(minute)} 分钟执行一次'
    # 每 N 小时（整点）：0 */N * * *
    if minute == '0' and _is_step_field(hour) and dom == '*' and month == '*' and dow == '*':
        return f'每 {_step_value(hour)} 小时执行一次'
    # 每 N 小时的第 M 分钟：M */N * * *
    if _is_fixed_field(minute) and _is_step_field(hour) and dom == '*' and month == '*' and dow == '*':
        return f'每 {_step_value(hour)} 小时的第 {int(minute)} 分钟执行一次'
    # 每小时的第 M 分钟：M * * * *
    if _is_fixed_field(minute) and hour == '*' and dom == '*' and month == '*' and dow == '*':
        return f'每小时的第 {int(minute)} 分钟执行一次'
    # 每天固定时间：M H * * *
    if _is_fixed_field(minute) and _is_fixed_field(hour) and dom == '*' and month == '*' and dow == '*':
        return f'每天 {_fmt_hhmm(hour, minute)} 执行一次'
    # 每周：M H * * DOW
    if _is_fixed_field(minute) and _is_fixed_field(hour) and dom == '*' and month == '*' and dow != '*':
        return f'每周{_fmt_weekdays(dow)} {_fmt_hhmm(hour, minute)} 执行一次'
    # 每月：M H D * *
    if _is_fixed_field(minute) and _is_fixed_field(hour) and _is_fixed_field(dom) and month == '*' and dow == '*':
        return f'每月 {int(dom)} 日 {_fmt_hhmm(hour, minute)} 执行一次'
    # 每年：M H D MO *
    if (_is_fixed_field(minute) and _is_fixed_field(hour) and _is_fixed_field(dom)
            and _is_fixed_field(month) and dow == '*'):
        return f'每年 {int(month)} 月 {int(dom)} 日 {_fmt_hhmm(hour, minute)} 执行一次'
    # 每年固定日期 + 星期限定：M H D MO DOW（如 "0 2 1 1 1" → 每年 1 月 1 日且为周一）
    if (_is_fixed_field(minute) and _is_fixed_field(hour) and _is_fixed_field(dom)
            and _is_fixed_field(month) and _is_fixed_field(dow)):
        return f'每年 {int(month)} 月 {int(dom)} 日 且为{_fmt_weekdays(dow)} {_fmt_hhmm(hour, minute)} 执行一次'
    # 兜底：保留原始 cron，避免复杂表达式被错误简化
    return f'cron 表达式：{cron}'


def build_crontab(cron: str):
    """把 5 段 cron 字符串转换为 celery crontab 对象

    调用前需先 validate_cron（本函数不再重复校验，保证只抛一次可读错误）。
    """
    fields = cron.strip().split()
    return crontab(
        minute=fields[0],
        hour=fields[1],
        day_of_month=fields[2],
        month_of_year=fields[3],
        day_of_week=fields[4],
    )


# ---------------------------------------------------------------------------
# SystemConfig 值的序列化 / 解析 / 规范化
# ---------------------------------------------------------------------------
def serialize_schedule(cron: str, enabled: bool) -> str:
    """把调度配置规范化为存储 JSON

    固定键序（cron 在前）保证新旧值可直接做字符串比较，
    避免 JSON 键序差异导致"值未变却触发工单"的误判。
    """
    return json.dumps({'cron': cron, 'enabled': bool(enabled)}, ensure_ascii=False)


def parse_schedule_value(value) -> Dict:
    """解析 SystemConfig 中存储的调度 JSON，返回 {'cron','enabled'}

    非法 JSON / 非法 cron 抛出 ValueError，由调用方决定兜底策略。
    """
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError(f'调度配置需为 JSON 格式: {value!r}')
    else:
        data = value
    if not isinstance(data, dict):
        raise ValueError(f'调度配置需为 JSON 对象: {value!r}')
    cron = str(data.get('cron') or '').strip()
    validate_cron(cron)
    enabled = bool(data.get('enabled', True))
    return {'cron': cron, 'enabled': enabled}


def normalize_schedule_value(value) -> str:
    """校验并规范化调度配置值，返回统一存储格式

    供 PUT /configs/<key>/ 在创建工单前调用：任何合法输入都会
    规范化为固定键序的 JSON，确保与存量值比较可靠。
    """
    parsed = parse_schedule_value(value)
    return serialize_schedule(parsed['cron'], parsed['enabled'])


def compute_schedule_change_summary(old_value: str, new_value: str) -> str:
    """调度类配置的变更摘要（cron / 启停分别给新旧值）

    审批人无需对比整段 JSON 即可识别本次改了调度时间还是启停状态；
    cron 变更附带 humanize 中文解释，便于快速判断改动影响。
    解析失败返回空串（按无摘要处理，不影响审批流程）。
    Returns: {"schedule": {"cron": {old,new,old_desc,new_desc}, "enabled": {old,new}}} 的 JSON 字符串
    """
    try:
        old = parse_schedule_value(old_value)
        new = parse_schedule_value(new_value)
    except ValueError:
        return ''
    summary = {}
    if old['cron'] != new['cron']:
        summary['cron'] = {
            'old': old['cron'],
            'new': new['cron'],
            'old_desc': humanize_cron(old['cron']),
            'new_desc': humanize_cron(new['cron']),
        }
    if old['enabled'] != new['enabled']:
        summary['enabled'] = {'old': old['enabled'], 'new': new['enabled']}
    if not summary:
        return ''
    return json.dumps({'schedule': summary}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 调度配置加载（启动 / 运行期共用）
# ---------------------------------------------------------------------------
def load_schedule_snapshot() -> Dict[str, dict]:
    """从 SystemConfig 读取全部可配置任务的调度快照

    Returns: {name: {'task','cron','enabled'}}
    读取策略：先取注册表默认值，再以 DB 中已存在的 SCHEDULE_* 行覆盖；
    DB 不可用或某行解析失败时回退默认值，保证 beat 始终可用、行为不回归。
    注意：直接读 DB 而非 config_loader 缓存，确保工单审批写库后
    下一次 tick 即可读到最新值（不受 5min 缓存 TTL 影响）。
    """
    snapshot = {}
    for t in SCHEDULED_TASKS:
        snapshot[t['name']] = {'task': t['task'], 'cron': t['cron'], 'enabled': t.get('enabled', True)}
    try:
        from .models import SystemConfig
        keys = [schedule_key(t['name']) for t in SCHEDULED_TASKS]
        rows = SystemConfig.objects.filter(key__in=keys).only('key', 'value')
        for row in rows:
            name = row.key[len(SCHEDULE_KEY_PREFIX):].lower()
            if name not in snapshot:
                continue
            try:
                parsed = parse_schedule_value(row.value)
            except ValueError as e:
                logger.warning(f'[scheduler_registry] 调度配置非法，使用默认值 key={row.key}: {e}')
                continue
            snapshot[name]['cron'] = parsed['cron']
            snapshot[name]['enabled'] = parsed['enabled']
    except Exception as e:
        logger.warning(f'[scheduler_registry] 读取调度配置失败，回退注册表默认值: {e}')
    return snapshot


def build_schedule_from_snapshot(snapshot: Dict[str, dict]) -> Dict[str, dict]:
    """把调度快照转换为 celery beat_schedule 结构（停用的任务不进入调度）

    Returns: {name: {'task': ..., 'schedule': crontab}}
    该结构可直接赋给 app.conf.beat_schedule / 覆盖 scheduler.schedule。
    """
    result = {}
    for name, spec in snapshot.items():
        if not spec.get('enabled', True):
            continue
        result[name] = {
            'task': spec['task'],
            'schedule': build_crontab(spec['cron']),
        }
    return result


def default_schedule_dict() -> Dict[str, dict]:
    """构建代码默认 beat_schedule（与历史 celery.py 硬编码一致）

    供 celery.py 在导入期使用；运行期由 SystemConfigScheduler 用
    load_schedule_snapshot() 的结果覆盖，DB 不可用时此默认值兜底。
    """
    return build_schedule_from_snapshot(
        {t['name']: {'task': t['task'], 'cron': t['cron'], 'enabled': t.get('enabled', True)}
         for t in SCHEDULED_TASKS}
    )


def get_tasks_meta() -> List[dict]:
    """返回任务元数据 + 当前调度值，供管理端定时任务页渲染

    Returns: [{name, key, task, label, description, cron, enabled, risk_level, pending_ticket_count}]
    """
    snapshot = load_schedule_snapshot()
    pending_count = {}
    try:
        from .models import SystemConfig, ConfigChangeTicket
        keys = [schedule_key(t['name']) for t in SCHEDULED_TASKS]
        from django.db.models import Count
        qs = ConfigChangeTicket.objects.filter(
            config_key__in=keys,
            status__in=['pending', 'first_approved'],
        ).values('config_key').annotate(cnt=Count('id'))
        pending_count = {r['config_key']: r['cnt'] for r in qs}
    except Exception as e:
        logger.warning(f'[scheduler_registry] 查询调度工单失败: {e}')
    result = []
    for t in SCHEDULED_TASKS:
        spec = snapshot.get(t['name'], {})
        cron = spec.get('cron', t['cron'])
        result.append({
            'name': t['name'],
            'key': schedule_key(t['name']),
            'task': t['task'],
            'label': t['label'],
            'description': t['description'],
            'estimated_minutes': t.get('estimated_minutes', 0),
            'cron': cron,
            'humanized': humanize_cron(cron),
            'cron_fields': parse_cron_fields(cron),
            'enabled': spec.get('enabled', t.get('enabled', True)),
            'risk_level': SCHEDULE_RISK_LEVEL,
            'pending_ticket_count': pending_count.get(schedule_key(t['name']), 0),
        })
    return result
    return result
