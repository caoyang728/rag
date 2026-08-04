"""
BM25 关键词检索
- 使用 rank_bm25 + jieba 中文分词
- keyword_weight 表动态加权
- 从 DocumentVector.keywords 字段做候选池筛选，避免全表扫描
"""
from loguru import logger
import time
from typing import List, Dict, Any, Optional

import jieba
from rank_bm25 import BM25Okapi
from django.db.models import Q

from apps.retrieval.models import DocumentVector
from apps.retrieval.permission import build_permission_q
from apps.analytics.models import KeywordWeight



def tokenize(text: str) -> List[str]:
    """jieba 分词，过滤停用词/短词"""
    if not text:
        return []
    tokens = jieba.lcut(text, cut_all=False)
    return [t for t in tokens if len(t.strip()) >= 2]


def bm25_search(query: str,
                user,
                top_k: int = 30,
                root_types: Optional[List[str]] = None,
                node_ids: Optional[List[int]] = None,
                candidate_pool: int = 2000) -> List[Dict[str, Any]]:
    """BM25 检索
    - 先按权限 + keywords GIN 命中拉候选池
    - Python 端 BM25 打分
    - 支持 keyword_weight 加权
    """
    t0 = time.time()
    tokens = tokenize(query)
    if not tokens:
        return []

    perm_q = build_permission_q(user, root_types=root_types, node_ids=node_ids)
    # 候选池：keywords overlap 或 content_preview 触发 pg_trgm
    kw_q = Q(keywords__overlap=tokens)
    for tok in tokens[:5]:
        kw_q = kw_q | Q(content_preview__icontains=tok)
    # visibility_level（TEAM_ONLY/DEPT_ONLY/PUBLIC）
    qs = (DocumentVector.objects.filter(perm_q).filter(kw_q)
          .values('id', 'chunk_id', 'document_id', 'node_id',
                  'visibility_level', 'root_type', 'node_path',
                  'content_preview', 'chunk_type', 'keywords')[:candidate_pool])
    candidates = list(qs)
    if not candidates:
        logger.info('[BM25] no candidate')
        return []

    corpus_tokens = [tokenize(c['content_preview']) or c.get('keywords') or [' '] for c in candidates]
    bm25 = BM25Okapi(corpus_tokens)
    scores = bm25.get_scores(tokens)

    # keyword_weight 加权
    kw_weight_map = {kw.keyword: kw.weight_score
                     for kw in KeywordWeight.objects.filter(keyword__in=tokens)}
    kw_bonus = 1.0
    if kw_weight_map:
        kw_bonus = sum(kw_weight_map.values()) / len(kw_weight_map)

    scored = []
    for i, c in enumerate(candidates):
        s = float(scores[i]) * kw_bonus
        scored.append({
            'vector_id': c['id'], 'chunk_id': c['chunk_id'],
            'document_id': c['document_id'], 'node_id': c['node_id'],
            'content': c['content_preview'], 'chunk_type': c['chunk_type'],
            'visibility_level': c['visibility_level'], 'root_type': c['root_type'],
            'node_path': c['node_path'], 'score': s,
        })
    scored.sort(key=lambda x: x['score'], reverse=True)
    result = scored[:top_k]
    # 归一化 [0,1]
    if result:
        max_s = max(x['score'] for x in result) or 1.0
        for x in result:
            x['score'] = x['score'] / max_s if max_s else 0.0
    logger.info(f'[BM25] hit={len(result)} latency={int((time.time() - t0) * 1000)}ms')
    return result
