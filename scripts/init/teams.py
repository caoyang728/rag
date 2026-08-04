"""团队初始化模块"""
import traceback

from loguru import logger


def create_teams(config, dry_run=False):
    """创建初始团队，按 department 名称关联部门"""
    logger.info('\n=== 创建团队 ===')
    from apps.users.models import Team, Department
    teams_config = config.get('teams', [])
    created = 0
    skipped = 0

    for team_data in teams_config:
        code = team_data['code']
        name = team_data['name']
        description = team_data.get('description', '')
        dept_name = team_data.get('department')

        try:
            if Team.objects.filter(code=code).exists():
                logger.info(f'  ⏭️  团队 "{code}" 已存在，跳过')
                skipped += 1
                continue

            department = None
            if dept_name:
                try:
                    department = Department.objects.get(name=dept_name)
                except Department.DoesNotExist:
                    logger.info(f'  ⚠️  团队 "{code}" 的部门 "{dept_name}" 不存在，将不设置部门')

            if not dry_run:
                Team.objects.create(
                    name=name,
                    code=code,
                    description=description,
                    department=department
                )
            logger.info(f'  ✅ 创建团队: {code} - {name}')
            created += 1
        except Exception as e:
            logger.info(f'  ❌ 创建团队 "{code}" 失败: {e}')
            traceback.print_exc()

    logger.info(f'  总计: 创建 {created} 个，跳过 {skipped} 个')
    return created
