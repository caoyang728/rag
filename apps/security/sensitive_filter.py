"""
敏感词流式审查器 - 输出侧内容安全防线

核心能力：
1. AC 自动机（Aho-Corasick）多模式匹配：O(n) 一次扫描全部词库
2. 流式缓冲 + 滑动窗口：解决"关键词跨 token 边界"问题
3. 三种动作策略：block（拦截中断）/ mask（脱敏替换）/ warn（仅告警）
4. 正则词单独走 re 模块（用于手机号/身份证等模式匹配）
5. 单例 + TTL 刷新：避免每次请求查 DB；词库变更时通过 signal 触发重建

接入位置：
- apps/agent/react.py:agent_ask_stream 的 delta 循环
- apps/agent/executor.py:ask_stream 的 LLM stream 循环
- 缓存命中 / 任务拆分的一次性 delta 也会过审

事件协议（命中 block 时新增 SSE 事件）：
    {'type': 'content_filtered', 'reason': '检测到违规内容，已拦截', 'category': '...'}
    前端收到后：清空已展示内容 + 显示拦截提示卡片（含误判反馈按钮）
"""
import re
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from loguru import logger


# ============ AC 自动机（纯 Python 实现） ============

class _ACNode:
    """AC 自动机节点"""
    __slots__ = ('children', 'fail', 'output')

    def __init__(self):
        # children: char -> _ACNode
        self.children: Dict[str, '_ACNode'] = {}
        # fail 指针：失配时跳转的节点
        self.fail: Optional['_ACNode'] = None
        # output: 该节点对应的命中词列表（一个节点可能对应多个等长词）
        self.output: List[str] = []


class Ahocorasick:
    """纯 Python AC 自动机

    性能：1 万词 × 1KB 文本约 5ms（流式场景每次只审几十字符，微秒级）
    构建：O(词总长)；查询：O(文本长 + 命中数)

    AC 自动机适合多模式匹配场景：一次扫描即可返回所有命中的关键词，
    比逐个词 str.find 高效几个数量级（后者是 O(词数 × 文本长)）。
    """

    def __init__(self):
        self._root = _ACNode()
        self._built = False
        self._words: List[str] = []

    def add_word(self, word: str):
        """添加一个词到 Trie"""
        if not word:
            return
        node = self._root
        for ch in word:
            if ch not in node.children:
                node.children[ch] = _ACNode()
            node = node.children[ch]
        node.output.append(word)
        self._words.append(word)
        self._built = False  # 词库变更后需要重建 fail 指针

    def build(self):
        """构建 fail 指针（BFS）"""
        queue = deque()
        # 根的直接子节点：fail 指向根
        for child in self._root.children.values():
            child.fail = self._root
            queue.append(child)

        while queue:
            node = queue.popleft()
            for ch, child in node.children.items():
                queue.append(child)
                # 沿父节点的 fail 链查找匹配的 fail 节点
                f = node.fail
                while f is not None and ch not in f.children:
                    f = f.fail
                child.fail = f.children[ch] if f else self._root
                # 合并 fail 节点的 output（后缀词）
                child.output = child.output + child.fail.output

        self._built = True

    def search(self, text: str) -> List[Tuple[int, int, str]]:
        """在 text 中查找所有命中词

        Returns:
            [(start, end, word), ...]  end 是 exclusive
        """
        if not self._built:
            self.build()
        results = []
        node = self._root
        for i, ch in enumerate(text):
            # 失配时沿 fail 链回退
            while node is not self._root and ch not in node.children:
                node = node.fail
            if ch in node.children:
                node = node.children[ch]
            # 收集当前节点所有 output（含 fail 链继承的）
            if node.output:
                for word in node.output:
                    start = i - len(word) + 1
                    results.append((start, i + 1, word))
        return results

    @property
    def word_count(self):
        return len(self._words)


# ============ 命中结果 ============

class HitResult:
    """单次命中结果"""
    __slots__ = ('word', 'category', 'action', 'start', 'end')

    def __init__(self, word: str, category: str, action: str,
                 start: int = 0, end: int = 0):
        self.word = word
        self.category = category
        self.action = action  # block / mask / warn
        self.start = start
        self.end = end

    def __repr__(self):
        return f'Hit({self.word!r}, action={self.action}, cat={self.category})'


# ============ 敏感词过滤器 ============

