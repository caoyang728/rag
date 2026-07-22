import os
apps_root = '/app/data/所有对话/主对话/用户上传/rag-agent-platform/backend/apps'
apps = ['users', 'knowledge', 'retrieval', 'llm', 'agent', 'memory',
        'chat', 'audit', 'security', 'analytics', 'notification', 'system']
for a in apps:
    klass = a[0].upper() + a[1:] + 'Config'
    content = f'''from django.apps import AppConfig


class {klass}(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.{a}'
    label = '{a}'
'''
    fp = os.path.join(apps_root, a, 'apps.py')
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(content)
    print('wrote', fp)
