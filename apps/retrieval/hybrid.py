"""
混合检索 - RRF (Reciprocal Rank Fusion) 融合向量检索与 BM25
- 三级混合召回架构：向量召回 30 + BM25 召回 30 -> RRF 融合 -> Rerank Top 5
- RRF 公式：score(d) = Σ 1 / (k + rank_i(d))，k=60（论文经验值）
- 权限过滤在两路 recall 中都完成，Rerank 层无需再判权
- 并发执行两路检索，减少延迟
- hybrid_search 为对外入口：总开关 QUERY_TRANSFORM_ENABLED 开启时，
  内部先做查询改写/分解（见 query_transform.search_with_transform），关闭时行为不变
"""
from loguru import logger
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

from django.conf import settings

from .vector_store import vector_search
from .bm25 import bm25_search
from .rerank import rerank_docs
from apps.llm.embedding import get_embedding_client, EmbeddingException
from apps.system.config_loader import get_config_value


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
                  do_rerank: bool = True,
                  personalize: bool = True) -> Dict[str, Any]:
    """混合检索对外入口（对外契约不变）

    - 总开关 QUERY_TRANSFORM_ENABLED 关闭（默认）时：行为与现状完全一致，
      直接走 _search_core 原链路
    - 开关开启时：检索前先做查询改写/同义词扩展，改写后置信度不足再查询分解
      （透明链路），返回结构不变，额外带 'transform' 审计信息，
      供 QaRecord.route_trace 记录改写/分解的输入输出
    - 个性化检索总开关 PERSONALIZED_RETRIEVAL_ENABLED 开启时（默认关闭）：
      对检索结果按用户画像轻量加权重排（默认影响 ≤10%），返回额外带
      'personalization' 审计信息；关闭时行为与现状完全一致。
      离线评估链路传 personalize=False，保证评估对象与用户画像无关
    """
    from .query_transform import transform_enabled, search_with_transform
    from .profile import apply_personalization

    if transform_enabled():
        result = search_with_transform(
            query, user, root_types=root_types, node_path_prefix=node_path_prefix,
            node_ids=node_ids, vector_top_k=vector_top_k, bm25_top_k=bm25_top_k,
            rrf_top_k=rrf_top_k, rerank_top_k=rerank_top_k, do_rerank=do_rerank,
        )
    else:
        result = _search_core(
            query, user, root_types=root_types, node_path_prefix=node_path_prefix,
            node_ids=node_ids, vector_top_k=vector_top_k, bm25_top_k=bm25_top_k,
            rrf_top_k=rrf_top_k, rerank_top_k=rerank_top_k, do_rerank=do_rerank,
        )
    if personalize:
        return apply_personalization(result, user, query)
    return result


