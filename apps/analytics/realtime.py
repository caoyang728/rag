"""
Analytics realtime - Redis 实时指标 & 队列深度操作封装

- 实时指标：QaRecord 创建时原子 HINCRBY，Dashboard 直接读 Hash，每 5 分钟刷新
- 队列深度：Celery Beat 每 5 分钟 LLEN 查询 + 写入 PG 历史表
- 使用 Analytics 专用 Redis DB（默认 3），独立于 Celery broker/result backend
- 所有 Redis 操作均使用 Pipeline 批量执行，减少网络往返
- 连接健康检查采用 TTL 缓存（60s），避免热点路径上每次请求都 ping
"""
import os
import time
from functools import lru_cache

from loguru import logger

from django.utils import timezone

from rag_project.config import AnalyticsConfig

# 队列监控的队列名列表（与 settings.py CELERY_TASK_QUEUES 保持一致）
QUEUE_NAMES = ['default', 'parse', 'memory', 'email', 'analytics']

# 历史数据保留天数
REALTIME_RETENTION_DAYS = 3

# Worker 状态在 Redis 的保留时长：大于 update_queue_depth 的执行间隔（5 分钟），
# 保证任务两次运行之间 API 始终能读到上一轮聚合结果
_WORKER_STATS_TTL = 10 * 60

# Redis 连接健康检查间隔（秒）
_HEALTH_CHECK_INTERVAL = 60
_last_health_check = 0


@lru_cache(maxsize=1)
def _get_redis():
    """获取 Analytics 专用 Redis 连接（DB 3，LRU 缓存复用）
    - 使用 lru_cache 缓存 Redis 连接实例，避免每次调用都创建新连接
    - 优先读 REDIS_URL，若不存在则从 REDIS_DB_HOST/PORT/PASSWORD 拼接
    - 返回 DB 3（原设计 DB 2 与 Celery result backend 冲突，已改为 3）
    - 线程安全：Redis 客户端是线程安全的，可在 Django 请求间复用
    - decode_responses=True 直接返回字符串，避免 bytes 解码
    """
    import redis as redis_lib
    from django.conf import settings
    from urllib.parse import urlparse

    # 优先使用 REDIS_URL 配置
    redis_url = getattr(settings, 'REDIS_URL', '')
    if redis_url:
        # 解析 URL 并切换到 Analytics 专用 DB
        parsed = urlparse(redis_url)
        # REDIS_URL 中已包含 DB，需替换为 Analytics 专用 DB
        analytics_db = AnalyticsConfig.redis_db()
        host = parsed.hostname or 'localhost'
        port = parsed.port or 6379
        password = parsed.password
        return redis_lib.Redis(
            host=host, port=port, password=password,
            db=analytics_db, decode_responses=True,
            max_connections=50, socket_connect_timeout=2, socket_timeout=2,
        )

    # 降级：从环境变量拼接
    host = os.getenv('REDIS_DB_HOST', 'localhost')
    port = int(os.getenv('REDIS_DB_PORT', '6379'))
    password = os.getenv('REDIS_DB_PASSWORD', '') or None
    return redis_lib.Redis(
        host=host, port=port, password=password,
        db=AnalyticsConfig.redis_db(), decode_responses=True,
        max_connections=50, socket_connect_timeout=2, socket_timeout=2,
    )


def _get_broker_redis():
    """获取 Celery broker 的 Redis 连接（用于 LLEN 真实队列长度）

    - 队列消息存于 Celery broker 的 Redis DB（默认 1，见 CELERY_BROKER_URL），
      与 Analytics 专用 DB（3）不同；此前直接复用 _get_redis() 会查错 DB，
      导致即使有积压任务队列深度也恒为 0
    - 直接从 settings.CELERY_BROKER_URL 解析端点（含 host/port/password/db），
      连接失败时返回 None，调用方按队列长度为 0 降级
    """
    import redis as redis_lib
    from django.conf import settings

    broker_url = getattr(settings, 'CELERY_BROKER_URL', '')
    if not broker_url:
        return None
    try:
        return redis_lib.Redis.from_url(
            broker_url, decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2,
        )
    except Exception:
        logger.debug('[QueueDepth] broker URL 解析失败，队列长度按 0 处理')
        return None


