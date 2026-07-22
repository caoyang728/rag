"""一键建库 + 装扩展"""
import os, sys
sys.stdout.reconfigure(encoding='utf-8')

from loguru import logger
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
logger.info(f'加载 .env: {env_path}')
load_dotenv(env_path)

import psycopg

host = os.getenv('PG_DB_HOST')
port = os.getenv('PG_DB_PORT', '5432')
user = os.getenv('PG_DB_USER')
password = os.getenv('PG_DB_PASSWORD')
dbname = os.getenv('PG_DB_DATABASE')

logger.info(f'目标: {host}:{port}, user={user}, db={dbname}')

try:
    conn = psycopg.connect(
        host=host, port=port, user=user, password=password, dbname='postgres',
        connect_timeout=10
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    if cur.fetchone():
        logger.info(f'数据库 {dbname} 已存在')
    else:
        cur.execute(f'CREATE DATABASE "{dbname}"')
        logger.success(f'数据库 {dbname} 创建成功')
    conn.close()
except Exception as e:
    logger.error(f'建库失败: {e}')
    sys.exit(1)

try:
    conn = psycopg.connect(
        host=host, port=port, user=user, password=password, dbname=dbname,
        connect_timeout=10
    )
    conn.autocommit = True
    cur = conn.cursor()
    for ext in ['vector', 'pg_trgm', 'btree_gin', 'pgcrypto']:
        cur.execute(f'CREATE EXTENSION IF NOT EXISTS {ext}')
        logger.success(f'扩展 {ext}')
    conn.close()
except Exception as e:
    logger.error(f'安装扩展失败: {e}')
    sys.exit(1)

logger.info('初始化完成')
