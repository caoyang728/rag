"""
系统配置（KV）初始化模块

将原 .env 中非安全敏感的运行期配置项迁移到 SystemConfig 表，由前端系统配置页面管理。

设计要点：
- update_or_create 策略：
  · 已存在的 key：默认不覆盖 value（保留用户已在前端调整的值），
    但仍更新 description/category/is_readonly/value_type 等元数据
    （元数据由开发者维护，value 由用户维护，两者解耦）
  · --force 时：连 value 一起覆盖，用于重置默认值
- 首次部署时表为空，全部走 create 分支
- 后续升级新增配置项时，仅新增项走 create，已有项保持用户值不变
"""
import json
import traceback

from loguru import logger


def _normalize_value(value, value_type: str) -> str:
    """按 value_type 规范化为字符串存储

    - bool: 统一存 'true' / 'false'（兼容 yaml 中 true/false/True/False/1/0 等写法）
    - json: list/dict 转 json 字符串；str 原样保留
    - int/float/string: 转 str
    """
    if value_type == 'bool':
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, str):
            return 'true' if value.lower() in ('1', 'true', 'yes', 'on') else 'false'
        return 'true' if value else 'false'
    if value_type == 'json':
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def create_system_configs(config, dry_run=False, force=False):
    """初始化系统配置项

    Args:
        config: yaml 加载的 dict，读取 system_configs 段
        dry_run: 仅预览不写库
        force: 强制覆盖已存在项的 value（默认 False，仅更新元数据）
    """
    logger.info('\n=== 创建系统配置 ===')
    from apps.system.models import SystemConfig
    configs = config.get('system_configs', [])
    created = 0
    updated = 0
    skipped = 0

    for item in configs:
        key = item['key']
        raw_value = item.get('value', '')
        value_type = item.get('value_type', 'string')
        # 按 value_type 规范化为字符串，避免 yaml 类型歧义（bool/json）
        value = _normalize_value(raw_value, value_type)
        label = item.get('label', '')
        description = item.get('description', '')
        unit = item.get('unit', '')
        # options 是 yaml 列表，转为 JSON 字符串存储（空列表/无选项时存空字符串）
        options_raw = item.get('options', '')
        if options_raw and isinstance(options_raw, list):
            options = json.dumps(options_raw, ensure_ascii=False)
        else:
            options = ''
        category = item.get('category', 'llm')
        is_secret = bool(item.get('is_secret', False))
        is_readonly = bool(item.get('is_readonly', False))
        # 风险等级：高风险项变更需走超管复核流程，由开发者维护，与 value 解耦
        risk_level = item.get('risk_level', 'normal')

        try:
            obj = SystemConfig.objects.filter(key=key).first()
            if obj is None:
                # 新增：首次部署或后续版本新增配置项
                if not dry_run:
                    SystemConfig.objects.create(
                        key=key,
                        value=value,
                        value_type=value_type,
                        label=label,
                        description=description,
                        unit=unit,
                        options=options,
                        category=category,
                        is_secret=is_secret,
                        is_readonly=is_readonly,
                        risk_level=risk_level,
                    )
                logger.info(f'  ✅ 新增配置: {key} = {value if not is_secret else "***"}')
                created += 1
            else:
                # 已存在：默认仅更新元数据，保留用户已调整的 value
                # force=True 时连 value 一起重置为 yaml 默认值
                if not force:
                    skipped += 1
                    # 仍更新元数据（label/description/unit/options/category/is_readonly/risk_level/value_type 由开发者维护）
                    if not dry_run:
                        obj.value_type = value_type
                        obj.label = label
                        obj.description = description
                        obj.unit = unit
                        obj.options = options
                        obj.category = category
                        obj.is_secret = is_secret
                        obj.is_readonly = is_readonly
                        obj.risk_level = risk_level
                        obj.save()
                    logger.info(f'  ⏭️  配置 "{key}" 已存在，保留用户 value，仅更新元数据')
                else:
                    if not dry_run:
                        obj.value = value
                        obj.value_type = value_type
                        obj.label = label
                        obj.description = description
                        obj.unit = unit
                        obj.options = options
                        obj.category = category
                        obj.is_secret = is_secret
                        obj.is_readonly = is_readonly
                        obj.risk_level = risk_level
                        obj.save()
                    logger.info(f'  🔄 强制覆盖配置: {key} = {value if not is_secret else "***"}')
                    updated += 1
        except Exception as e:
            logger.info(f'  ❌ 配置 "{key}" 初始化失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 新增 {created} 个，覆盖 {updated} 个，保留 {skipped} 个')
    return created


def create_schedule_configs(dry_run=False, force=False):
    """初始化定时任务调度配置项（SCHEDULE_*）

    任务清单与默认调度时间来自 apps.system.scheduler_registry.SCHEDULED_TASKS
    （单一数据源），不写入 yaml，避免两处维护不一致。
    所有调度项统一标记为高风险：修改调度时间 / 启停需走"审核 + 超管复核"工单，
    与"修改定时任务时间需审批"的要求一致（定时任务影响生产批量作业与成本）。

    Args:
        dry_run: 仅预览不写库
        force: 强制覆盖已存在项的 value（默认 False，仅更新元数据）
    """
    logger.info('\n=== 创建定时任务调度配置 ===')
    from apps.system.models import SystemConfig
    from apps.system.scheduler_registry import (
        SCHEDULED_TASKS,
        SCHEDULE_CATEGORY,
        SCHEDULE_RISK_LEVEL,
        schedule_key,
        serialize_schedule,
    )
    created = 0
    updated = 0
    skipped = 0

    for task in SCHEDULED_TASKS:
        key = schedule_key(task['name'])
        value = serialize_schedule(task['cron'], task.get('enabled', True))
        label = task['label']
        description = task['description']
        try:
            obj = SystemConfig.objects.filter(key=key).first()
            if obj is None:
                # 新增：首次部署或后续新增调度任务
                if not dry_run:
                    SystemConfig.objects.create(
                        key=key, value=value, value_type='json',
                        label=label, description=description,
                        category=SCHEDULE_CATEGORY, is_secret=False,
                        is_readonly=False, risk_level=SCHEDULE_RISK_LEVEL,
                    )
                logger.info(f'  ✅ 新增调度配置: {key} = {value}')
                created += 1
            elif force:
                # 强制覆盖：重置为用户之前的默认值（含 value）
                if not dry_run:
                    obj.value = value
                    obj.value_type = 'json'
                    obj.label = label
                    obj.description = description
                    obj.category = SCHEDULE_CATEGORY
                    obj.is_secret = False
                    obj.is_readonly = False
                    obj.risk_level = SCHEDULE_RISK_LEVEL
                    obj.save()
                logger.info(f'  🔄 强制覆盖调度配置: {key} = {value}')
                updated += 1
            else:
                # 已存在：仅更新元数据，保留用户在管理端调整的 value
                skipped += 1
                if not dry_run:
                    obj.value_type = 'json'
                    obj.label = label
                    obj.description = description
                    obj.category = SCHEDULE_CATEGORY
                    obj.is_secret = False
                    obj.is_readonly = False
                    obj.risk_level = SCHEDULE_RISK_LEVEL
                    obj.save()
                logger.info(f'  ⏭️  调度配置 "{key}" 已存在，保留用户 value，仅更新元数据')
        except Exception as e:
            logger.info(f'  ❌ 调度配置 "{key}" 初始化失败: {e}')
            traceback.print_exc()

    logger.info(f'  调度配置总计: 新增 {created} 个，覆盖 {updated} 个，保留 {skipped} 个')
    return created