def _get_redis_safe():
    """获取 Redis 连接（带健康检查，TTL 缓存避免热点路径开销）

    - lru_cache 缓存的连接如果已经断开，直接用会抛 ConnectionError
    - 仅在距上次检查超过 _HEALTH_CHECK_INTERVAL 秒时才 ping 检查
    - 连接失效时清空 lru_cache 并重建
    - 这样热点路径（如 increment_realtime_metrics）大多数时候不触发 ping
    """
    global _last_health_check
    r = _get_redis()

    now = time.time()
    if now - _last_health_check > _HEALTH_CHECK_INTERVAL:
        try:
            r.ping()
            _last_health_check = now
        except Exception:
            logger.warning('[Analytics] Redis connection stale, recreating...')
            _get_redis.cache_clear()
            r = _get_redis()
            _last_health_check = time.time()

    return r


# ============================================================================
# 1. 队列深度操作（PG 历史 + Redis 实时）
# ============================================================================

def update_queue_depth():
    """更新所有队列深度（每 5 分钟由 Celery Beat 调用）

    - 通过 Redis LLEN 直接查询 Celery 队列长度（O(1) 操作）
    - 同时写入 Redis 当前值（供 API 实时查询）和 PG QueueDepthLog 历史表
    - PG 存储的好处：可展示更长时间的历史趋势（7 天+），
      Redis 仅用于当前值的快速查询
    - Worker 数量通过 Celery inspect API 获取（容忍分钟级延迟），
      失败时 worker_count=0 不影响核心指标
    """
    from apps.analytics.models import QueueDepthLog

    r = _get_redis_safe()
    now = timezone.now()

    # 先用 pipeline 批量查询所有队列长度（LLEN），
    # 避免逐个 LLEN 产生多次网络往返。
    # 队列消息在 broker Redis（默认 DB1），不能用 Analytics DB（3）查询，否则恒为 0；
    # Celery Redis 传输层的队列 key 就是队列名本身（如 parse），不带 celery: 前缀
    depths = [0] * len(QUEUE_NAMES)
    broker = _get_broker_redis()
    if broker:
        try:
            llen_pipe = broker.pipeline()
            for queue_name in QUEUE_NAMES:
                llen_pipe.llen(queue_name)
            depths = llen_pipe.execute()
        except Exception as e:
            logger.warning(f'[QueueDepth] broker LLEN 查询失败: {e}')

    pipeline = r.pipeline()
    logs = []
    for i, queue_name in enumerate(QUEUE_NAMES):
        depth = depths[i] or 0

        # 1) 更新 Redis 当前值（String，供实时查询）
        current_key = f'analytics:queue:current:{queue_name}'
        pipeline.set(current_key, depth, ex=24 * 3600)

        # 2) 准备写入 PG 历史记录
        # minute_bucket 截断到分钟，配合 unique_together 防止重复写入
        minute_bucket = now.replace(second=0, microsecond=0)
        logs.append(QueueDepthLog(
            queue_name=queue_name,
            depth=depth,
            worker_count=0,
            minute_bucket=minute_bucket,
        ))

    pipeline.execute()

    # --- 获取 Worker 数量（可选，失败时降级）---
    # 同时聚合完整 worker 状态（active/queued/idle）写入 Redis，
    # 供快照 API 秒级读取：inspect 是广播等待，实测每命令等待 2s+ 超时，
    # 若放 API 热路径会导致接口 4s+ 响应，故只在后台任务里执行一次。
    worker_count = 0
    worker_stats = {'active': None, 'queued': None, 'idle': None}
    try:
        # 复用 rag_project.celery.app 而非创建新实例，
        # 新实例无法连接到正在运行的 Worker，inspect 会返回空
        from rag_project.celery import app as celery_app
        insp = celery_app.control.inspect(timeout=3)
        if insp:
            active = insp.active() or {}
            reserved = insp.reserved() or {}
            # 活跃 Worker 总数（无响应时为空 dict，降级为 0）
            worker_count = max(1, len(active)) if active else 0
            # 正在执行的任务数（active）+ 已预取等待执行的任务数（reserved）
            total_active = sum(len(tasks) for tasks in active.values())
            total_reserved = sum(len(tasks) for tasks in reserved.values())
            # 空闲 Worker：总 Worker 数 - 有 active 任务的 Worker 数
            busy_workers = sum(1 for tasks in active.values() if len(tasks) > 0)
            worker_stats['active'] = total_active
            worker_stats['queued'] = total_reserved
            worker_stats['idle'] = max(0, worker_count - busy_workers)
    except Exception as e:
        logger.warning(f"[QueueDepth] Failed to inspect workers: {e}")

    # 写入 Redis worker 状态键（TTL 10 分钟，下次任务刷新覆盖）。
    # inspect 失败时各值保持 None，删除对应键，让前端显示 "-"
    try:
        _set_worker_stats(r, worker_stats)
    except Exception as e:
        logger.warning(f"[QueueDepth] Failed to write worker stats: {e}")

    for log in logs:
        log.worker_count = worker_count

    # --- 批量写入 PG（5 条记录）---
    # 注意：使用 ignore_conflicts=True 防止 Beat 重入时 IntegrityError
    # 当同一分钟内两个 Worker 同时写入时，唯一约束冲突的记录会被静默跳过，
    # 保证一个成功、另一个不报错
    try:
        QueueDepthLog.objects.bulk_create(logs, batch_size=50, ignore_conflicts=True)
    except Exception as e:
        logger.warning(f'[QueueDepth] bulk_create conflict (expected on Beat re-entry): {e}')

    logger.debug(
        f"[QueueDepth] Updated: {[(l.queue_name, l.depth) for l in logs]}, "
        f"workers={worker_count}"
    )