class SensitiveFilter:
    """敏感词流式审查器（单例）

    使用方式（流式场景）::

        sf = SensitiveFilter.get_instance()
        state = sf.new_state()
        for chunk in llm.stream(...):
            delta = chunk.get('delta', '')
            if delta:
                outputs, hit = sf.feed(state, delta)
                if hit and hit.action == 'block':
                    yield {'type': 'content_filtered', 'reason': '...', 'category': hit.category}
                    return  # 立即中断 LLM 流
                for safe in outputs:
                    yield {'type': 'delta', 'delta': safe}
        # 流结束：flush 残余 buffer
        outputs, hit = sf.flush(state)
        ...

    配置项（settings.py）::
        SENSITIVE_FILTER_ENABLED: bool   总开关，默认 True
        SENSITIVE_FILTER_CHUNK_SIZE: int 累积多少字符送审一次，默认 32
        SENSITIVE_FILTER_WINDOW_SIZE: int 滑动窗口大小，默认 16
        SENSITIVE_FILTER_MASK_STR: str   脱敏替换字符串，默认 '***'
        SENSITIVE_FILTER_RELOAD_TTL: int 词库缓存 TTL 秒，默认 300

    多进程版本号同步（关键机制）::
        多 worker（gunicorn/uwsgi）部署下，一个进程内 force_reload() 只重载本进程词库，
        其他进程的内存实例仍是旧版本。通过 Redis 原子自增版本号实现跨进程广播：
        - force_reload() 完成后 INCR sensitive_filter:version
        - get_instance() 每次先 GET version，版本号增大时触发 reload（懒加载）
        - Redis 不可用时降级为 TTL 机制，不报错（宁可漏审不能崩溃）
    """

    _instance: Optional['SensitiveFilter'] = None
    _lock = threading.Lock()
    _reload_lock = threading.Lock()

    # Redis 版本号 key：用于多 worker 间广播词库变更
    _REDIS_VERSION_KEY = 'sensitive_filter:version'

    # 默认参数（被 settings.SENSITIVE_FILTER_* 覆盖，在 get_instance 时同步）
    CHUNK_SIZE = 32
    WINDOW_SIZE = 16
    MASK_STR = '***'
    RELOAD_TTL = 300  # 5 分钟刷新一次词库
    # buffer 最大长度上限：防止异常输入（如超大 delta 或误用 feed）导致 buffer 无限增长
    # 正常流式场景 buffer 约 CHUNK_SIZE + 单次 delta 长度，8KB 足够容纳 LLM 单次大片段
    MAX_BUFFER_SIZE = 8192

    # 分隔符：遇到这些字符时强制送审（避免长缓冲导致延迟）
    _SEPARATORS = set('。！？!?\n\r；;，,')

    @classmethod
    def _sync_config_from_settings(cls):
        """从 settings 同步配置参数（仅在单例首次创建时调用一次）

        注意：进程启动后阈值即冻结，运行时修改 settings 不会生效。
        如需运行时动态调整，请重启 worker 或调 SENSITIVE_FILTER_RELOAD_TTL 走 TTL 刷新。
        """
        try:
            cls.CHUNK_SIZE = int(getattr(settings, 'SENSITIVE_FILTER_CHUNK_SIZE', 32))
            cls.WINDOW_SIZE = int(getattr(settings, 'SENSITIVE_FILTER_WINDOW_SIZE', 16))
            cls.MASK_STR = getattr(settings, 'SENSITIVE_FILTER_MASK_STR', '***')
            cls.RELOAD_TTL = int(getattr(settings, 'SENSITIVE_FILTER_RELOAD_TTL', 300))
        except Exception:
            pass  # Django 未配置时用默认值

    # ---------- Redis 版本号：多 worker 下词库变更广播 ----------

    # Redis 客户端缓存：避免每次请求新建连接（连接池复用）
    _redis_client = None
    # Redis 熔断时间戳：连接失败后短时间内不再尝试，避免每请求都走一次失败 RTT
    _redis_unavailable_until = 0.0

    @classmethod
    def _get_redis(cls):
        """获取 Redis 连接（失败返回 None，降级走 TTL 机制）

        复用 Django settings.REDIS_URL 或默认环境变量；
        连接失败不报错——Redis 不可用不影响审查功能。
        采用"缓存客户端 + 熔断"策略：成功后长期复用连接池，失败后 5s 内不再尝试。
        """
        now = time.time()
        # 熔断窗口内直接降级，避免每请求都走失败 RTT
        if cls._redis_client is None and now < cls._redis_unavailable_until:
            return None
        if cls._redis_client is not None:
            return cls._redis_client
        try:
            import redis as redis_lib
            import os
            redis_url = getattr(settings, 'REDIS_URL', '')
            if redis_url:
                client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
            else:
                client = redis_lib.Redis(
                    host=os.getenv('REDIS_DB_HOST', 'redis'),
                    port=int(os.getenv('REDIS_DB_PORT', 6379)),
                    password=os.getenv('REDIS_DB_PASSWORD', ''),
                    decode_responses=True,
                    db=int(os.getenv('REDIS_DB_CAPTCHA', '1')),
                )
            client.ping()
            cls._redis_client = client
            return client
        except Exception:
            # 熔断 5s，期间所有 get_instance 走 TTL 兜底
            cls._redis_unavailable_until = now + 5
            return None

    @classmethod
    def _read_redis_version(cls, r=None) -> int:
        """读取 Redis 中的词库版本号（不存在返回 0）"""
        if r is None:
            r = cls._get_redis()
        if r is None:
            return 0
        try:
            v = r.get(cls._REDIS_VERSION_KEY)
            return int(v) if v else 0
        except Exception:
            # 连接异常：清空缓存客户端，下次 _get_redis 会重建
            cls._redis_client = None
            cls._redis_unavailable_until = time.time() + 5
            return 0

    @classmethod
    def _incr_redis_version(cls) -> int:
        """原子自增 Redis 版本号（词库变更后广播），返回新版本号"""
        r = cls._get_redis()
        if r is None:
            return 0
        try:
            new_v = r.incr(cls._REDIS_VERSION_KEY)
            logger.info('[SensitiveFilter] redis version INCR -> %d', new_v)
            return int(new_v)
        except Exception:
            # 连接异常：清空缓存客户端，下次 _get_redis 会重建
            cls._redis_client = None
            cls._redis_unavailable_until = time.time() + 5
            return 0

    def __init__(self):
        self._ac = Ahocorasick()
        self._regexes: List[Tuple[re.Pattern, str, str, str]] = []  # (pattern, word, category, action)
        self._word_meta: Dict[str, Dict] = {}  # word -> {category, action}
        self._loaded_at = 0.0
        self._version = 0  # 内存实例版本号（每次 _load_from_db +1）
        self._last_redis_version = 0  # 上次读到的 Redis 版本号，避免每次都 INCR 竞争

    @classmethod
    def get_instance(cls) -> 'SensitiveFilter':
        """单例获取，自动按 TTL 刷新词库

        首次调用会从 DB 加载词库并构建 AC 自动机；
        后续调用若超过 TTL 也会触发刷新（双检锁避免并发重建）；
        额外检查 Redis 版本号：若其他进程 CRUD 后自增了版本号，本进程也会重载。
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._sync_config_from_settings()
                    inst = cls()
                    # 首次初始化时也持 _reload_lock，避免与并发 force_reload 竞争同一实例
                    with cls._reload_lock:
                        inst._load_from_db()
                        # 初始载入后同步 Redis 版本号（避免下次 get_instance 立刻又触发 reload）
                        try:
                            inst._last_redis_version = cls._read_redis_version()
                        except Exception:
                            inst._last_redis_version = 0
                    # 构建完成后再发布到全局，避免其他线程拿到半成品实例
                    cls._instance = inst
        else:
            inst = cls._instance
            # 1. 优先检查 Redis 版本号（多 worker 词库变更广播）
            redis_v = cls._read_redis_version()
            if redis_v > 0 and redis_v != inst._last_redis_version:
                # 其他进程触发了 force_reload：本进程也立即重载（绕过 TTL 守卫）
                logger.info('[SensitiveFilter] redis version diverged local=%d redis=%d, reload',
                            inst._last_redis_version, redis_v)
                inst._force_reload_local(redis_v)
            else:
                # 2. TTL 兜底检查：超过 TTL 则后台刷新（不阻塞当前请求）
                if time.time() - inst._loaded_at > cls.RELOAD_TTL:
                    inst._maybe_reload()
        return cls._instance

    @classmethod
    def force_reload(cls):
        """强制重建词库（由敏感词 CRUD 信号触发）

        先重载本进程词库 → INCR Redis 版本号 → 其他进程在下一次 get_instance 时感知版本差并重载
        （Redis 不可用时降级为仅本进程重载，其他进程靠 TTL 兜底。
        """
        if cls._instance is not None:
            with cls._reload_lock:
                cls._instance._load_from_db()
                logger.info('[SensitiveFilter] force reloaded, version=%d',
                            cls._instance._version)
                # 更新进程内 _last_redis_version，避免 get_instance 马上重复 reload
                try:
                    new_v = cls._incr_redis_version()
                    cls._instance._last_redis_version = new_v or cls._instance._last_redis_version
                except Exception:
                    pass
        else:
            # 单例尚未创建：直接 INCR 版本号，等首次 get_instance 时加载最新词库
            cls._incr_redis_version()

    def _maybe_reload(self):
        """双检锁的 TTL 刷新（受 TTL 守卫限制）"""
        if time.time() - self._loaded_at < self.RELOAD_TTL:
            return
        with self._reload_lock:
            if time.time() - self._loaded_at < self.RELOAD_TTL:
                return
            self._load_from_db()

    def _force_reload_local(self, redis_v: int):
        """版本号分歧时立即重载（绕过 TTL 守卫）

        与 _maybe_reload 的区别：
        - _maybe_reload 受 TTL 守卫限制，TTL 窗口内不重载（用于定期刷新）
        - _force_reload_local 不受 TTL 限制，检测到其他进程 force_reload 后立即同步
        - 两者都在 _reload_lock 保护下，避免并发 _load_from_db

        Args:
            redis_v: 触发本次重载的 Redis 版本号（用于更新 _last_redis_version）
        """
        with self._reload_lock:
            # 二次校验：可能其他线程刚刚已完成重载并对齐了版本号
            if redis_v == self._last_redis_version:
                return
            self._load_from_db()
            self._last_redis_version = redis_v

    def _load_from_db(self):
        """从 DB 加载全部启用的敏感词，重建 AC 自动机

        重建期间用新对象替换 self._ac，避免影响正在进行的查询。
        异常时不替换旧实例，保证服务可用（宁可漏审不能崩溃）。
        多字段通过元组解包一次性替换，缩短"AC 已更新但 meta 未更新"的不一致窗口。
        """
        try:
            from apps.security.models import SensitiveWord
            # 用 iterator 流式读取，降低大词库的内存峰值
            qs = SensitiveWord.objects.filter(is_enabled=True).iterator(chunk_size=1000)

            new_ac = Ahocorasick()
            new_regexes = []
            new_meta = {}

            for sw in qs:
                meta = {'category': sw.category, 'action': sw.action}
                new_meta[sw.word] = meta
                if sw.is_regex:
                    try:
                        pattern = re.compile(sw.word)
                        new_regexes.append((pattern, sw.word, sw.category, sw.action))
                    except re.error as e:
                        logger.warning('[SensitiveFilter] invalid regex %r: %s', sw.word, e)
                else:
                    new_ac.add_word(sw.word)

            new_ac.build()
            # 元组解包一次性替换三个字段，缩短 AC 与 meta 不一致的窗口
            # （GIL 下元组解包整体执行，比三条独立赋值的窗口小得多）
            self._ac, self._regexes, self._word_meta = new_ac, new_regexes, new_meta
            self._loaded_at = time.time()
            self._version += 1
            logger.info('[SensitiveFilter] loaded %d words + %d regexes (v%d)',
                        new_ac.word_count, len(new_regexes), self._version)
        except Exception:
            logger.exception('[SensitiveFilter] load_from_db failed, keep old version')

    # ---------- 流式状态管理 ----------

    def new_state(self) -> Dict:
        """创建流式审查状态（每个问答流独占一个）

        state 结构：
            - buffer: 累积待审文本（含上次保留的窗口）
            - warn_hits: warn 动作的命中记录（用于审计）
        """
        return {'buffer': '', 'warn_hits': []}

    # ---------- 全量审查（非流式场景）----------

    def check(self, text: str) -> List[HitResult]:
        """对完整文本做全量审查，返回所有命中

        用于：缓存命中 / 任务拆分的一次性 delta / 落库后二次审查
        """
        if not text or not self._is_enabled():
            return []
        return self._scan(text)

    # ---------- 流式增量审查 ----------

    def feed(self, state: Dict, delta: str) -> Tuple[List[str], Optional[HitResult]]:
        """流式增量审查

        策略：
        1. 把 delta 追加到 state.buffer
        2. 若 buffer 不足 CHUNK_SIZE 且无分隔符：暂不下发（返回空列表）
           - 原因：关键词可能跨 delta，过早下发会漏审
        3. 若 buffer 达到阈值或含分隔符：全量审查
           - 命中 block：返回 hit，不下发任何内容（前端会清空已展示的）
           - 命中 mask：替换敏感词为 MASK_STR 后下发
           - 无命中：保留尾部 WINDOW_SIZE 字符（防跨边界），其余下发

        Args:
            state: feed 流程维护的状态（new_state 创建）
            delta: 本次新到的 LLM 输出片段

        Returns:
            (outputs, hit)
            - outputs: 可立即下发的安全文本片段列表（可能 0/1/多个）
            - hit: 命中 block 时返回 HitResult，否则 None
        """
        if not self._is_enabled():
            # 审查关闭：先把 buffer 中残余的安全内容下发（避免乱序），再透传 delta
            buf = state.get('buffer', '')
            state['buffer'] = ''
            outs = [buf] if buf else []
            if delta:
                outs.append(delta)
            return outs, None
        if not delta:
            return [], None

        state['buffer'] += delta

        # 累积不足且无分隔符：继续等
        # 但若 buffer 超过 MAX_BUFFER_SIZE 则强制送审，防止异常输入导致内存耗尽
        if (len(state['buffer']) < self.CHUNK_SIZE
                and not self._has_separator(state['buffer'])
                and len(state['buffer']) < self.MAX_BUFFER_SIZE):
            return [], None

        return self._review_buffer(state, flush=False)

    def flush(self, state: Dict) -> Tuple[List[str], Optional[HitResult]]:
        """流结束时的收尾审查

        把 buffer 中残余的文本审查并下发（不再保留窗口）。
        必须在 LLM stream 结束后调用一次，避免尾部内容丢失。
        """
        if not self._is_enabled():
            buf = state.get('buffer', '')
            state['buffer'] = ''
            return [buf] if buf else [], None

        return self._review_buffer(state, flush=True)

    # ---------- 内部实现 ----------

    def _is_enabled(self) -> bool:
        return getattr(settings, 'SENSITIVE_FILTER_ENABLED', True)

    def _has_separator(self, text: str) -> bool:
        return any(ch in self._SEPARATORS for ch in text)

    def _scan(self, text: str) -> List[HitResult]:
        """对一段文本做 AC + 正则扫描，返回全部命中"""
        hits: List[HitResult] = []

        # AC 自动机匹配普通词
        for start, end, word in self._ac.search(text):
            meta = self._word_meta.get(word, {})
            hits.append(HitResult(
                word=word,
                category=meta.get('category', 'other'),
                action=meta.get('action', 'mask'),
                start=start, end=end,
            ))

        # 正则匹配（手机号/身份证等）
        for pattern, word, category, action in self._regexes:
            for m in pattern.finditer(text):
                hits.append(HitResult(
                    word=word,
                    category=category,
                    action=action,
                    start=m.start(), end=m.end(),
                ))

        return hits

    def _review_buffer(self, state: Dict, flush: bool) -> Tuple[List[str], Optional[HitResult]]:
        """审查 buffer 并返回可下发的安全文本

        Args:
            flush: True=流结束（不保留窗口），False=保留尾部窗口
        """
        text = state['buffer']
        if not text:
            return [], None

        hits = self._scan(text)

        # 优先处理 block：立即中断，不下发任何内容
        for h in hits:
            if h.action == 'block':
                logger.warning('[SensitiveFilter] BLOCK hit: word=%r category=%s',
                               h.word, h.category)
                state['buffer'] = ''
                return [], h

        # 收集 warn 命中（不影响下发，仅记录用于审计）
        for h in hits:
            if h.action == 'warn':
                state['warn_hits'].append(h)

        # mask 命中：替换敏感词为 ***
        masked = text
        mask_hits = [h for h in hits if h.action == 'mask']
        if mask_hits:
            # 按 start 降序替换（避免索引错位）
            for h in sorted(mask_hits, key=lambda x: x.start, reverse=True):
                masked = masked[:h.start] + self.MASK_STR + masked[h.end:]
            logger.info('[SensitiveFilter] MASK %d words in %d chars',
                        len(mask_hits), len(text))

        if flush:
            # 流结束：全部下发
            state['buffer'] = ''
            return [masked], None

        # 非收尾：保留尾部 WINDOW_SIZE 字符（防跨边界关键词）
        win = self.WINDOW_SIZE
        if len(masked) <= win:
            # buffer 太短：暂不下发，等下次或 flush
            state['buffer'] = masked
            return [], None
        safe = masked[:-win]
        state['buffer'] = masked[-win:]
        return [safe], None


# ============ 便捷函数 ============

def get_sensitive_filter() -> SensitiveFilter:
    """获取敏感词过滤器单例"""
    return SensitiveFilter.get_instance()
