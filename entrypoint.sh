#!/bin/bash
# ==========================================================================
# Django 容器启动脚本：等 DB → 迁移 → 造超管 → 收集静态 → 启动 gunicorn
# ==========================================================================
set -e

echo "[entrypoint] 等待 PostgreSQL 就绪..."
until python -c "import psycopg, os; \
    host=os.environ.get('PG_DB_HOST'); \
    port=os.environ.get('PG_DB_PORT', '5432'); \
    db=os.environ.get('PG_DB_DATABASE'); \
    user=os.environ.get('PG_DB_USER'); \
    password=os.environ.get('PG_DB_PASSWORD'); \
    dsn=f'postgresql://{user}:{password}@{host}:{port}/{db}'; \
    conn=psycopg.connect(dsn); conn.close(); print('DB ok')" 2>/dev/null; do
  echo "  DB not ready, sleep 2s..."
  sleep 2
done

echo "[entrypoint] Django makemigrations..."
python manage.py makemigrations --noinput || true

echo "[entrypoint] Django migrate..."
python manage.py migrate --noinput

echo "[entrypoint] 收集静态资源..."
python manage.py collectstatic --noinput || true

# 生产服务器, 根据情况进行调整配置
# echo "[entrypoint] 启动 Gunicorn..."
# exec gunicorn rag_project.wsgi:application \
#     --bind 0.0.0.0:8000 \
#     --workers 2 \
#     --timeout 120 \
#     --access-logfile - \
#     --error-logfile -

# 开发服务器
echo "[entrypoint] 启动 Django 开发服务器..."
exec python manage.py runserver 0.0.0.0:8000
