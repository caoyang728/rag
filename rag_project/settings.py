"""
Django settings for RAG-Agent 知识库平台
"""
import os
import sys
import inspect
import logging
from datetime import timedelta
from pathlib import Path

import dj_database_url
from loguru import logger

# 导入独立配置模块
from .config import (
    SecurityConfig, DatabaseConfig, RedisConfig, CeleryConfig,
    CorsConfig, LLMConfig, EmbeddingConfig, LogConfig
)

BASE_DIR = Path(__file__).resolve().parent.parent

# --- 初始化日志 ---
LogConfig.setup_logger()

# --- 安全 & 环境 ---
SECRET_KEY = SecurityConfig.secret_key()
DEBUG = SecurityConfig.debug()
ALLOWED_HOSTS = SecurityConfig.allowed_hosts()

# --- 应用注册 ---
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_filters',
    'django_celery_beat',
    'django_celery_results',
]

LOCAL_APPS = [
    'apps.users',
    'apps.knowledge',
    'apps.retrieval',
    'apps.llm',
    'apps.agent',
    'apps.memory',
    'apps.chat',
    'apps.audit',
    'apps.security',
    'apps.analytics',
    'apps.notification',
    'apps.system',
    'apps.graph',
    'apps.wiki',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# --- 中间件 ---
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # 自定义中间件
    'apps.security.middleware.IpFilterMiddleware',   # IP 白/黑名单
    'apps.audit.middleware.AuditMiddleware',         # 审计日志
    'apps.system.middleware.SlowRequestMiddleware',  # 慢请求日志
]

ROOT_URLCONF = 'rag_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'rag_project.wsgi.application'
ASGI_APPLICATION = 'rag_project.asgi.application'

# --- 数据库 ---
DATABASE_URL = DatabaseConfig.build_url()
_conn_max_age = DatabaseConfig.conn_max_age()
_pool_enabled = DatabaseConfig.pool_enabled()
_pool_available = False

if _pool_enabled:
    # 检测 psycopg_pool 是否可用，不可用时自动降级到 Django 原生连接池
    try:
        import psycopg_pool
        _pool_available = True
    except ImportError:
        logger.warning('psycopg_pool 未安装，自动降级为 Django 原生连接模式。'
                       '如需使用连接池，请 pip install psycopg_pool。')

if _pool_enabled and _pool_available:
    # 启用 psycopg_pool 连接池: 使用自定义后端封装池化连接
    # CONN_MAX_AGE 设为 0，由连接池的 max_lifetime 统一管理连接生命周期
    _pool_options = DatabaseConfig.get_pool_options()
    DATABASES = {
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=0)
    }
    DATABASES['default']['ENGINE'] = 'rag_project.db.pooled_postgresql'
    DATABASES['default']['CONN_MAX_AGE'] = 0  # 池化后禁用 Django 原生年龄控制
    DATABASES['default']['CONN_HEALTH_CHECKS'] = True  # 保留健康检查语义，检测坏连接
    DATABASES['default']['POOL_OPTIONS'] = _pool_options
    DATABASES['default']['OPTIONS'] = {
        'connect_timeout': _pool_options.get('timeout', 30),
    }
else:
    # 未启用连接池或 psycopg_pool 不可用: 回退到 Django 原生连接模式
    DATABASES = {
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=_conn_max_age)
    }
    DATABASES['default']['ENGINE'] = 'django.db.backends.postgresql'
    DATABASES['default']['CONN_MAX_AGE'] = _conn_max_age
    DATABASES['default']['CONN_HEALTH_CHECKS'] = True
    DATABASES['default']['OPTIONS'] = {
        'connect_timeout': 10,
    }

# --- 连接池清理（进程退出或 Django 关闭时关闭连接池）---
if _pool_enabled and _pool_available:
    from .db.pooled_postgresql import setup_pool_cleanup
    setup_pool_cleanup()

# --- 密码校验 ---
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]

# --- 国际化 ---
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

# --- 静态 & 媒体 ---
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'
                    if not DEBUG else 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

# WhiteNoise 静态缓存：长缓存一年，二次访问命中缓存不再下载。
# 注意：写死的 /static/vendor/*.js URL 未带 hash 指纹，升级第三方库时应
# 同时修改文件名与页面引用（见 graph.html 的 echarts 引用），避免浏览器缓存旧版
WHITENOISE_MAX_AGE = 31536000

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- 自定义用户模型 ---
AUTH_USER_MODEL = 'users.User'

# --- REST Framework ---
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rag_project.pagination.StandardPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.AnonRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'user': '600/min',
        'anon': '60/min',
        'login': '20/min',
        'captcha': '30/min',
    },
    'EXCEPTION_HANDLER': 'apps.users.exceptions.custom_exception_handler',
}

# --- JWT ---
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=8),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# --- CORS（开发放开）---
CORS_ALLOW_ALL_ORIGINS = CorsConfig.allow_all_origins()
CORS_ALLOWED_ORIGINS = CorsConfig.allowed_origins()
CORS_ALLOW_CREDENTIALS = True

