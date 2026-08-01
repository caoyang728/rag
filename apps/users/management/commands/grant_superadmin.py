from django.core.management.base import BaseCommand
from apps.users.models import User, Role, UserRoleRel, GrantStatus


class Command(BaseCommand):
    help = 'Grant super_admin role to users'

    def add_arguments(self, parser):
        parser.add_argument('--username', help='Specific username to grant role')
        parser.add_argument(
            '--role',
            default='super_admin',
            choices=['super_admin'],
            help='Role to grant: super_admin (超级管理员)',
        )

    def handle(self, *args, **options):
        role_key = options['role']
        role_obj, _ = Role.objects.get_or_create(
            role_key=role_key,
            defaults={
                'name': '超级管理员',
                'description': '系统级快路径角色，鉴权时绕过所有 permission_key 判定',
                'is_builtin': True,
            }
        )

        if options['username']:
            users = User.objects.filter(username=options['username'])
        else:
            users = User.objects.all()

        updated = 0
        skipped = 0

        for user in users:
            if UserRoleRel.objects.filter(
                user=user, role=role_obj, status=GrantStatus.ACTIVE
            ).exists():
                self.stdout.write(self.style.WARNING(
                    f'User {user.username} already has {role_key} role'
                ))
                skipped += 1
            else:
                UserRoleRel.objects.create(
                    user=user, role=role_obj, status=GrantStatus.ACTIVE
                )
                self.stdout.write(self.style.SUCCESS(
                    f'Granted {role_key} role to user: {user.username}'
                ))
                updated += 1

        if not options['username']:
            self.stdout.write(self.style.SUCCESS(f'Total: {updated} updated, {skipped} skipped'))
