"""
配置模块 - 环境变量与业务配置统一入口

配置来源分两类：
1. 基础设施配置（数据库 / Redis / Celery / SECRET_KEY / API Key 等）：
   仅存 env，不在 DB 中管理，因为修改这些需要重启服务或涉及安全凭证。
2. 业务配置（LLM 模型选择 / 超时 / 评估参数 / 检索参数等）：
   统一存 SystemConfig 表，通过 config_loader.get_config_value 读取（内存→Redis→DB 三层缓存）。
   配置变更后通过工单审批流程修改，无需重启服务。
"""
import os
import sys
from pathlib import Path

from loguru import logger


def _get_db_config(key: str, default, value_type: str = 'string'):
    """从 SystemConfig 表读取业务配置（带三层缓存：内存→Redis→DB）

    在 settings.py 初始化阶段调用时，Django ORM 可能尚未就绪，
    此时 get_config_value 会捕获异常并返回 default，与原 os.getenv 行为一致。

    Args:
        key: SystemConfig 中的配置 key
        default: DB 不可用或未配置时的兜底值
        value_type: string/int/float/bool/json
    Returns:
        按类型转换后的配置值
    """
    from apps.system.config_loader import get_config_value
    return get_config_value(key, default=default, value_type=value_type)


class DatabaseConfig:
    """数据库配置（基础设施，仅从 env 读取）"""

    @staticmethod
    def build_url() -> str:
        """优先读 DATABASE_URL，否则从 PG_DB_* 拼接"""
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
    """Redis配置（基础设施，仅从 env 读取）"""

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
    """LLM配置 - 支持双模型（基础模型+高级模型）

    API Key 属敏感凭证，仅从 env 读取；模型选择和超时从 DB 读取（可在线变更）。
    """

    @staticmethod
    def api_key() -> str:
        """API Key 属敏感凭证，仅从 env 读取，不入库"""
        return os.getenv('LLM_API_KEY', '')

    @staticmethod
    def default_model() -> str:
        """默认模型（基础模型，用于简单任务），从 DB 读取可在线变更"""
        return _get_db_config('LLM_BASE_MODEL', default='deepseek-v4-flash', value_type='string')

    @staticmethod
    def advanced_model() -> str:
        """高级模型（用于复杂任务），从 DB 读取可在线变更"""
        return _get_db_config('LLM_ADVANCED_MODEL', default='deepseek-v4-pro', value_type='string')

    @staticmethod
    def timeout() -> int:
        """LLM 调用超时（秒），从 DB 读取可在线变更"""
        return _get_db_config('LLM_TIMEOUT', default=60, value_type='int')


class EmbeddingConfig:
    """Embedding & Rerank配置

    API Key 属敏感凭证，仅从 env 读取；模型选择和参数从 DB 读取（可在线变更）。
    EMBEDDING_DIM 为只读项（修改需重建索引），但仍从 DB 读取以保持统一入口。
    """

    @staticmethod
    def api_key() -> str:
        """API Key 属敏感凭证，仅从 env 读取，不入库"""
        return os.getenv('EMBEDDING_API_KEY', '')

    @staticmethod
    def model() -> str:
        """Embedding 模型名，从 DB 读取可在线变更"""
        return _get_db_config('EMBEDDING_MODEL', default='BAAI/bge-m3', value_type='string')

    @staticmethod
    def dim() -> int:
        """向量维度，只读项（修改需重建索引），从 DB 读取但前端禁止修改"""
        return _get_db_config('EMBEDDING_DIM', default=1024, value_type='int')

    @staticmethod
    def rerank_model() -> str:
        """Rerank 模型名，从 DB 读取可在线变更"""
        return _get_db_config('RERANK_MODEL', default='BAAI/bge-reranker-v2-m3', value_type='string')

    @staticmethod
    def provider() -> str:
        """embedding provider: docker / api，从 DB 读取可在线变更"""
        return _get_db_config('EMBEDDING_PROVIDER', default='api', value_type='string')

    @staticmethod
    def docker_url() -> str:
        """Docker 服务地址，从 DB 读取可在线变更"""
        return _get_db_config('EMBEDDING_DOCKER_URL', default='', value_type='string')

    @staticmethod
    def docker_timeout() -> int:
        """Docker 调用超时（秒），从 DB 读取可在线变更"""
        return _get_db_config('EMBEDDING_DOCKER_TIMEOUT', default=30, value_type='int')


