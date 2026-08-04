"""LLM/Embedding/Rerank 模型配置初始化模块

将 initial_data.yaml 中 llm_models 段写入 LLMModel 表。
- 首次部署：创建全部预置模型条目。
- 已初始化模式（再次运行 init_system.py）：增量补齐缺失条目，已存在的
  (model_type, name) 因 unique_together 约束跳过，保留用户在前端已调整的值。
- --force 时：连已存在项的字段也一并覆盖为 yaml 默认值。
"""
import traceback

from loguru import logger


def create_llm_models(config, dry_run=False, force=False):
    """初始化模型配置（LLM / Embedding / Rerank）

    Args:
        config: yaml 加载的 dict，读取 llm_models 段
        dry_run: 仅预览不写库
        force: 强制覆盖已存在项的字段（默认 False，仅创建缺失项）

    Returns:
        int: 本次新增的条目数
    """
    logger.info('\n=== 创建模型配置 ===')
    from apps.system.models import LLMModel
    models_config = config.get('llm_models', [])
    created = 0
    updated = 0
    skipped = 0

    for item in models_config:
        name = item.get('name', '').strip()
        model_type = item.get('model_type', '')
        # 缺关键字段直接跳过，避免脏数据落库
        if not name or not model_type:
            logger.info(f'  ⏭️  跳过缺字段的模型配置: {item}')
            skipped += 1
            continue

        provider = item.get('provider', '').strip()
        base_url = item.get('base_url', '').strip()
        model_name = item.get('model_name', '').strip()
        is_active = bool(item.get('is_active', True))

        try:
            # 按 unique_together (model_type, name) 判定是否已存在
            obj = LLMModel.objects.filter(model_type=model_type, name=name).first()
            if obj is None:
                if not dry_run:
                    LLMModel.objects.create(
                        name=name,
                        provider=provider,
                        model_type=model_type,
                        base_url=base_url,
                        model_name=model_name,
                        is_active=is_active,
                    )
                logger.info(f'  ✅ 新增模型: [{model_type}] {name} ({model_name})')
                created += 1
            else:
                # 已存在：默认跳过保留用户值；--force 时覆盖非主键字段
                if not force:
                    skipped += 1
                    logger.info(f'  ⏭️  模型 [{model_type}] {name} 已存在，保留用户配置')
                else:
                    if not dry_run:
                        obj.provider = provider
                        obj.base_url = base_url
                        obj.model_name = model_name
                        obj.is_active = is_active
                        obj.save()
                    logger.info(f'  🔄 强制覆盖模型: [{model_type}] {name}')
                    updated += 1
        except Exception as e:
            logger.info(f'  ❌ 模型 [{model_type}] {name} 初始化失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 新增 {created} 个，覆盖 {updated} 个，跳过 {skipped} 个')
    return created