def _set_worker_stats(r, worker_stats):
    """把 worker 状态聚合结果写入 Redis 键（供快照 API 秒级读取）

    - 值非 None 时 SET 并带 TTL，None 时 DEL（表示该状态不可用）
    - pipeline 批量执行，一次网络往返
    """
    pipe = r.pipeline()
    for key, value in worker_stats.items():
        rk = f'analytics:queue:worker:{key}'
        if value is None:
            pipe.delete(rk)
        else:
            pipe.set(rk, value, ex=_WORKER_STATS_TTL)
    pipe.execute()


def get_queue_depth_snapshot():
    """获取所有队列当前深度快照（供 API 调用）

    返回结构与前端 JS 解析对齐：
      {queue_name: {size, length, queued, active, idle, failed}}
    - size/length：等待任务数（读取 Redis current 键，LLEN 兜底）
    - queued/active/idle/failed：Worker 状态（由 update_queue_depth 每 5 分钟
      聚合写入 Redis，此处只读；缺失为 None）

    优先读取 Redis current 键，避免每次都 LLEN 全队列。
    若 Redis current 键不存在（如服务刚重启），降级为直接 LLEN。

    注意：不在本函数内执行 Celery inspect——inspect 是广播等待，每命令
    会等满 timeout（实测 2s+），放 API 热路径会让接口响应 4s+；
    worker 状态属于分钟级延迟可接受的数据，统一由后台任务聚合。
    """
    r = _get_redis_safe()
    result = {}

    # 降级 LLEN 用的 broker 连接（队列消息所在 DB），只创建一次复用
    broker = _get_broker_redis()

    # 1) 获取 Worker 状态：只读 Redis（键由 update_queue_depth 任务写入），
    #    缺失/异常时为 None（前端显示 "-"），不影响队列长度返回
    worker_stats = {'queued': None, 'active': None, 'idle': None, 'failed': None}
    try:
        pipe = r.pipeline()
        for key in ('active', 'queued', 'idle'):
            pipe.get(f'analytics:queue:worker:{key}')
        vals = pipe.execute()
        for key, val in zip(('active', 'queued', 'idle'), vals):
            if val is not None:
                worker_stats[key] = int(val)
    except Exception as e:
        logger.debug(f'[QueueSnapshot] read worker stats failed: {e}')

    # 2) 获取队列等待长度（Redis current -> LLEN 降级）
    for queue_name in QUEUE_NAMES:
        current_key = f'analytics:queue:current:{queue_name}'
        val = r.get(current_key)
        if val is not None:
            size = int(val)
        elif broker:
            # 降级：直接 LLEN（Celery Redis 传输层队列 key 为队列名本身），
            # broker 连接失败时按 0 处理
            try:
                size = broker.llen(queue_name) or 0
            except Exception as e:
                logger.debug(f'[QueueSnapshot] LLEN failed for {queue_name}: {e}')
                size = 0
        else:
            size = 0
        result[queue_name] = {
            'size': size,
            'length': size,      # 兼容 JS：d.size || d.length
            'queued': worker_stats['queued'],
            'active': worker_stats['active'],
            'idle': worker_stats['idle'],
            'failed': worker_stats['failed'],
        }
    return result


