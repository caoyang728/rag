"""
Chunker - 语义感知切片
按段落聚合，避免固定长度切断上下文；保留 section_path 溯源
表格双层存储：摘要用于embedding，完整数据存入extra.full_content
"""
from typing import List, Dict, Any

DEFAULT_CHUNK_SIZE = 500       # 目标切片字符数
DEFAULT_CHUNK_OVERLAP = 50     # 相邻切片重叠字数
TABLE_SUMMARY_THRESHOLD = 2000  # 超过此长度时使用双层存储


def _generate_table_summary(content: str, extra: dict) -> str:
    """生成表格摘要，用于embedding和上下文保护"""
    rows = content.strip().split('\n')
    if not rows:
        return '空表格'
    
    header_row = rows[0]
    column_names = [c.strip() for c in header_row.split('|') if c.strip()]
    total_rows = len(rows) - 1
    num_cols = len(column_names)
    
    summary_parts = []
    
    if extra.get('title'):
        summary_parts.append(f'表格标题：{extra["title"]}')
    summary_parts.append(f'表格结构：{total_rows}行 × {num_cols}列')
    
    if column_names:
        summary_parts.append(f'列名：{", ".join(column_names)}')
    
    if extra.get('rows'):
        summary_parts.append(f'数据行数：{extra["rows"]}')
    if extra.get('cols'):
        summary_parts.append(f'数据列数：{extra["cols"]}')
    
    if extra.get('caption'):
        summary_parts.append(f'表格说明：{extra["caption"]}')
    
    if total_rows > 0 and total_rows <= 10:
        preview_rows = rows[1:]
        summary_parts.append(f'\n表格数据：')
        for row in preview_rows:
            cells = [c.strip()[:30] for c in row.split('|') if c.strip()]
            summary_parts.append(f'  {" | ".join(cells)}')
    elif total_rows > 10:
        preview_rows = rows[1:6]
        summary_parts.append(f'\n表格前5行数据：')
        for row in preview_rows:
            cells = [c.strip()[:30] for c in row.split('|') if c.strip()]
            summary_parts.append(f'  {" | ".join(cells)}')
        summary_parts.append(f'  ...（共{total_rows}行）')
    
    return '\n'.join(summary_parts)


def chunk_blocks(blocks: List[Dict[str, Any]],
                 chunk_size: int = DEFAULT_CHUNK_SIZE,
                 overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Dict[str, Any]]:
    """把 parser 输出的 blocks 二次切片
    - 短的直接保留
    - 长的按 chunk_size 切分，overlap 重叠
    - 记录段落组ID，用于构建LLM上下文时合并相邻切片
    - 表格类型的block不切分，保留完整内容
    - 表格双层存储：超过阈值时，摘要存入content用于embedding，完整markdown存入extra.full_content
    """
    out: List[Dict[str, Any]] = []
    paragraph_group_id = 0
    for blk in blocks:
        content = blk.get('content', '') or ''
        chunk_type = blk.get('type', 'text')
        
        if chunk_type == 'table':
            new_blk = dict(blk)
            extra = dict(blk.get('extra') or {})
            extra['paragraph_group'] = paragraph_group_id
            
            if len(content) > TABLE_SUMMARY_THRESHOLD:
                summary = _generate_table_summary(content, extra)
                extra['full_content'] = content
                new_blk['content'] = summary
            else:
                extra['full_content'] = content
            
            new_blk['extra'] = extra
            out.append(new_blk)
            paragraph_group_id += 1
            continue
        
        if len(content) <= chunk_size * 2:
            new_blk = dict(blk)
            extra = dict(blk.get('extra') or {})
            extra['paragraph_group'] = paragraph_group_id
            new_blk['extra'] = extra
            out.append(new_blk)
            paragraph_group_id += 1
            continue
        # 长切片：按段落切
        pieces = _split_long(content, chunk_size, overlap)
        for i, p in enumerate(pieces):
            new_blk = dict(blk)
            new_blk['content'] = p
            extra = dict(blk.get('extra') or {})
            extra['piece'] = i
            extra['paragraph_group'] = paragraph_group_id
            new_blk['extra'] = extra
            out.append(new_blk)
        paragraph_group_id += 1
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
