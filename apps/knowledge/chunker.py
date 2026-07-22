"""
Chunker - 语义感知切片
按段落聚合，避免固定长度切断上下文；保留 section_path 溯源
"""
from typing import List, Dict, Any

DEFAULT_CHUNK_SIZE = 500       # 目标切片字符数
DEFAULT_CHUNK_OVERLAP = 50     # 相邻切片重叠字数


def chunk_blocks(blocks: List[Dict[str, Any]],
                 chunk_size: int = DEFAULT_CHUNK_SIZE,
                 overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Dict[str, Any]]:
    """把 parser 输出的 blocks 二次切片
    - 短的直接保留
    - 长的按 chunk_size 切分，overlap 重叠
    """
    out: List[Dict[str, Any]] = []
    for blk in blocks:
        content = blk.get('content', '') or ''
        if len(content) <= chunk_size * 2:
            out.append(blk)
            continue
        # 长切片：按段落切
        pieces = _split_long(content, chunk_size, overlap)
        for i, p in enumerate(pieces):
            new_blk = dict(blk)
            new_blk['content'] = p
            extra = dict(blk.get('extra') or {})
            extra['piece'] = i
            new_blk['extra'] = extra
            out.append(new_blk)
    return out


def _split_long(text: str, chunk_size: int, overlap: int) -> List[str]:
    """先按 \\n\\n 段落切，再按 chunk_size 聚合"""
    paras = [p for p in text.split('\n') if p.strip()]
    chunks: List[str] = []
    buf: List[str] = []
    cur_len = 0
    for p in paras:
        p_len = len(p)
        if cur_len + p_len <= chunk_size or not buf:
            buf.append(p)
            cur_len += p_len + 1
        else:
            chunks.append('\n'.join(buf))
            # 保留 overlap
            tail = '\n'.join(buf)[-overlap:]
            buf = [tail, p] if tail else [p]
            cur_len = len(tail) + p_len + 1
    if buf:
        chunks.append('\n'.join(buf))
    return chunks
