from django.core.management.base import BaseCommand
from apps.users.models import SysUser, Role, UserRole


class Command(BaseCommand):
    help = 'Grant super_admin role to users'

    def add_arguments(self, parser):
        parser.add_argument('--username', help='Specific username to grant super_admin role')

    def handle(self, *args, **options):
        super_admin_role, _ = Role.objects.get_or_create(
            code='super_admin',
            defaults={'name': '超级管理员', 'description': '系统超级管理员', 'is_builtin': True}
        )

        if options['username']:
            users = SysUser.objects.filter(username=options['username'])
        else:
            users = SysUser.objects.all()

        updated = 0
        skipped = 0

        for user in users:
            # 检查是否已有super_admin角色
            if UserRole.objects.filter(user=user, role=super_admin_role).exists():
                self.stdout.write(self.style.WARNING(f'User {user.username} already has super_admin role'))
                skipped += 1
            else:
                UserRole.objects.create(user=user, role=super_admin_role)
                self.stdout.write(self.style.SUCCESS(f'Granted super_admin role to user: {user.username}'))
                updated += 1

        if not options['username']:
            self.stdout.write(self.style.SUCCESS(f'Total: {updated} updated, {skipped} skipped'))