"""
Agent ReAct Prompt
用于 Agentic RAG 模式，LLM 可调用工具辅助回答。
与原 QA prompt 的区别：不再预先注入检索片段，而是让 LLM 自主决定是否调用
knowledge_search 工具检索知识库，或调用其他工具补齐信息。
"""

# Agent System Prompt
AGENT_SYSTEM_PROMPT = """你是「企业智能助手」，服务于公司内部员工，可以调用工具辅助回答用户问题。

你的核心原则：
1. **善用工具**——当问题涉及公司内部文档、规章制度、产品资料时，调用 knowledge_search 检索知识库；
   涉及实时信息、外部资料时调用 web_search；涉及业务数据统计时调用 text2sql。
2. **基于事实回答**——回答必须基于工具返回的真实数据或检索到的知识片段，不要编造。
3. **引用来源**——如果使用了 knowledge_search 检索到的内容，在回答末尾用 [1][2] 标注引用编号。
4. **简洁专业**——中文回答，除非用户用英文提问；避免长篇大论，直接给出答案。
5. **拒答原则**——如果工具调用后仍无法获得相关信息，诚实回答"根据现有资料，暂无法回答该问题"。
6. **工具调用节制**——一次只调用必要的工具；如果一次调用结果已足够回答，不要继续调用其他工具。
"""


def build_agent_messages(question: str, memory_block: str = '',
                         system_prompt: str = None) -> list:
    """构造 Agent 模式的初始 messages

    与 build_qa_messages 的区别：
    - 不预注入 context_block（让 LLM 自主决定是否调用 knowledge_search）
    - 保留 memory_block（历史对话记忆）
    - 使用 AGENT_SYSTEM_PROMPT（含工具使用指引）

    Args:
        question: 用户问题
        memory_block: 历史记忆文本（可空）
        system_prompt: 自定义系统提示（默认用 AGENT_SYSTEM_PROMPT）

    Returns:
        OpenAI messages 格式列表
    """
    user_content = f"""{memory_block or '（无历史记忆）'}

## 用户问题
{question}

请根据需要调用合适的工具回答用户问题。如果问题可以直接用常识回答且不涉及公司内部信息，可以直接回答。
"""
    return [
        {'role': 'system', 'content': system_prompt or AGENT_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]
