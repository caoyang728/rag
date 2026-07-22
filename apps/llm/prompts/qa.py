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


def build_context_block(chunks: list) -> str:
    """把检索命中的 chunks 拼成 Prompt 上下文
    每个 chunk 结构：{'chunk_id','content','doc_title','section_path','page_number','score'}"""
    if not chunks:
        return '（无相关知识片段）'
    lines = []
    for i, ch in enumerate(chunks, 1):
        title = ch.get('doc_title', '未知文档')
        section = ch.get('section_path') or ''
        page = ch.get('page_number')
        header = f'[{i}] 来源：《{title}》'
        if section:
            header += f' · {section}'
        if page:
            header += f' · P{page}'
        content = ch.get('content', '').strip()
        # 单片段截断，避免超预算
        if len(content) > 1200:
            content = content[:1200] + '...'
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
