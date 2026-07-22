# --------------------------------------------------------------------
# RAG-Agent Backend Dockerfile
# 基础镜像：python:3.11-slim（含 libpq 客户端）
# --------------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 系统依赖：gcc（安全网）/ libmagic（MIME检测）/ ca-certificates（HTTPS证书）/ tzdata（时区）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libmagic1 \
    ca-certificates \
    tzdata \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 使用清华镜像加速（可注释）
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple/ || true

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
