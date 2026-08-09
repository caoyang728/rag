"""
个性化检索 - 用户画像特征提取 + 排序轻量加权

- 画像来源：UserMemory（domain_tags / frequent_topics）+ QaRecord 近 30 天聚合
  （jieba 高频领域词 + 偏好文档类型 root_type）
- 排序加权：把基础排序分数（rerank_score / rrf_score / score）归一化到 [0,1] 后，
  与画像相关度按 PERSONALIZED_WEIGHT（默认 0.1，即影响 ≤10%）做线性融合后重排，
  加权必须克制：系数钳制上限 0.2，且基础分数至少占 80%，避免画像污染全局结果
- 开关（SystemConfig，风险 normal）：
  - PERSONALIZED_RETRIEVAL_ENABLED: 总开关，关闭时 hybrid_search 行为与现状完全一致
  - PERSONALIZED_WEIGHT: 加权系数（0-0.2，默认 0.1）
- 冷启动：无 UserMemory 且近 30 天无问答视为无画像，不参与加权，无副作用
- 审计：每次走个性化链路都输出 personalization 审计 dict，并入
  QaRecord.route_trace（layer=personalization），供评估看板统计"个性化命中率"（对比开/关）
"""
import time
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from typing import Dict, List, Optional, Any

from django.core.cache import cache
from django.utils import timezone
from loguru import logger

from apps.system.config_loader import get_config_value


# 画像缓存 TTL：10 分钟，平衡画像新鲜度与 QaRecord 聚合开销
_PROFILE_CACHE_TTL = 600
# 加权系数上限：默认 0.1（影响 ≤10%），钳制到 0.2 防止画像污染全局检索结果
_WEIGHT_MAX = 0.2
# 画像聚合窗口：近 30 天问答
_PROFILE_DAYS = 30
# 偏好文档类型最低出现次数：少于该次数视为偶发，不算稳定偏好
_PREFERRED_ROOT_TYPE_MIN_HITS = 3


@dataclass
class UserProfile:
    """用户画像（用于检索排序轻量加权）

    terms: 领域词（UserMemory.domain_tags/frequent_topics + QaRecord 高频词）
    preferred_root_types: 偏好文档类型（近 30 天问答 root_type 计数排序，≥3 次）
    """

    terms: List[str] = field(default_factory=list)
    preferred_root_types: List[str] = field(default_factory=list)

    @property
    def has_profile(self) -> bool:
        """冷启动判定：无领域词且无偏好文档类型视为无画像"""
        return bool(self.terms or self.preferred_root_types)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserProfile':
        return cls(
            terms=list(data.get('terms') or []),
            preferred_root_types=list(data.get('preferred_root_types') or []),
        )


# ---------------------------------------------------------------------------
# 配置读取（SystemConfig，风险 normal）
# ---------------------------------------------------------------------------

def personalization_enabled() -> bool:
    """个性化检索总开关，默认关闭

    关闭时 hybrid_search 行为与现状完全一致（不进入画像提取与加权链路）。
    """
    try:
        return bool(get_config_value('PERSONALIZED_RETRIEVAL_ENABLED',
                                     default=False, value_type='bool'))
    except Exception:
        return False


def personalization_weight() -> float:
    """个性化加权系数，默认 0.1（影响 ≤10%），钳制 0~0.2

    数值越大个性化对排序影响越明显，钳制上限防止画像污染全局检索结果。
    """
    try:
        val = get_config_value('PERSONALIZED_WEIGHT', default=0.1, value_type='float')
        return max(0.0, min(float(val), _WEIGHT_MAX))
    except (TypeError, ValueError):
        return 0.1


# ---------------------------------------------------------------------------
# 用户画像提取
# ---------------------------------------------------------------------------

