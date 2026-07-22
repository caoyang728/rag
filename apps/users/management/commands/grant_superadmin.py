from django.core.management.base import BaseCommand
from apps.users.models import SysUser, Role, UserRole


class Command(BaseCommand):
    help = 'Grant super_admin role to existing superusers (created via createsuperuser)'

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
            if user.is_superuser:
                _, created = UserRole.objects.get_or_create(user=user, role=super_admin_role)
                if created:
                    self.stdout.write(self.style.SUCCESS(f'Granted super_admin role to user: {user.username}'))
                    updated += 1
                else:
                    self.stdout.write(self.style.WARNING(f'User {user.username} already has super_admin role'))
                    skipped += 1

        if not options['username']:
            self.stdout.write(self.style.SUCCESS(f'Total: {updated} updated, {skipped} skipped'))