"""
初始化公共工具

- 数据库连接测试（Django 启动前用 psycopg 直连，避免 ORM 依赖未就绪）
- Django 启动 / 迁移检查 / 表存在检查
- yaml 配置文件加载
"""
import os
import sys
import traceback

from loguru import logger


def test_db_connection():
    """直连 PG 测试连通性，settings 未就绪时使用

    避免 Django ORM 还没初始化时就报错，给用户更友好的提示
    """
    import psycopg
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        host = os.getenv('PG_DB_HOST', 'localhost')
        port = int(os.getenv('PG_DB_PORT', '5432'))
        db = os.getenv('PG_DB_DATABASE', 'rag_agent')
        user = os.getenv('PG_DB_USER', 'rag_user')
        password = os.getenv('PG_DB_PASSWORD', 'rag_pass_2026')
        ssl_mode = os.getenv('PG_SSL_MODE', 'prefer')
    else:
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        host = parsed.hostname
        port = parsed.port or 5432
        db = parsed.path.lstrip('/')
        user = parsed.username
        password = parsed.password
        ssl_mode = 'prefer'

    logger.info(f'🔍 测试数据库连接: {user}@{host}:{port}/{db}')
    try:
        conn = psycopg.connect(
            host=host,
            port=port,
            dbname=db,
            user=user,
            password=password,
            sslmode=ssl_mode,
            connect_timeout=10
        )
        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        result = cursor.fetchone()
        logger.info(f'✅ 数据库连接成功: {result[0].split()[0]}')
        conn.close()
        return True
    except psycopg.Error as e:
        logger.info(f'❌ 数据库连接失败: {e}')
        return False
    except Exception as e:
        logger.info(f'❌ 连接测试异常: {e}')
        return False


def setup_django():
    """初始化 Django 环境，让 ORM/迁移可用"""
    try:
        import django
        django.setup()
        logger.info('✅ Django 初始化成功')
        return True
    except Exception as e:
        logger.info(f'❌ Django 初始化失败: {e}')
        traceback.print_exc()
        return False


def check_migrations():
    """检查 users 模块迁移是否已执行（避免空库直接 init 报错）"""
    from django.core.management import call_command
    try:
        from django.db import connection
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app='users';")
        count = cursor.fetchone()[0]
        if count == 0:
            logger.info('❌ 用户模块迁移未执行，请先运行:')
            logger.info('   python manage.py makemigrations')
            logger.info('   python manage.py migrate')
            return False
        logger.info(f'✅ 用户模块迁移已执行（{count} 条记录）')
        return True
    except Exception as e:
        logger.info(f'⚠️  检查迁移状态失败: {e}')
        return False


def check_table_exists(table_name):
    """检查指定表是否存在，用于前置依赖校验"""
    from django.db import connection
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = %s);",
            [table_name]
        )
        exists = cursor.fetchone()[0]
        return exists
    except Exception as e:
        logger.info(f'❌ 检查表 "{table_name}" 失败: {e}')
        return False


def load_config(config_path):
    """加载 yaml 初始化数据"""
    import yaml
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.info(f'❌ 配置文件加载失败: {e}')
        traceback.print_exc()
        return None
