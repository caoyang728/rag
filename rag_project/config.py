"""
配置模块 - 独立提取环境变量和日志配置
避免将所有配置混杂在 Django settings.py 中
"""
import os
import sys
from pathlib import Path

from loguru import logger


class DatabaseConfig:
    """数据库配置"""

    @staticmethod
    def build_url() -> str:
        """优先读 REDIS_URL，否则从 PG_DB_* 拼接"""
        url = os.getenv('DATABASE_URL')
        if url:
            return url
        host = os.getenv('PG_DB_HOST', 'localhost')
        port = os.getenv('PG_DB_PORT', '5432')
        db = os.getenv('PG_DB_DATABASE', 'rag_agent')
        user = os.getenv('PG_DB_USER', 'rag_user')
        password = os.getenv('PG_DB_PASSWORD', 'rag_pass_2026')
        return f'postgres://{user}:{password}@{host}:{port}/{db}'

    @staticmethod
    def conn_max_age() -> int:
        return int(os.getenv('PG_CONN_MAX_AGE', '60'))


class RedisConfig:
    """Redis配置"""

    @staticmethod
    def build_url(db: int = 0) -> str:
        """优先读 REDIS_URL，否则从 REDIS_DB_* 拼接"""
        url = os.getenv('REDIS_URL')
        if url:
            return url
        host = os.getenv('REDIS_DB_HOST', '')
        if not host:
            return ''
        port = os.getenv('REDIS_DB_PORT', '6379')
        password = os.getenv('REDIS_DB_PASSWORD', '')
        if password:
            return f'redis://:{password}@{host}:{port}/{db}'
        return f'redis://{host}:{port}/{db}'


class LLMConfig:
    """LLM配置 - 支持双模型（基础模型+高级模型）"""

    @staticmethod
    def api_key() -> str:
        return os.getenv('LLM_API_KEY', '')

    @staticmethod
    def base_url() -> str:
        return os.getenv('LLM_BASE_URL', 'https://api.deepseek.com')

    @staticmethod
    def default_model() -> str:
        """默认模型（基础模型，用于简单任务）"""
        return os.getenv('LLM_BASE_MODEL', 'deepseek-v4-flash')

    @staticmethod
    def advanced_model() -> str:
        """高级模型（用于复杂任务）"""
        return os.getenv('LLM_ADVANCED_MODEL', 'deepseek-v4-pro')

    @staticmethod
    def timeout() -> int:
        return int(os.getenv('LLM_TIMEOUT', '60'))


class EmbeddingConfig:
    """Embedding & Rerank配置"""

    @staticmethod
    def api_key() -> str:
        return os.getenv('EMBEDDING_API_KEY', '')

    @staticmethod
    def base_url() -> str:
        return os.getenv('EMBEDDING_BASE_URL', 'https://api.siliconflow.cn/v1')

    @staticmethod
    def model() -> str:
        return os.getenv('EMBEDDING_MODEL', 'BAAI/bge-m3')

    @staticmethod
    def dim() -> int:
        return int(os.getenv('EMBEDDING_DIM', '1024'))

    @staticmethod
    def rerank_model() -> str:
        return os.getenv('RERANK_MODEL', 'BAAI/bge-reranker-v2-m3')

    @staticmethod
    def provider() -> str:
        """embedding provider: docker / api"""
        return os.getenv('EMBEDDING_PROVIDER', 'docker')

    @staticmethod
    def docker_url() -> str:
        return os.getenv('EMBEDDING_DOCKER_URL', '')

    @staticmethod
    def docker_timeout() -> int:
        return int(os.getenv('EMBEDDING_DOCKER_TIMEOUT', '30'))


class CeleryConfig:
    """Celery配置"""

    @staticmethod
    def broker_url() -> str:
        return os.getenv('CELERY_BROKER_URL', RedisConfig.build_url(db=1) or 'redis://localhost:6379/1')

    @staticmethod
    def result_backend() -> str:
        return os.getenv('CELERY_RESULT_BACKEND', RedisConfig.build_url(db=2) or 'redis://localhost:6379/2')


class SecurityConfig:
    """安全配置"""

    @staticmethod
    def secret_key() -> str:
        key = os.getenv('SECRET_KEY')
        if not key:
            raise RuntimeError("SECRET_KEY 环境变量必须设置，请在 .env 中配置")
        return key

    @staticmethod
    def debug() -> bool:
        return os.getenv('DEBUG', '0') == '1'

    @staticmethod
    def allowed_hosts() -> list:
        return os.getenv('ALLOWED_HOSTS', '*').split(',')


class CorsConfig:
    """CORS配置"""

    @staticmethod
    def allow_all_origins() -> bool:
        return SecurityConfig.debug()

    @staticmethod
    def allowed_origins() -> list:
        return os.getenv('CORS_ORIGINS', 'http://localhost,http://127.0.0.1').split(',')


class LogConfig:
    """日志配置"""

    @staticmethod
    def setup_logger(log_dir: str = None):
        """配置 loguru 日志"""
        if not log_dir:
            log_dir = os.path.join(Path(__file__).resolve().parent.parent, 'logs')

        # 创建日志目录
        os.makedirs(log_dir, exist_ok=True)

        # 清除默认配置
        logger.remove()

        # 终端输出（开发环境）
        logger.add(
            sys.stdout,
            level='INFO',
            format='<green>{time:YYYY-MM-DD HH:mm:ss}</green> | '
                   '<level>{level: <8}</level> | '
                   '<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - '
                   '<level>{message}</level>',
            colorize=True,
        )

        # 错误日志文件（仅 ERROR）
        logger.add(
            os.path.join(log_dir, 'error_{time:YYYY-MM-DD}.log'),
            level='ERROR',
            format='{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}',
            rotation='00:00',
            retention='7 days',
            encoding='utf-8',
        )

        # 完整日志文件（DEBUG及以上）
        logger.add(
            os.path.join(log_dir, 'app_{time:YYYY-MM-DD}.log'),
            level='DEBUG',
            format='{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}',
            rotation='00:00',
            retention='30 days',
            encoding='utf-8',
        )

        # 慢请求日志（用于性能分析）
        logger.add(
            os.path.join(log_dir, 'slow_{time:YYYY-MM-DD}.log'),
            level='WARNING',
            format='{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}',
            rotation='00:00',
            retention='7 days',
            encoding='utf-8',
            filter=lambda record: 'slow' in record['message'].lower() or '耗时' in record['message'],
        )

        logger.info('[LogConfig] 日志系统初始化完成')
