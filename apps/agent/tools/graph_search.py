"""
Agent 工具：知识图谱搜索
- GraphSearchTool: 在企业知识图谱中检索实体关系（local/global/auto）
"""
from typing import Any, Dict

from .base import BaseTool, ToolContext


class GraphSearchTool(BaseTool):
    """知识图谱搜索工具

    适用于涉及人物关联、组织结构、项目归属、跨部门关系等需要多跳推理的问题。
    mode 说明：
    - local: 局部实体关系检索（命中实体后做多跳关系扩展）
    - global: 全局知识领域检索（返回社区摘要）
    - auto: 先 local 后 global，按置信度择优（默认）
    """

    name = 'graph_search'
    description = ('在企业知识图谱中搜索实体关系。'
                   '当问题涉及人物关联、组织结构、项目归属、'
                   '跨部门关系等需要多跳推理时调用。')
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '搜索查询语句'},
            'mode': {
                'type': 'string',
                'enum': ['auto', 'local', 'global'],
                'default': 'auto',
                'description': 'local=局部实体关系检索, global=全局知识领域检索, auto=自动选择',
            },
        },
        'required': ['query'],
    }

    def execute(self, ctx: ToolContext, query: str, mode: str = 'auto',
                **kwargs) -> Dict[str, Any]:
        """执行图谱检索

        Args:
            ctx: 工具执行上下文（user 用于权限过滤预留）
            query: 用户搜索语句
            mode: 检索模式（auto/local/global）

        Returns:
            {'result', 'ok', 'meta'}：命中时返回实体关系上下文，未命中时返回提示
        """
        from apps.graph.retriever import graphrag_search

        result = graphrag_search(query, ctx.user, mode=mode)

        if not result.get('context'):
            return {
                'result': '未在知识图谱中找到相关信息。',
                'ok': True,
                'meta': {'entities': [], 'relations': [], 'hit': False},
            }

        return {
            'result': result['context'],
            'ok': True,
            'meta': {
                'entities': result.get('entities', []),
                'relations': result.get('relations', []),
                'communities': result.get('communities', []),
                'hit': True,
                'graph_source': result.get('source', ''),
            },
        }
