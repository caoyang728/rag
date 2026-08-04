"""部门初始化模块"""
import traceback

from loguru import logger


def create_departments(config, dry_run=False):
    """创建初始部门"""
    logger.info('\n=== 创建部门 ===')
    from apps.users.models import Department
    depts_config = config.get('departments', [])
    created = 0
    skipped = 0

    for dept_data in depts_config:
        code = dept_data['code']
        name = dept_data['name']
        sort_order = dept_data.get('sort_order', 0)

        try:
            if Department.objects.filter(code=code).exists():
                logger.info(f'  ⏭️  部门 "{code}" 已存在，跳过')
                skipped += 1
            else:
                if not dry_run:
                    Department.objects.create(
                        name=name,
                        code=code,
                        sort_order=sort_order
                    )
                logger.info(f'  ✅ 创建部门: {code} - {name}')
                created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建部门 "{code}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created