def get_user_profile(user, force: bool = False) -> Optional[UserProfile]:
    """读取用户画像（Redis 缓存 10 分钟，按 user_id 隔离）

    - 无画像（冷启动）不缓存空结果，每次重新判定：
      用户刚产生问答后能尽快生效画像，避免长期命中空缓存
    - 任何异常（DB/Redis 抖动）降级为无画像，绝不阻断主检索流程
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    cache_key = f'user_profile:{user.id}'
    if not force:
        try:
            cached = cache.get(cache_key)
            if cached:
                return UserProfile.from_dict(cached)
        except Exception as e:
            logger.warning(f'[Profile] 读画像缓存失败 user={user.id}: {e}')
    try:
        profile = build_user_profile(user)
    except Exception as e:
        logger.error(f'[Profile] 画像构建失败 user={user.id}: {e}')
        return None
    if profile is None:
        return None
    try:
        cache.set(cache_key, profile.to_dict(), _PROFILE_CACHE_TTL)
    except Exception as e:
        logger.warning(f'[Profile] 写画像缓存失败 user={user.id}: {e}')
    return profile


def build_user_profile(user) -> Optional[UserProfile]:
    """从 UserMemory / QaRecord 聚合用户画像

    1. UserMemory.domain_tags + frequent_topics 直接作为画像领域词
    2. 近 30 天 QaRecord 按 root_type 聚合，出现 ≥3 次的类型记为偏好文档类型
    3. 近 30 天问题文本 jieba 提取高频词，补充画像领域词
    4. 领域词与偏好类型皆空视为冷启动，返回 None（不参与个性化加权）

    画像由 get_user_profile 缓存（10 分钟），避免每次检索都触发聚合。
    """
    from apps.memory.models import UserMemory
    from apps.chat.models import QaRecord

    terms: List[str] = []

    # 1. 用户长期记忆：领域标签 + 高频主题
    um = UserMemory.objects.filter(user=user).first()
    if um:
        terms.extend(um.domain_tags or [])
        terms.extend(um.frequent_topics or [])

    # 2/3. 近 30 天问答聚合：偏好文档类型 + jieba 高频领域词
    since = timezone.now() - timedelta(days=_PROFILE_DAYS)
    qas = QaRecord.objects.filter(user=user, created_at__gte=since)
    root_counts: Dict[str, int] = {}
    questions: List[str] = []
    for r in qas.values('question', 'root_type').iterator():
        rt = r.get('root_type') or ''
        if rt:
            root_counts[rt] = root_counts.get(rt, 0) + 1
        q = (r.get('question') or '').strip()
        if q:
            questions.append(q)

    # 偏好文档类型：按提问次数降序取前 5，且至少出现 3 次才算稳定偏好
    preferred = sorted(root_counts.items(), key=lambda x: (-x[1], x[0]))[:5]
    preferred_root_types = [rt for rt, cnt in preferred if cnt >= _PREFERRED_ROOT_TYPE_MIN_HITS]

    # jieba 高频词（仅取部分问题，控制单次聚合开销）
    if questions:
        text = ' '.join(questions[:200])
        try:
            import jieba.analyse
            terms.extend(jieba.analyse.extract_tags(text, topK=15))
        except Exception as e:
            logger.warning(f'[Profile] jieba 提取高频词失败: {e}')

    terms = _dedupe(terms, limit=20)

    if not terms and not preferred_root_types:
        return None  # 冷启动：无画像
    return UserProfile(terms=terms, preferred_root_types=preferred_root_types)


def _dedupe(items: List[str], limit: int) -> List[str]:
    """保序去重并截断，去掉空串"""
    seen = set()
    result: List[str] = []
    for it in items:
        s = (it or '').strip()
        if not s or s in seen:
            continue
        seen.add(s)
        result.append(s)
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------------
# 排序加权
# ---------------------------------------------------------------------------

def _chunk_personal_similarity(chunk: Dict[str, Any], profile: UserProfile) -> float:
    """chunk 与用户画像的相关度（0~1）

    - 文档类型偏好：chunk.root_type 命中偏好类型 +0.5
    - 领域词命中：chunk 内容命中画像领域词，按命中数累加（单个词 0.15，上限 0.5）
    两项合计封顶 1.0；纯类型偏好无法区分同库结果，由领域词提供主要区分度。
    """
    score = 0.0
    if chunk.get('root_type') in profile.preferred_root_types:
        score += 0.5
    text = (chunk.get('content') or '')[:500]
    if text and profile.terms:
        hits = sum(1 for t in profile.terms if t and t in text)
        score += min(0.5, hits * 0.15)
    return min(1.0, score)


def _base_score(chunk: Dict[str, Any]) -> float:
    """取 chunk 的基础排序分数：rerank_score > rrf_score > score，缺省 0"""
    for key in ('rerank_score', 'rrf_score', 'score'):
        v = chunk.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def apply_personalization(result: Dict[str, Any], user, query: str) -> Dict[str, Any]:
    """对 hybrid_search 结果应用个性化排序加权（对外契约不变）

    流程：
    1. 总开关关闭 → 原样返回（与现状完全一致）
    2. 用户无画像（冷启动）/ 无召回 → 原样返回，仅附加 applied=False 审计信息
    3. 有画像 → 基础分数归一化后与画像相关度按 weight 线性融合，重排 chunks，
       并附加完整审计 dict（供 QaRecord.route_trace 记录与看板统计）

    审计信息挂到 result['transform']['personalization'] 下，
    使现有 build_route_trace 链路能一并审计，无需改动调用方。
    """
    if not personalization_enabled():
        return result

    profile = get_user_profile(user)
    if profile is None or not profile.has_profile:
        pz = {'enabled': True, 'applied': False, 'cold_start': True, 'latency_ms': 0}
        result['personalization'] = pz
        _merge_into_transform(result, pz)
        return result

    chunks = result.get('chunks') or []
    if not chunks:
        pz = {'enabled': True, 'applied': False, 'cold_start': False,
              'reason': 'no_chunks', 'latency_ms': 0}
        result['personalization'] = pz
        _merge_into_transform(result, pz)
        return result

    t0 = time.time()
    weight = personalization_weight()

    original_order = [c.get('chunk_id') for c in chunks]
    base_scores = [_base_score(c) for c in chunks]
    span = (max(base_scores) - min(base_scores)) if base_scores else 0.0
    base_min = min(base_scores) if base_scores else 0.0

    sims = [_chunk_personal_similarity(c, profile) for c in chunks]

    # 归一化基础分数 → [0,1]，再与画像相关度按 weight 线性融合
    # 基础分数至少占 (1-weight)（默认 90%），保证个性化只做轻量微调
    weighted: List[tuple] = []
    for chunk, base, sim in zip(chunks, base_scores, sims):
        norm = (base - base_min) / span if span > 0 else 0.5
        final_score = norm * (1 - weight) + sim * weight
        weighted.append((final_score, sim, chunk))
    weighted.sort(key=lambda x: x[0], reverse=True)

    new_chunks = [item[2] for item in weighted]
    adjusted_count = sum(
        1 for nc, oc in zip(new_chunks, chunks)
        if nc.get('chunk_id') != oc.get('chunk_id')
    )
    reordered = adjusted_count > 0
    top_sim = weighted[0][1] if weighted else 0.0
    personalized_hits = sum(1 for _, sim, _ in weighted if sim > 0.0)

    result['chunks'] = new_chunks
    pz = {
        'enabled': True,
        'weight': weight,
        'applied': True,
        'cold_start': False,
        'profile': {
            'terms': profile.terms[:10],
            'preferred_root_types': profile.preferred_root_types[:5],
        },
        'adjusted_count': adjusted_count,
        'reordered': reordered,
        'top_personalized': top_sim > 0.0,
        'personalized_hits': personalized_hits,
        'latency_ms': int((time.time() - t0) * 1000),
    }
    result['personalization'] = pz
    _merge_into_transform(result, pz)
    logger.info(
        f'[Personalization] user={getattr(user, "id", None)} query={query!r} '
        f'weight={weight} adjusted={adjusted_count} reordered={reordered} '
        f'personalized_hits={personalized_hits} latency={pz["latency_ms"]}ms'
    )
    return result


def _merge_into_transform(result: Dict[str, Any], pz: Dict[str, Any]) -> None:
    """把个性化审计并入 result['transform']，使 build_route_trace 能统一审计

    - 查询改写/分解关闭时不存在 transform，这里补一个 enabled=False 的占位，
      只携带 personalization（build_route_trace 会跳过 enabled=False 的改写块）
    - 改写链路开启时直接挂到已有 transform 下
    """
    transform = result.setdefault('transform', {'enabled': False})
    transform['personalization'] = pz


# ---------------------------------------------------------------------------
# 审计：route_trace
# ---------------------------------------------------------------------------

def build_personalization_route_trace(pz: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把个性化检索审计信息转换为 QaRecord.route_trace 审计条目

    结构与 query_transform.build_route_trace 保持一致：list of {'layer', ...}，
    供评估看板统计"个性化命中率"。开关关闭（无 pz）时返回空列表。
    """
    if not pz or not pz.get('enabled'):
        return []
    profile = pz.get('profile') or {}
    return [{
        'layer': 'personalization',
        'enabled': True,
        'applied': bool(pz.get('applied', False)),
        'cold_start': bool(pz.get('cold_start', False)),
        'weight': float(pz.get('weight', 0.1)),
        'adjusted_count': int(pz.get('adjusted_count', 0)),
        'reordered': bool(pz.get('reordered', False)),
        'top_personalized': bool(pz.get('top_personalized', False)),
        'personalized_hits': int(pz.get('personalized_hits', 0)),
        'profile_domains': (profile.get('terms') or [])[:10],
        'preferred_root_types': (profile.get('preferred_root_types') or [])[:5],
        'latency_ms': int(pz.get('latency_ms', 0)),
    }]