# --- Redis ---
REDIS_URL = RedisConfig.build_url(db=int(os.getenv('REDIS_DB_DB', '0')))

# --- 缓存（自动降级为内存缓存）---
if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {'CLIENT_CLASS': 'django_redis.client.DefaultClient'},
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }

# --- Celery ---
CELERY_BROKER_URL = CeleryConfig.broker_url()
CELERY_RESULT_BACKEND = CeleryConfig.result_backend()
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540
CELERY_TASK_DEFAULT_QUEUE = 'default'
# 定时任务调度器：从 SystemConfig 动态读取调度配置（管理端"定时任务"页维护），
# 工单审批通过后无需重启 beat 即生效（见 apps/system/schedulers.py）
CELERY_BEAT_SCHEDULER = 'apps.system.schedulers:SystemConfigScheduler'
# beat 轮询上限：调度配置变更后最多 30s 内被热更新，权衡实时性与 DB 开销
CELERY_BEAT_MAX_LOOP_INTERVAL = 30
CELERY_TASK_QUEUES = {
    'default': {},
    'parse': {},     # 文档解析
    'memory': {},    # 记忆提炼
    'email': {},     # 邮件推送
    'analytics': {}, # 系统监控 & 忠实度评估（低优先级任务）
}

# --- LLM ---
# 支持双模型：基础模型（简单任务）和高级模型（复杂任务）
# base_url 已迁移到模型管理（LLMModel 表），不再作为系统配置项
LLM_API_KEY = LLMConfig.api_key()
LLM_BASE_MODEL = LLMConfig.default_model()         # 基础模型（用于简单任务）
LLM_ADVANCED_MODEL = LLMConfig.advanced_model()    # 高级模型（用于复杂任务）

# --- Agent 配置（Agentic RAG 工具调用）---
# TAVILY_API_KEY: 联网搜索工具的 API Key（https://tavily.com，免费 1000 次/月）
# 未配置时自动降级到 DuckDuckGo（免费、无需 Key）
TAVILY_API_KEY = os.getenv('TAVILY_API_KEY', '')
# BUSINESS_DB_DSN: Text2SQL 工具的业务数据库连接串
# 留空时使用 django 默认数据库；格式：postgresql://user:pass@host:port/dbname
BUSINESS_DB_DSN = os.getenv('BUSINESS_DB_DSN', '')
# BUSINESS_DB_TABLES: Text2SQL 可查询表的名称白名单（逗号分隔）
# 留空表示允许 public schema 下的全部表（生产环境建议配置白名单）
BUSINESS_DB_TABLES = os.getenv('BUSINESS_DB_TABLES', '')
# AGENT_DEFAULT_MODE: 默认问答模式（auto / rag / agent）
# auto: Agent 模式，LLM 自主决定是否调用工具
# rag: 传统 RAG 模式，预检索 + LLM 生成
# agent: 强制 Agent 模式
AGENT_DEFAULT_MODE = os.getenv('AGENT_DEFAULT_MODE', 'auto')

# --- 敏感词流式审查（输出侧内容安全防线）---
# 总开关：关闭后 LLM 输出不再过审（仅在调试或低风险场景关闭）
SENSITIVE_FILTER_ENABLED = os.getenv('SENSITIVE_FILTER_ENABLED', '1') == '1'
# 累积多少字符送审一次（过小会增加审查开销，过大延迟感知到违规）
SENSITIVE_FILTER_CHUNK_SIZE = int(os.getenv('SENSITIVE_FILTER_CHUNK_SIZE', '32'))
# 滑动窗口大小：保留尾部 N 字符防止关键词被 delta 边界切分
SENSITIVE_FILTER_WINDOW_SIZE = int(os.getenv('SENSITIVE_FILTER_WINDOW_SIZE', '16'))
# 脱敏替换字符串（mask 动作使用）
SENSITIVE_FILTER_MASK_STR = os.getenv('SENSITIVE_FILTER_MASK_STR', '***')
# 词库缓存 TTL 秒：超过后自动从 DB 刷新（避免每次请求都查库）
SENSITIVE_FILTER_RELOAD_TTL = int(os.getenv('SENSITIVE_FILTER_RELOAD_TTL', '300'))

# --- Embedding & Rerank ---
# 使用通用变量名，支持切换不同平台
# base_url 已迁移到模型管理（LLMModel 表），不再作为系统配置项
EMBEDDING_API_KEY = EmbeddingConfig.api_key()
EMBEDDING_MODEL = EmbeddingConfig.model()
EMBEDDING_DIM = EmbeddingConfig.dim()
RERANK_MODEL = EmbeddingConfig.rerank_model()

# --- Embedding Provider 切换开关 ---
# docker: 优先使用 Docker Embedding 服务（本地部署）
# api:    优先使用云 API（如 SiliconFlow）
EMBEDDING_PROVIDER = EmbeddingConfig.provider()

