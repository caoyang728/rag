"""
System / QA Prompt
Prompt 单独文件、显式引用规范、拒答策略
"""

# 主答问 System Prompt
SYSTEM_PROMPT = """你是「企业私有化知识库智能助手」，服务于公司内部员工。
你的核心原则：
1. **基于给定的知识片段回答**——不要编造知识库外的内容；如无相关片段就诚实回答"根据现有资料，暂无法回答该问题"。
2. **引用来源**——在回答末尾用 [1][2] 的方式标注引用的知识片段编号。
3. **中文回答，简洁专业**——除非用户明确用英文提问，否则使用中文；避免长篇大论。
4. **区分事实与推理**——事实性内容标注来源，推理性内容明确写出"根据文档 X 推测"。
5. **拒绝越权**——不透露给定知识片段之外的内部信息；不回答敏感/政治/隐私类问题。
"""


# QA 主答问 Prompt 模板
QA_USER_TEMPLATE = """{memory_block}

## 检索到的相关知识片段
{context_block}

## 用户当前问题
{question}

## 请回答
请严格基于上述知识片段回答用户问题；如果片段与问题无关或不足以回答，请诚实说明，禁止编造。
用 [编号] 标注引用的知识片段。回答后单独一行列出「参考来源」。
"""


def _merge_chunks_by_group(chunks: list) -> list:
    """按文档和段落组合并相邻切片，恢复完整段落"""
    if not chunks:
        return []
    
    grouped = {}
    for ch in chunks:
        doc_id = ch.get('document_id')
        group_id = ch.get('extra', {}).get('paragraph_group', -1)
        key = (doc_id, group_id)
        if key not in grouped:
            grouped[key] = {**ch, '_contents': [ch.get('content', '')]}
        else:
            grouped[key]['_contents'].append(ch.get('content', ''))
    
    merged = []
    for key, ch in grouped.items():
        contents = ch.pop('_contents')
        if len(contents) == 1:
            ch['content'] = contents[0]
        else:
            merged_content = contents[0]
            for i in range(1, len(contents)):
                prev = contents[i-1]
                curr = contents[i]
                overlap_len = min(50, len(prev), len(curr))
                if prev[-overlap_len:] == curr[:overlap_len]:
                    merged_content += curr[overlap_len:]
                else:
                    merged_content += '\n' + curr
            ch['content'] = merged_content
        merged.append(ch)
    
    return merged


MAX_TABLE_CONTEXT_LENGTH = 2000
MAX_TABLE_PREVIEW_ROWS = 10


def _generate_table_summary(content: str, extra: dict) -> str:
    """生成表格摘要，用于大表格的上下文保护"""
    rows = content.strip().split('\n')
    
    header_row = rows[0] if rows else ''
    column_names = [c.strip() for c in header_row.split('|') if c.strip()]
    total_rows = len(rows) - 1
    num_cols = len(column_names)
    
    summary_lines = []
    summary_lines.append(f'表格摘要：共 {total_rows} 行 × {num_cols} 列')
    
    if column_names:
        summary_lines.append(f'列名：{", ".join(column_names[:5])}' + ('...' if len(column_names) > 5 else ''))
    
    if extra.get('rows'):
        summary_lines.append(f'数据行数：{extra["rows"]}')
    if extra.get('cols'):
        summary_lines.append(f'数据列数：{extra["cols"]}')
    
    if total_rows > 0:
        preview_rows = rows[1:min(MAX_TABLE_PREVIEW_ROWS + 1, len(rows))]
        summary_lines.append(f'\n前 {len(preview_rows)} 行数据：')
        for row in preview_rows:
            cells = [c.strip()[:20] for c in row.split('|') if c.strip()]
            summary_lines.append(f'  {" | ".join(cells)}')
    
    if total_rows > MAX_TABLE_PREVIEW_ROWS:
        summary_lines.append(f'\n（表格共 {total_rows} 行，仅展示前 {MAX_TABLE_PREVIEW_ROWS} 行，如需完整数据请查阅原文）')
    
    return '\n'.join(summary_lines)


def build_context_block(chunks: list) -> str:
    """把检索命中的 chunks 拼成 Prompt 上下文
    每个 chunk 结构：{'chunk_id','content','doc_title','section_path','page_number','score','chunk_type'}
    支持父子切片策略：按段落组合并相邻切片，恢复完整段落
    支持表格和图片类型的chunk展示
    表格上下文保护：超过阈值时自动降级为摘要模式"""
    if not chunks:
        return '（无相关知识片段）'
    
    merged_chunks = _merge_chunks_by_group(chunks)
    
    lines = []
    for i, ch in enumerate(merged_chunks, 1):
        title = ch.get('doc_title', '未知文档')
        section = ch.get('section_path') or ''
        page = ch.get('page_number')
        chunk_type = ch.get('chunk_type', 'text')
        
        type_label = ''
        if chunk_type == 'table':
            type_label = '【表格】'
        elif chunk_type == 'image':
            type_label = '【图片】'
        
        header = f'[{i}] {type_label}来源：《{title}》'
        if section:
            header += f' · {section}'
        if page:
            header += f' · P{page}'
        
        content = ch.get('content', '').strip()
        if chunk_type == 'table':
            extra = ch.get('extra', {})
            full_content = extra.get('full_content', '')
            
            if full_content and len(full_content) <= MAX_TABLE_CONTEXT_LENGTH:
                content = full_content
            elif full_content and len(full_content) > MAX_TABLE_CONTEXT_LENGTH:
                content = _generate_table_summary(full_content, extra)
            elif len(content) > MAX_TABLE_CONTEXT_LENGTH:
                content = _generate_table_summary(content, extra)
        elif chunk_type == 'image':
            extra = ch.get('extra', {})
            if extra.get('base64_data'):
                content = f'图片数据已提取（{extra.get("width", 0)}×{extra.get("height", 0)}像素）'
            else:
                content = '图片数据未提取'
        
        lines.append(f'{header}\n{content}')
    return '\n\n'.join(lines)


def build_qa_messages(question: str, chunks: list, memory_block: str = '',
                      system_prompt: str = None) -> list:
    """构造完整 messages"""
    ctx = build_context_block(chunks)
    user_content = QA_USER_TEMPLATE.format(
        memory_block=memory_block or '（无历史记忆）',
        context_block=ctx,
        question=question,
    )
    return [
        {'role': 'system', 'content': system_prompt or SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]