class CeleryConfig:
    """Celery配置（基础设施，仅从 env 读取）"""

    @staticmethod
    def broker_url() -> str:
        return os.getenv('CELERY_BROKER_URL', RedisConfig.build_url(db=1) or 'redis://localhost:6379/1')

    @staticmethod
    def result_backend() -> str:
        return os.getenv('CELERY_RESULT_BACKEND', RedisConfig.build_url(db=2) or 'redis://localhost:6379/2')


class SecurityConfig:
    """安全配置（基础设施，仅从 env 读取）"""

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
    """CORS配置（基础设施，仅从 env 读取）"""

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


class AnalyticsConfig:
    """Analytics 配置 - 监控指标 & 忠实度评估 & 队列监控

    - 业务参数统一从 SystemConfig 表读取（通过 _get_db_config 三层缓存），可在线变更
    - ANALYTICS_REDIS_DB 属基础设施配置，仍从 env 读取
    - 忠实度评估的开关/批量/日限/成本限分离，灵活控制成本和覆盖度
    - 队列监控可独立开关，生产环境故障时可临时关闭以减压
    - Redis DB 选择：Celery broker=DB1, result_backend=DB2, Analytics=DB3（避免冲突）
    """

    @staticmethod
    def redis_db() -> int:
        """Analytics 专用 Redis DB（默认 3，与 Celery broker/result backend 隔离）
        属基础设施配置，仅从 env 读取
        """
        return int(os.getenv('ANALYTICS_REDIS_DB', '3'))

    @staticmethod
    def eval_enabled() -> bool:
        """是否启用评估总开关，从 DB 读取可在线变更"""
        return _get_db_config('EVAL_ENABLED', default=True, value_type='bool')

    @staticmethod
    def eval_daily_limit() -> int:
        """每日评估总量上限（默认 500，防止成本失控），从 DB 读取可在线变更"""
        return _get_db_config('EVAL_DAILY_LIMIT', default=500, value_type='int')

    @staticmethod
    def eval_model() -> str:
        """评估所用模型（默认 deepseek-chat），从 DB 读取可在线变更"""
        return _get_db_config('EVAL_MODEL', default='deepseek-chat', value_type='string')

    @staticmethod
    def eval_cost_limit() -> float:
        """每日评估成本上限（元，默认 1.0），从 DB 读取可在线变更"""
        return _get_db_config('EVAL_COST_LIMIT', default=1.0, value_type='float')

    # --- 生产对话自动评估（内联采样 + 限速）---
    # 对话持久化后按采样率 + 令牌桶限速异步触发 DeepEval 12 维评估
    # (evaluate_with_deepeval)，与定时批量任务 run_multi_dimension_evaluation 互补:
    # 采样负责即时代表性样本，批量负责回扫未采样项；默认关闭，按需开启

    @staticmethod
    def production_eval_enabled() -> bool:
        """是否启用生产对话内联采样评估（默认关闭，按需开启），从 DB 读取可在线变更"""
        return _get_db_config('PRODUCTION_EVAL_ENABLED', default=False, value_type='bool')

    @staticmethod
    def production_eval_sample_rate() -> float:
        """采样率（0~1，默认 0.05 即 5% 对话触发评估）
        采样而非全量，兼顾覆盖度与成本；配合 rate_per_min 做双重限速
        从 DB 读取可在线变更
        """
        return _get_db_config('PRODUCTION_EVAL_SAMPLE_RATE', default=0.05, value_type='float')

    @staticmethod
    def production_eval_rate_per_min() -> int:
        """每分钟最大评估请求数（默认 5）
        防止高峰对话量打爆 LLM 评估接口；与 hourly/daily_limit 三重保护
        从 DB 读取可在线变更
        """
        return _get_db_config('PRODUCTION_EVAL_RATE_PER_MIN', default=5, value_type='int')

    @staticmethod
    def production_eval_rate_per_hour() -> int:
        """每小时最大评估请求数（默认 50）
        与 per_min / daily_limit 配合做分层限速，超出排队到下一小时
        从 DB 读取可在线变更
        """
        return _get_db_config('PRODUCTION_EVAL_RATE_PER_HOUR', default=50, value_type='int')

    @staticmethod
    def production_eval_hourly_guarantee() -> int:
        """每小时保底评估条数（默认 0，已禁用）

        保底机制已废弃：没有对话时无法进行评估，保底逻辑不再适用。
        保留方法签名用于向后兼容，默认返回 0 表示禁用。
        """
        return 0

    @staticmethod
    def production_eval_daily_guarantee() -> int:
        """每日保底评估上限（默认 0，已禁用）

        保底机制已废弃：没有对话时无法进行评估，保底逻辑不再适用。
        保留方法签名用于向后兼容，默认返回 0 表示禁用。
        """
        return 0

    @staticmethod
    def production_eval_batch_size() -> int:
        """2h 批量回扫每次评估条数（默认 10）

        run_multi_dimension_evaluation 每 2 小时执行一次,从未评估的 QA 中
        随机取 X 条评估。混合时间窗:优先取最近 2h,不足时扩展到当天。
        从 DB 读取可在线变更
        """
        return _get_db_config('PRODUCTION_EVAL_BATCH_SIZE', default=10, value_type='int')

    @staticmethod
    def production_eval_metric_groups() -> list:
        """生产评估启用的指标组（默认 all 全部 12 维）

        可选值(逗号分隔组合,小写):
        - all: 全部 12 维(默认,指标最全面)
        - core: 核心质量(2维) faithfulness + answer_relevancy
        - retrieval: 检索质量(2维) context_relevancy + hallucination
        - safety: 安全性(2维) toxicity + bias
        - quality: 答案质量(3维) completeness + conciseness + clarity
        - business: 业务体验(3维) professionalism + helpfulness + actionability

        降本场景示例:
        - PRODUCTION_EVAL_METRIC_GROUPS=core,safety  → 4 维,核心+安全
        - PRODUCTION_EVAL_METRIC_GROUPS=core         → 2 维,最低成本
        - 不设置或 all                               → 12 维,全覆盖

        从 DB 读取可在线变更
        """
        raw = _get_db_config('PRODUCTION_EVAL_METRIC_GROUPS', default='all', value_type='string')
        if not raw:
            return ['all']
        return [g.strip() for g in raw.split(',') if g.strip()]

    @staticmethod
    def queue_monitor_enabled() -> bool:
        """是否启用队列深度监控（生产故障时可临时关闭），从 DB 读取可在线变更"""
        return _get_db_config('QUEUE_MONITOR_ENABLED', default=True, value_type='bool')

    # --- 低分回归测试集 ---
    # 从生产低分对话沉淀为回归测试集,防止已知 bad case 在迭代中退化。
    # 通过阈值(均分 ≥ 视为通过)与连续通过次数(pass_count)控制移除流程,
    # 最终移除决策由人工 review,pass_count 仅作辅助提示。

    @staticmethod
    def low_score_regression_enabled() -> bool:
        """是否启用低分回归测试集(沉淀 + 定时评估,默认开启)
        关闭后定时任务跳过,手动触发仍可用。从 DB 读取可在线变更
        """
        return _get_db_config('LOW_SCORE_REGRESSION_ENABLED', default=True, value_type='bool')

    @staticmethod
    def low_score_regression_top_n() -> int:
        """每次沉淀从低分对话中取的 top N 数量(默认 50)
        按均分升序取最低分的前 N 条,作为回归测试集候选。从 DB 读取可在线变更
        """
        return _get_db_config('LOW_SCORE_REGRESSION_TOP_N', default=50, value_type='int')

    @staticmethod
    def low_score_regression_pass_threshold() -> float:
        """回归评估通过阈值(默认 0.7)
        全链路 12 维评估均分 ≥ 该值视为通过,pass_count += 1;否则重置为 0
        从 DB 读取可在线变更
        """
        return _get_db_config('LOW_SCORE_REGRESSION_PASS_THRESHOLD', default=0.7, value_type='float')

    @staticmethod
    def low_score_regression_capacity() -> int:
        """低分回归测试集容量上限(默认 200)
        超出时按 pass_count 降序 + last_eval_at 升序淘汰(优先移除已多次通过的旧记录)
        从 DB 读取可在线变更
        """
        return _get_db_config('LOW_SCORE_REGRESSION_CAPACITY', default=200, value_type='int')

    @staticmethod
    def low_score_regression_suggest_remove_passes() -> int:
        """建议人工移除的连续通过次数阈值(默认 3)
        pass_count 达到该值时前端高亮提示"建议 review 移除",但不自动删除
        从 DB 读取可在线变更
        """
        return _get_db_config('LOW_SCORE_REGRESSION_SUGGEST_REMOVE_PASSES', default=3, value_type='int')