# ============================================================================
# 2. 实时指标操作（Redis Hash，原子 INCR）
# ============================================================================

def increment_realtime_metrics(qa_record):
    """QaRecord 创建后调用，原子递增今日实时指标
    
    - 在 _persist_qa() 成功后调用，保证数据一致性
    - 使用 Redis Pipeline 批量执行 HINCRBY/HINCRBYFLOAT，减少网络往返
    - 缓存命中时仅累加 total_qa 和 cache_hits，不增加 Token/费用计数
    - 设置 3 天 TTL，防止数据无限累积
    - 指标用途：Dashboard 实时展示今日数据概览，精确 P50/P95 仍以 T+1 报表为准
    """
    r = _get_redis_safe()
    # 实时指标 key 按"今日"业务日期生成：timezone.now().date() 返回 UTC 日期，
    # 本地凌晨时段会落到前一天 key 上，导致 Dashboard 今日数据错位
    today = timezone.localdate().isoformat()
    key = f'analytics:realtime:{today}'

    pipe = r.pipeline()
    pipe.hincrby(key, 'total_qa', 1)

    if qa_record.is_hit_cache:
        pipe.hincrby(key, 'cache_hits', 1)
    else:
        pipe.hincrby(key, 'normal_qa', 1)
        # HINCRBYFLOAT 支持浮点累加，用于 Token 和费用
        # tokens_prompt / tokens_completion / cost_estimate 为空时安全降级为 0，
        # 避免 None → float(None) 的 TypeError（QaRecord 刚创建时某些字段可能还未回填）
        pipe.hincrbyfloat(key, 'tokens_prompt', float(qa_record.tokens_prompt or 0))
        pipe.hincrbyfloat(key, 'tokens_completion', float(qa_record.tokens_completion or 0))
        pipe.hincrbyfloat(key, 'cost_estimate', float(qa_record.cost_estimate or 0))

    if not qa_record.is_success:
        pipe.hincrby(key, 'llm_errors', 1)

    pipe.expire(key, REALTIME_RETENTION_DAYS * 86400)
    pipe.execute()


def get_realtime_snapshot():
    """获取今日实时指标快照（Dashboard 实时展示用）

    返回字典格式，包含 total_qa、cache_hits、llm_errors、tokens_*、cost_estimate、last_flush_at 等字段。
    所有字段均为累计值（非增量），直接展示即可。
    last_flush_at 用于 Dashboard 判断 Redis 数据是否新鲜（>10 分钟未刷新则降级）。
    """
    r = _get_redis_safe()
    today = timezone.localdate().isoformat()
    key = f'analytics:realtime:{today}'

    data = r.hgetall(key)
    return {
        'date': today,
        'total_qa': int(data.get('total_qa', 0)),
        'cache_hits': int(data.get('cache_hits', 0)),
        'normal_qa': int(data.get('normal_qa', 0)),
        'llm_errors': int(data.get('llm_errors', 0)),
        'tokens_prompt': round(float(data.get('tokens_prompt', 0)), 2),
        'tokens_completion': round(float(data.get('tokens_completion', 0)), 2),
        'cost_estimate': round(float(data.get('cost_estimate', 0)), 4),
        # last_flush_at 由 flush_realtime_metrics 写入，
        # Dashboard 可据此判断数据是否新鲜（默认 5 分钟刷新）
        'last_flush_at': int(data.get('last_flush_at', 0)),
    }


def flush_realtime_metrics():
    """每 5 分钟标记一次实时指标的同步时间
    
    - 实时指标主要用于 Dashboard 秒级展示，T+1 精确数据在 SystemMetricsReport 中持久化
    - 此函数仅更新 last_flush_at 标记，用于判断 Redis 数据是否新鲜
    - 不移动或删除数据，确保 Dashboard 始终可读取
    """
    r = _get_redis_safe()
    today = timezone.localdate().isoformat()
    key = f'analytics:realtime:{today}'

    r.hset(key, 'last_flush_at', int(timezone.now().timestamp()))
    logger.debug("[Realtime] Flushed realtime metrics timestamp")