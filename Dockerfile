# --------------------------------------------------------------------
# RAG-Agent Backend Dockerfile
# 基础镜像：python:3.13-slim
# --------------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 系统依赖：build-essential(安全网,以备原生扩展编译)/ libmagic(MIME检测)/ ca-certificates(HTTPS)/ tzdata(时区)/ 字体
# LibreOffice(headless)：docx/xlsx/pptx 转 PDF 在线预览（文档预览功能依赖；
#   仅装 writer/calc/impress 三个组件 + --no-install-recommends 控制体积；
# 使用清华 apt 镜像加速(国内网络显著提速)
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libmagic1 \
    ca-certificates \
    tzdata \
    fonts-dejavu-core \
    fonts-noto-cjk \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 使用清华 pip 镜像加速
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple/ || true

# 分步安装依赖,解决 deepeval(要求 click<8.4) 与 huggingface-hub(要求 click>=8.4.2) 的冲突
# 1. 安装主依赖(不含 ragas/deepeval,避免 pip resolver 冲突)
# 2. 安装 ragas(拉取 huggingface-hub → click>=8.4.2)
# 3. 安装 deepeval(会将 click 降级到 8.3.x,但 deepeval 实际兼容 8.4.2)
# 4. 强制升级 click 到 8.4.2(huggingface-hub 硬性要求)
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r /app/requirements.txt && \
    pip install "ragas>=0.4.0,<1.0.0" "pandas>=2.0.0" && \
    pip install "deepeval>=4.0.0,<5.0.0" && \
    pip install --force-reinstall click==8.4.2

COPY . /app

RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
