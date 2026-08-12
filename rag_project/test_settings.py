"""
测试环境专用 settings 配置
- 数据库: rag_test（独立测试库，不影响生产数据）
- Celery: 同步执行，不涉及异步队列
- 邮件: 控制台输出，不实际发送
"""
import os
import sys
from pathlib import Path

# 加载 .env 文件（pytest 不经过 manage.py，需手动加载）
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

# 使用测试数据库
os.environ['PG_DB_DATABASE'] = 'rag_test'

# 先加载基础 settings
from .settings import *  # noqa: F401,F403,E402

# --- 测试数据库配置 ---
# 主数据库：rag_test（预先创建好，已安装 pgvector 扩展，用于手动调试）
# Django 测试框架（含 pytest-django 的 django_db）会自动创建 rag_test_test 作为测试数据库
# pgvector 扩展通过 retrieval app 的 0002 迁移自动安装
# 测试环境默认使用原生后端（不用连接池），因为测试用例按序执行，无需池化；
# 若需压测连接池行为，可将 ENGINE 改为 rag_project.db.pooled_postgresql 并配置 POOL_OPTIONS
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        # TEST_DB_NAME 用于并行测试隔离（pytest-django 会在其后加 test_ 前缀）
        'NAME': os.getenv('TEST_DB_NAME', 'rag_test'),
        'USER': os.getenv('PG_DB_USER', 'root'),
        # 密码一律从 .env 读取，代码内不保留默认口令（避免真实密码随代码泄露）；
        # 缺省时留空会让连接校验失败，迫使部署方显式配置
        'PASSWORD': os.getenv('PG_DB_PASSWORD', 'password'),
        'HOST': os.getenv('PG_DB_HOST', 'localhost'),
        'PORT': os.getenv('PG_DB_PORT', '5432'),
        # CONN_MAX_AGE 在测试中不影响正确性，仅需保持连接不过期
        'CONN_MAX_AGE': 60,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}

# --- 测试配置 ---
# Celery 任务同步执行，避免异步问题
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# 邮件使用控制台后端
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# 密码哈希使用快速算法（加速测试）
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# 日志级别：WARNING（减少噪音）
LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'WARNING',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

# 敏感词审查：测试时默认关闭（避免依赖词库数据）
SENSITIVE_FILTER_ENABLED = os.getenv('TEST_SENSITIVE_FILTER_ENABLED', '0') == '1'

# --- 精简中间件（降低每个请求的内存 & IO 开销） ---
# AuditMiddleware: 每次 POST/PUT/DELETE 都写 AuditLog DB 记录，测试时无意义；
#   其单元测试（test_middleware.py）直接实例化类测试，不依赖 MIDDLEWARE 列表。
# SlowRequestMiddleware: 仅记录慢请求日志，测试中无用。
# WhiteNoiseMiddleware: 测试不涉及静态文件服务。
# 注意：IpFilterMiddleware 必须保留 —— test_views_middleware.py 的集成测试
#   依赖真实中间件链路验证白名单/黑名单/过期自动解封行为。
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.security.middleware.IpFilterMiddleware',   # IP 白/黑名单（集成测试依赖）
]

# --- 禁用 loguru 文件日志（测试中只保留 stdout WARNING+，减少 IO） ---
from loguru import logger as _test_logger
_test_logger.remove()
_test_logger.add(sys.stdout, level='WARNING',
                 format='{time:HH:mm:ss} | {level: <7} | {message}',
                 colorize=False)
