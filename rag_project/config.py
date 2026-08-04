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


class AnalyticsConfig:
    """Analytics 配置 - 监控指标 & 忠实度评估 & 队列监控

    - 所有参数均通过 .env 控制，无需改代码即可调整
    - 忠实度评估的开关/批量/日限/成本限分离，灵活控制成本和覆盖度
    - 队列监控可独立开关，生产环境故障时可临时关闭以减压
    - Redis DB 选择：Celery broker=DB1, result_backend=DB2, Analytics=DB3（避免冲突）
    """

    @staticmethod
    def redis_db() -> int:
        """Analytics 专用 Redis DB（默认 3，与 Celery broker/result backend 隔离）"""
        return int(os.getenv('ANALYTICS_REDIS_DB', '3'))

    @staticmethod
    def _parse_bool(env_key: str, default: str = 'true') -> bool:
        """统一布尔环境变量解析（支持 1/0, true/false, yes/no 等格式）

        - Docker/Compose 环境变量常以字符串传递，需兼容多种格式
        - 接受值：'1','0','true','false','yes','no','on','off'（大小写不敏感）
        - 默认值 'true' 对应 Python 布尔 True
        """
        val = os.getenv(env_key, default).lower().strip()
        return val in ('1', 'true', 'yes', 'on')

    @staticmethod
    def eval_enabled() -> bool:
        """是否启用评估总开关"""
        return AnalyticsConfig._parse_bool('EVAL_ENABLED', 'true')

    @staticmethod
    def eval_daily_limit() -> int:
        """每日评估总量上限（默认 500，防止成本失控）"""
        return int(os.getenv('EVAL_DAILY_LIMIT', '500'))

    @staticmethod
    def eval_model() -> str:
        """评估所用模型（默认 deepseek-chat）"""
        return os.getenv('EVAL_MODEL', 'deepseek-chat')

    @staticmethod
    def eval_cost_limit() -> float:
        """每日评估成本上限（元，默认 1.0）"""
        return float(os.getenv('EVAL_COST_LIMIT', '1.0'))

    # --- 生产对话自动评估（内联采样 + 限速）---
    # 对话持久化后按采样率 + 令牌桶限速异步触发 DeepEval 12 维评估
    # (evaluate_with_deepeval)，与定时批量任务 run_multi_dimension_evaluation 互补:
    # 采样负责即时代表性样本，批量负责回扫未采样项；默认关闭，按需开启

    @staticmethod
    def production_eval_enabled() -> bool:
        """是否启用生产对话内联采样评估（默认关闭，按需开启）"""
        return AnalyticsConfig._parse_bool('PRODUCTION_EVAL_ENABLED', 'false')

    @staticmethod
    def production_eval_sample_rate() -> float:
        """采样率（0~1，默认 0.05 即 5% 对话触发评估）
        采样而非全量，兼顾覆盖度与成本；配合 rate_per_min 做双重限速
        """
        return float(os.getenv('PRODUCTION_EVAL_SAMPLE_RATE', '0.05'))

    @staticmethod
    def production_eval_rate_per_min() -> int:
        """每分钟最大评估请求数（令牌桶，默认 10）
        防止高峰对话量打爆 LLM 评估接口；与 daily_limit/cost_limit 三重保护
        """
        return int(os.getenv('PRODUCTION_EVAL_RATE_PER_MIN', '10'))

    @staticmethod
    def production_eval_hourly_guarantee() -> int:
        """每小时保底评估条数（默认 10）

        每小时前 N 条对话直接评估(不经采样率),保证低流量初期也有即时质量信号。
        Redis 小时计数器 analytics:eval_guarantee_hourly:{YYYYMMDDHH} 控制,
        超过 N 后降级为采样率兜底。与 daily_guarantee 组合:日保底达上限后,
        即使小时未满也不再保底。
        """
        return int(os.getenv('PRODUCTION_EVAL_HOURLY_GUARANTEE', '10'))

    @staticmethod
    def production_eval_daily_guarantee() -> int:
        """每日保底评估上限（默认 50）

        保底评估的日总量上限,防止高流量下保底成本失控。
        达到上限后,剩余对话全部走采样率兜底。与日预算上限(eval_daily_limit)
        互不冲突:保底上限是软目标,日预算是硬护栏。
        """
        return int(os.getenv('PRODUCTION_EVAL_DAILY_GUARANTEE', '50'))

    @staticmethod
    def production_eval_batch_size() -> int:
        """2h 批量回扫每次评估条数（默认 10）

        run_multi_dimension_evaluation 每 2 小时执行一次,从未评估的 QA 中
        随机取 X 条评估。混合时间窗:优先取最近 2h,不足时扩展到当天。
        """
        return int(os.getenv('PRODUCTION_EVAL_BATCH_SIZE', '10'))

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
        """
        raw = os.getenv('PRODUCTION_EVAL_METRIC_GROUPS', 'all').strip().lower()
        if not raw:
            return ['all']
        return [g.strip() for g in raw.split(',') if g.strip()]

    @staticmethod
    def queue_monitor_enabled() -> bool:
        """是否启用队列深度监控（生产故障时可临时关闭）"""
        return AnalyticsConfig._parse_bool('QUEUE_MONITOR_ENABLED', 'true')

    # --- 低分回归测试集 ---
    # 从生产低分对话沉淀为回归测试集,防止已知 bad case 在迭代中退化。
    # 通过阈值(均分 ≥ 视为通过)与连续通过次数(pass_count)控制移除流程,
    # 最终移除决策由人工 review,pass_count 仅作辅助提示。

    @staticmethod
    def low_score_regression_enabled() -> bool:
        """是否启用低分回归测试集(沉淀 + 定时评估,默认开启)
        关闭后定时任务跳过,手动触发仍可用
        """
        return AnalyticsConfig._parse_bool('LOW_SCORE_REGRESSION_ENABLED', 'true')

    @staticmethod
    def low_score_regression_top_n() -> int:
        """每次沉淀从低分对话中取的 top N 数量(默认 50)
        按均分升序取最低分的前 N 条,作为回归测试集候选
        """
        return int(os.getenv('LOW_SCORE_REGRESSION_TOP_N', '50'))

    @staticmethod
    def low_score_regression_pass_threshold() -> float:
        """回归评估通过阈值(默认 0.7)
        全链路 12 维评估均分 ≥ 该值视为通过,pass_count += 1;否则重置为 0
        """
        return float(os.getenv('LOW_SCORE_REGRESSION_PASS_THRESHOLD', '0.7'))

    @staticmethod
    def low_score_regression_capacity() -> int:
        """低分回归测试集容量上限(默认 200)
        超出时按 pass_count 降序 + last_eval_at 升序淘汰(优先移除已多次通过的旧记录)
        """
        return int(os.getenv('LOW_SCORE_REGRESSION_CAPACITY', '200'))

    @staticmethod
    def low_score_regression_suggest_remove_passes() -> int:
        """建议人工移除的连续通过次数阈值(默认 3)
        pass_count 达到该值时前端高亮提示"建议 review 移除",但不自动删除
        """
        return int(os.getenv('LOW_SCORE_REGRESSION_SUGGEST_REMOVE_PASSES', '3'))
