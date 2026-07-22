import os

from loguru import logger

FRONTEND = 'frontend-modular'
TARGET = os.path.join('templates', 'pages')
os.makedirs(TARGET, exist_ok=True)

pages = [
    'index', 'login', 'reset-password', 'chat', 'upload',
    'profile', 'admin-users', 'admin-nodes', 'admin-analytics', 'admin-audit'
]

for name in pages:
    src = os.path.join(FRONTEND, f'{name}.html')
    dst = os.path.join(TARGET, f'{name}.html')
    with open(src, 'r', encoding='utf-8') as f:
        content = f.read()

    replacements = [
        ('./css/', "{% static 'css/"),
        ('./js/',  "{% static 'js/"),
    ]
    for old, new in replacements:
        content = content.replace(old, new)

    import re
    content = re.sub(r"\.css(['\"])([^%])", r".css' %}\1\2", content)
    content = re.sub(r"\.js(['\"])([^%])",  r".js' %}\1\2", content)

    content = '{% load static %}\n' + content

    with open(dst, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.success(f'{name}.html')

logger.info('Done')
