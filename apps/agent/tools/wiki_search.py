"""
Agent 工具：Wiki 知识页搜索
- WikiSearchTool: 在企业 Wiki 知识库中检索已整理好的知识页面
"""
from typing import Any, Dict

from .base import BaseTool, ToolContext

# Wiki 命中阈值：与三层路由的 WIKI_DIRECT_HIT_THRESHOLD(0.68) 保持一致，
# 只有足够相关的页面才作为"已整理的 Wiki 知识"返回给 Agent。
WIKI_HIT_THRESHOLD = 0.68


class WikiSearchTool(BaseTool):
    """Wiki 知识页搜索工具

    适用于问题涉及已定义的概念、流程、规范等场景（如"XX 是什么/怎么做"）。
    LLM 可据此直接获取结构化整理过的知识页面，无需再从原始文档切片拼凑。
    """

    name = 'wiki_search'
    description = ('在企业 Wiki 知识库中搜索已整理好的知识页面。'
                   '当问题涉及某个已定义的概念、流程、规范时调用，'
                   '返回结构化的 Wiki 页面内容。')
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '搜索查询语句'},
        },
        'required': ['query'],
    }

    def execute(self, ctx: ToolContext, query: str, **kwargs) -> Dict[str, Any]:
        """执行 Wiki 搜索

        Args:
            ctx: 工具执行上下文
            query: 用户搜索语句

        Returns:
            {'result', 'ok', 'meta'}：命中时返回页面内容，未命中时返回提示
        """
        from apps.wiki.retriever import search_wiki

        results = search_wiki(query, top_k=1, threshold=WIKI_HIT_THRESHOLD)

        if not results:
            return {
                'result': '未找到相关的 Wiki 知识页面。',
                'ok': True,
                'meta': {'wiki_pages': [], 'hit': False},
            }

        page = results[0]
        content = f"# {page['title']}\n\n{page['summary']}\n\n---\n\n{page['content'][:2000]}"

        return {
            'result': content,
            'ok': True,
            'meta': {
                'wiki_pages': [page],
                'hit': True,
                'wiki_id': page['wiki_id'],
            },
        }
