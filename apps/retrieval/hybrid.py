"""
混合检索 - RRF (Reciprocal Rank Fusion) 融合向量检索与 BM25
- 三级混合召回架构：向量召回 30 + BM25 召回 30 -> RRF 融合 -> Rerank Top 5
- RRF 公式：score(d) = Σ 1 / (k + rank_i(d))，k=60（论文经验值）
- 权限过滤在两路 recall 中都完成，Rerank 层无需再判权
- 并发执行两路检索，减少延迟
"""
from loguru import logger
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

from django.conf import settings

from .vector_store import vector_search
from .bm25 import bm25_search
from .rerank import rerank_docs
from apps.llm.embedding import get_embedding_client


RRF_K = 60

_thread_pool = ThreadPoolExecutor(max_workers=2)


def rrf_fuse(*result_lists: List[List[Dict[str, Any]]], k: int = RRF_K,
             top_k: int = 30) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion
    输入多个已排序的召回列表，融合后按 rrf_score 降序返回"""
    fused: Dict[int, Dict[str, Any]] = {}
    for results in result_lists:
        for rank, item in enumerate(results, 1):
            cid = item['chunk_id']
            if cid not in fused:
                fused[cid] = {**item, 'rrf_score': 0.0, 'from': []}
            fused[cid]['rrf_score'] += 1.0 / (k + rank)
    ranked = sorted(fused.values(), key=lambda x: x['rrf_score'], reverse=True)
    return ranked[:top_k]


def hybrid_search(query: str,
                  user,
                  root_types: Optional[List[str]] = None,
                  node_path_prefix: Optional[str] = None,
                  node_ids: Optional[List[int]] = None,
                  vector_top_k: int = None,
                  bm25_top_k: int = None,
                  rrf_top_k: int = 30,
                  rerank_top_k: int = None,
                  do_rerank: bool = True) -> Dict[str, Any]:
    """三级混合检索
    返回: {
      'chunks': [...],  # Rerank 后的最终结果
      'stats': {'vector_ms','bm25_ms','rrf_ms','rerank_ms','total_ms'},
      'raw': {'vector': [...], 'bm25': [...], 'rrf': [...]}
    }
    """
    t_total = time.time()
    vector_top_k = vector_top_k or settings.VECTOR_TOP_K
    bm25_top_k = bm25_top_k or settings.BM25_TOP_K
    rerank_top_k = rerank_top_k or settings.RETRIEVAL_RERANK_TOP_K

    # 1. 生成 query 向量
    embed_client = get_embedding_client()
    qvec = embed_client.embed_one(query)

    stats = {}

    # 2. 并行两路召回
    def _vec():
        t = time.time()
        r = vector_search(qvec, user, top_k=vector_top_k,
                          root_types=root_types, node_path_prefix=node_path_prefix, node_ids=node_ids)
        stats['vector_ms'] = int((time.time() - t) * 1000)
        return r

    def _bm():
        t = time.time()
        r = bm25_search(query, user, top_k=bm25_top_k, root_types=root_types, node_ids=node_ids)
        stats['bm25_ms'] = int((time.time() - t) * 1000)
        return r

    f_vec = _thread_pool.submit(_vec)
    f_bm = _thread_pool.submit(_bm)
    vec_res = f_vec.result()
    bm_res = f_bm.result()

    # 3. RRF 融合
    t3 = time.time()
    rrf_res = rrf_fuse(vec_res, bm_res, k=RRF_K, top_k=rrf_top_k)
    stats['rrf_ms'] = int((time.time() - t3) * 1000)

    # 4. Rerank
    final = rrf_res[:rerank_top_k]
    if do_rerank and rrf_res:
        t4 = time.time()
        final = rerank_docs(query, rrf_res, top_k=rerank_top_k)
        stats['rerank_ms'] = int((time.time() - t4) * 1000)
    else:
        stats['rerank_ms'] = 0

    # 5. 丰富 chunk 元信息（doc_title / section_path / page_number）
    _enrich_chunks(final)

    stats['total_ms'] = int((time.time() - t_total) * 1000)
    logger.info('[Hybrid] vec=%d bm25=%d rrf=%d final=%d stats=%s',
                len(vec_res), len(bm_res), len(rrf_res), len(final), stats)

    return {
        'chunks': final,
        'stats': stats,
        'raw': {'vector': vec_res, 'bm25': bm_res, 'rrf': rrf_res},
    }


def _enrich_chunks(chunks: List[Dict[str, Any]]) -> None:
    """就地扩充 doc_title / section_path / page_number"""
    if not chunks:
        return
    from apps.knowledge.models import DocumentChunk, Document
    chunk_ids = [c['chunk_id'] for c in chunks]
    doc_ids = list({c['document_id'] for c in chunks})

    doc_titles = dict(Document.objects.filter(id__in=doc_ids).values_list('id', 'title'))
    chunk_meta = {
        c.id: c for c in DocumentChunk.objects.filter(id__in=chunk_ids)
        .only('id', 'section_path', 'page_number', 'content')
    }
    for c in chunks:
        c['doc_title'] = doc_titles.get(c['document_id'], '未知文档')
        m = chunk_meta.get(c['chunk_id'])
        if m:
            c['section_path'] = m.section_path
            c['page_number'] = m.page_number
            c['content'] = m.content  # 用完整内容替换 preview