# --- Docker Embedding 配置（预留）---
EMBEDDING_DOCKER_URL = EmbeddingConfig.docker_url()
EMBEDDING_DOCKER_TIMEOUT = EmbeddingConfig.docker_timeout()

# --- 图片存储模式 ---
IMAGE_STORAGE_MODE = os.getenv('IMAGE_STORAGE_MODE', 'base64')  # base64 / oss

# --- 文档存储配置 ---
DOCUMENT_STORAGE_MODE = os.getenv('DOCUMENT_STORAGE_MODE', 'local')  # local / oss
DOCUMENT_RETENTION_ENABLED = os.getenv('DOCUMENT_RETENTION_ENABLED', '1') == '1'
DOCUMENT_MAX_SIZE_MB = int(os.getenv('DOCUMENT_MAX_SIZE_MB', '100'))

# --- OSS 配置 ---
OSS_ENDPOINT = os.getenv('OSS_ENDPOINT', '')
OSS_ACCESS_KEY_ID = os.getenv('OSS_ACCESS_KEY_ID', '')
OSS_ACCESS_KEY_SECRET = os.getenv('OSS_ACCESS_KEY_SECRET', '')
OSS_BUCKET_NAME = os.getenv('OSS_BUCKET_NAME', '')
OSS_REGION = os.getenv('OSS_REGION', '')

# --- 检索参数（可被 system_config 覆盖）---
RETRIEVAL_TOP_K = int(os.getenv('RETRIEVAL_TOP_K', '20'))
RETRIEVAL_RERANK_TOP_K = int(os.getenv('RETRIEVAL_RERANK_TOP_K', '5'))
HNSW_EF_SEARCH = int(os.getenv('HNSW_EF_SEARCH', '40'))
BM25_TOP_K = int(os.getenv('BM25_TOP_K', '30'))
VECTOR_TOP_K = int(os.getenv('VECTOR_TOP_K', '30'))

# --- 记忆 Token 预算 ---
MEMORY_TOKEN_BUDGET = int(os.getenv('MEMORY_TOKEN_BUDGET', '8000'))
SHORT_TERM_TTL = int(os.getenv('SHORT_TERM_TTL', '3600'))  # 短时记忆 TTL 秒
SHORT_TERM_MAX_TURNS = int(os.getenv('SHORT_TERM_MAX_TURNS', '6'))

# --- 安全参数 ---
MAX_LOGIN_FAIL = int(os.getenv('MAX_LOGIN_FAIL', '5'))
BAN_DURATION_MIN = int(os.getenv('BAN_DURATION_MIN', '15'))

# --- 邮件服务（SMTP）---
# 用于密码重置、系统通知等场景；未配置 EMAIL_HOST 时不发送邮件
EMAIL_ENABLED = os.getenv('EMAIL_ENABLED', '0') == '1'
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend' if EMAIL_ENABLED else 'django.core.mail.backends.console.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '465'))
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', '1') == '1'
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', '0') == '1'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_FROM = os.getenv('EMAIL_FROM', EMAIL_HOST_USER)
# 密码重置链接有效期（秒），默认 5 分钟
PASSWORD_RESET_TIMEOUT = int(os.getenv('PASSWORD_RESET_TIMEOUT', '300'))
# 前端基础地址（用于拼接重置密码页面链接）
FRONTEND_BASE_URL = os.getenv('FRONTEND_BASE_URL', 'http://localhost:8080')

# --- 日志 ---
class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == record.pathname):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _extract_class_name(frame):
    if frame is None:
        return ""
    args, _, _, values = inspect.getargvalues(frame)
    if len(args) > 0 and args[0] in ("self", "cls"):
        instance = values.get(args[0])
        if instance is not None:
            return instance.__class__.__name__
    return ""


logger.remove()
logger.configure(
    patcher=lambda record: record.update(
        {"class_name": next((_extract_class_name(f.frame) for f in inspect.stack() if f.function != "_extract_class_name"), "")}
    )
)
logger.add(
    sys.stdout,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
        "<level>{level: <8}</level> "
        "<cyan>{module}</cyan> "
        "<magenta>{class_name}</magenta> "
        "<blue>{function}</blue> "
        "<yellow>:{line}</yellow> "
        "- <level>{message}</level>"
    ),
    level="INFO",
    colorize=True,
    backtrace=True,
    diagnose=True,
)

logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
for name in logging.root.manager.loggerDict:
    if name.startswith(("django", "apps", "celery", "uvicorn")):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).addHandler(InterceptHandler())
        logging.getLogger(name).propagate = False

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'loguru': {'class': __name__ + '.InterceptHandler'}},
    'root': {'handlers': ['loguru'], 'level': 'INFO'},
    'loggers': {
        'django': {'handlers': ['loguru'], 'level': 'INFO', 'propagate': False},
        'apps': {'handlers': ['loguru'], 'level': 'INFO', 'propagate': False},
        'celery': {'handlers': ['loguru'], 'level': 'INFO', 'propagate': False},
        'uvicorn': {'handlers': ['loguru'], 'level': 'INFO', 'propagate': False},
    },
}