def _search_core(query: str,
                 user,
                 root_types: Optional[List[str]] = None,
                 node_path_prefix: Optional[str] = None,
                 node_ids: Optional[List[int]] = None,
                 vector_top_k: int = None,
                 bm25_top_k: int = None,
                 rrf_top_k: int = 30,
                 rerank_top_k: int = None,
                 do_rerank: bool = True) -> Dict[str, Any]:
    """三级混合检索核心（原 hybrid_search 实现，供包装层与查询改写/分解复用）
    返回: {
      'chunks': [...],  # Rerank 后的最终结果
      'stats': {'vector_ms','bm25_ms','rrf_ms','rerank_ms','total_ms'},
      'raw': {'vector': [...], 'bm25': [...], 'rrf': [...]}
    }
    """
    t_total = time.time()
    # 召回阈值优先读 SystemConfig（检索参数分类，运营可后台调整），
    # 未配置或读取失败时回退 settings（.env），保证与旧部署行为一致
    vector_top_k = vector_top_k or get_config_value('VECTOR_TOP_K', default=settings.VECTOR_TOP_K, value_type='int') or settings.VECTOR_TOP_K
    bm25_top_k = bm25_top_k or get_config_value('BM25_TOP_K', default=settings.BM25_TOP_K, value_type='int') or settings.BM25_TOP_K
    rerank_top_k = rerank_top_k or settings.RETRIEVAL_RERANK_TOP_K

    # 1. 生成 query 向量
    embed_client = get_embedding_client()
    try:
        qvec = embed_client.embed_one(query)
    except EmbeddingException as e:
        logger.error(f'[Hybrid] query embedding failed: {e}')
        raise
    
    # 检测零向量
    if all(v == 0.0 for v in qvec):
        logger.error('[Hybrid] query embedding returned zero vector')
        raise EmbeddingException("embedding服务返回零向量，无法进行向量检索")

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
        # 相关性阈值过滤：rerank 分数低于阈值的片段视为与问题无关，直接丢弃，
        # 避免把无关联的文档（如法规类）当作引用返回给用户
        # 仅过滤带 rerank_score 的结果（rerank 失败回退时无分数，保持原行为不过滤）
        # 阈值优先读 SystemConfig（检索参数分类，运营可后台调整），
        # 未配置/非法值回退 settings（.env），0 表示不过滤（用 None 判断以兼容 0）
        cfg_threshold = get_config_value('RETRIEVAL_MIN_RERANK_SCORE', default=None, value_type='float')
        min_rerank_score = cfg_threshold if cfg_threshold is not None else settings.RETRIEVAL_MIN_RERANK_SCORE
        if min_rerank_score > 0:
            final = [c for c in final
                     if 'rerank_score' not in c or c['rerank_score'] >= min_rerank_score]
            if len(final) < len(rrf_res[:rerank_top_k]):
                logger.info(f'[Hybrid] rerank threshold {min_rerank_score} '
                            f'filtered {len(rrf_res[:rerank_top_k]) - len(final)} chunks')
    else:
        stats['rerank_ms'] = 0

    # 5. 丰富 chunk 元信息（doc_title / section_path / page_number）
    _enrich_chunks(final)

    stats['total_ms'] = int((time.time() - t_total) * 1000)
    logger.info(f'[Hybrid] vec={len(vec_res)} bm25={len(bm_res)} rrf={len(rrf_res)} final={len(final)} stats={stats}')

    return {
        'chunks': final,
        'stats': stats,
        'raw': {'vector': vec_res, 'bm25': bm_res, 'rrf': rrf_res},
    }


def _enrich_chunks(chunks: List[Dict[str, Any]]) -> None:
    """就地扩充 doc_title / section_path / page_number / extra / image_data"""
    if not chunks:
        return
    from apps.knowledge.models import DocumentChunk, Document, ImageResource
    chunk_ids = [c['chunk_id'] for c in chunks]
    doc_ids = list({c['document_id'] for c in chunks})

    doc_titles = dict(Document.objects.filter(id__in=doc_ids).values_list('id', 'title'))
    chunk_meta = {
        c.id: c for c in DocumentChunk.objects.filter(id__in=chunk_ids)
        .only('id', 'section_path', 'page_number', 'content', 'extra', 'image_id', 'chunk_type')
    }
    
    image_ids = [m.image_id for m in chunk_meta.values() if m.image_id]
    image_data = {}
    if image_ids:
        for img in ImageResource.objects.filter(id__in=image_ids):
            image_data[img.id] = {
                'base64_data': img.base64_data,
                'width': img.width,
                'height': img.height,
                'mime_type': img.mime_type,
            }
    
    for c in chunks:
        c['doc_title'] = doc_titles.get(c['document_id'], '未知文档')
        m = chunk_meta.get(c['chunk_id'])
        if m:
            c['section_path'] = m.section_path
            c['page_number'] = m.page_number
            c['content'] = m.content  # 用完整内容替换 preview
            c['extra'] = m.extra  # 传递段落组信息
            c['chunk_type'] = m.chunk_type  # 传递类型信息
            if m.image_id and m.image_id in image_data:
                c['extra']['base64_data'] = image_data[m.image_id]['base64_data']
                c['extra']['width'] = image_data[m.image_id]['width']
                c['extra']['height'] = image_data[m.image_id]['height']
                c['extra']['mime_type'] = image_data[m.image_id]['mime_type']
