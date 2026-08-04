"""全局记忆初始化模块"""
import traceback

from loguru import logger


def create_global_memories(config, dry_run=False):
    """创建全局记忆（公司规则、回答规范等）"""
    logger.info('\n=== 创建全局记忆 ===')
    from apps.memory.models import GlobalMemory
    gm_config = config.get('global_memories', [])
    created = 0
    skipped = 0

    for gm_data in gm_config:
        key = gm_data['key']
        content = gm_data['content']
        scope_root_types = gm_data.get('scope_root_types', [])
        priority = gm_data.get('priority', 0)
        is_enabled = gm_data.get('is_enabled', True)

        try:
            if GlobalMemory.objects.filter(key=key).exists():
                logger.info(f'  ⏭️  全局记忆 "{key}" 已存在，跳过')
                skipped += 1
            else:
                if not dry_run:
                    GlobalMemory.objects.create(
                        key=key,
                        content=content,
                        scope_root_types=scope_root_types,
                        priority=priority,
                        is_enabled=is_enabled
                    )
                logger.info(f'  ✅ 创建全局记忆: {key}')
                created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建全局记忆 "{key}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created
