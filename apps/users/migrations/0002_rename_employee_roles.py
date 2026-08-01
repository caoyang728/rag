"""重命名内置普通用户角色 role_key：
- employee → contributor
- read_only_employee → viewer

同时同步更新 name 和 description，与 seed_permissions.py 保持一致。
对于已有系统中存在的旧角色记录，以幂等方式更新，避免重复执行报错。
"""
from django.db import migrations


def rename_role_keys(apps, schema_editor):
    Role = apps.get_model('users', 'Role')
    updates = [
        ('employee', 'contributor', '参与者', '显式授权角色，需申请获得，获得后覆盖 viewer 兜底，可查看/上传/下载文档'),
        ('read_only_employee', 'viewer', '查看者', '兜底角色，随人事归属生效自带只读，未显式授权 contributor 时自动叠加'),
    ]
    for old_key, new_key, new_name, new_desc in updates:
        Role.objects.filter(role_key=old_key).update(
            role_key=new_key,
            name=new_name,
            description=new_desc,
        )


def reverse_rename(apps, schema_editor):
    Role = apps.get_model('users', 'Role')
    updates = [
        ('contributor', 'employee', '普通员工', '随人事归属生效的兜底角色，仅查看/上传/下载本人团队文档'),
        ('viewer', 'read_only_employee', '只读员工', '显式授权角色，仅可读取文档，无下载/写操作权限；新用户默认获得'),
    ]
    for old_key, new_key, new_name, new_desc in updates:
        Role.objects.filter(role_key=old_key).update(
            role_key=new_key,
            name=new_name,
            description=new_desc,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(rename_role_keys, reverse_rename),
    ]
