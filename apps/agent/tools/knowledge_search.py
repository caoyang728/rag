"""
knowledge_search 工具 - 内部知识库检索
复用现有 hybrid_search（向量 + BM25 + RRF + Rerank）+ 二次权限过滤，
供 Agent 在 ReAct 循环中按需检索企业知识库。
"""
import json
from typing import Any, Dict

from loguru import logger

from .base import BaseTool, ToolContext


class KnowledgeSearchTool(BaseTool):
    """内部知识库检索工具

    供 Agent 在 ReAct 循环中调用：当用户问题需要查询企业内部文档时，
    LLM 会调用本工具，传入检索 query，工具返回相关文档片段。

    复用 hybrid_search 全链路（向量+BM25+RRF+Rerank），并执行与 executor.py
    一致的二次权限过滤，确保 Agent 不会泄露用户无权访问的文档内容。
    """

    name = 'knowledge_search'
    description = (
        '在企业内部知识库中检索相关文档片段。'
        '当用户问题涉及公司文档、规章制度、产品资料、技术文档等内部知识时调用。'
        '返回匹配的文档片段内容及其来源信息。'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '检索查询语句，应为能体现用户信息需求的自然语言或关键词',
            },
            'top_k': {
                'type': 'integer',
                'description': '返回的文档片段数量，默认 5，范围 1-10',
                'default': 5,
            },
        },
        'required': ['query'],
    }

    def execute(self, ctx: ToolContext, query: str, top_k: int = 5,
                **kwargs) -> Dict[str, Any]:
        """执行知识库检索

        流程：
        1. 调用 hybrid_search（复用现有检索 + Rerank 链路）
        2. 二次权限过滤（与 executor.py 一致，过滤无权访问的文档）
        3. 截断 top_k，格式化为 LLM 易于理解的文本

        Args:
            ctx: 执行上下文（需要 user 做权限过滤、root_types/node_ids 限定检索范围）
            query: 检索查询
            top_k: 返回片段数量，限制 1-10 防止过长

        Returns:
            {'result': str, 'ok': bool, 'meta': {'chunks': [...], 'chunk_ids': [...]}}
            result 为格式化的检索结果文本，meta 保留原始 chunk 信息供引用溯源。
        """
        # 延迟导入避免循环依赖（hybrid_search 链路较重）
        from apps.retrieval.hybrid import hybrid_search
        from apps.knowledge.access import filter_accessible_doc_ids
        from apps.llm.embedding import EmbeddingException

        # 参数防御：top_k 限制在 1-10，避免 LLM 传入异常值
        top_k = max(1, min(int(top_k or 5), 10))

        user = ctx.user
        root_types = ctx.root_types
        node_ids = ctx.node_ids

        try:
            retrieval = hybrid_search(
                query, user, root_types=root_types, node_ids=node_ids, do_rerank=True,
            )
            chunks = retrieval.get('chunks', [])
        except EmbeddingException as e:
            logger.warning(f'[KnowledgeSearchTool] embedding unavailable: {e}')
            return {
                'result': '知识库向量检索服务暂时不可用，请尝试其他方式回答。',
                'ok': False,
                'meta': {'chunks': [], 'chunk_ids': [], 'error': 'embedding_error'},
            }
        except Exception as e:
            logger.exception('[KnowledgeSearchTool] hybrid_search error')
            return {
                'result': f'知识库检索失败: {e.__class__.__name__}: {str(e)[:200]}',
                'ok': False,
                'meta': {'chunks': [], 'chunk_ids': [], 'error': str(e)},
            }

        if not chunks:
            return {
                'result': '未在知识库中检索到相关文档片段。',
                'ok': True,
                'meta': {
                    'chunks': [], 'chunk_ids': [],
                    # 查询改写/分解审计信息（供 QaRecord.route_trace 记录）
                    'transform': retrieval.get('transform'),
                },
            }

        # 二次权限验证：过滤用户无权访问的文档片段
        # 与 executor.py 的逻辑保持一致，防止 Agent 通过工具调用绕过权限
        if user and getattr(user, 'is_authenticated', False):
            doc_ids = list({c['document_id'] for c in chunks})
            accessible_ids = filter_accessible_doc_ids(user, doc_ids)
            filtered = [c for c in chunks if c['document_id'] in accessible_ids]
            if len(filtered) != len(chunks):
                logger.info(f'[KnowledgeSearchTool] permission filter removed {len(chunks) - len(filtered)} chunks')
            chunks = filtered

        if not chunks:
            return {
                'result': '检索到相关文档，但当前用户无权访问。',
                'ok': True,
                'meta': {'chunks': [], 'chunk_ids': [], 'permission_denied': True},
            }

        # 截断 top_k
        chunks = chunks[:top_k]

        # 格式化为 LLM 易于理解的文本
        # 包含：片段序号、文档标题、章节、内容；不含向量分数（对 LLM 无意义）
        lines = []
        for i, c in enumerate(chunks, 1):
            doc_title = c.get('doc_title', '未知文档')
            section = c.get('section_path') or ''
            page = c.get('page_number')
            content = c.get('content', '') or ''
            # 单片段内容截断到 1500 字，避免 context 过长
            if len(content) > 1500:
                content = content[:1500] + '...'
            loc = f'（章节: {section}' + (f'，页码: {page}' if page else '') + '）' if section or page else ''
            lines.append(f'[{i}] 来源: {doc_title}{loc}\n{content}')

        return {
            'result': '\n\n'.join(lines),
            'ok': True,
            'meta': {
                'chunks': chunks,
                'chunk_ids': [c['chunk_id'] for c in chunks],
                'doc_ids': list({c['document_id'] for c in chunks}),
                # 查询改写/分解审计信息（供 QaRecord.route_trace 记录）
                'transform': retrieval.get('transform'),
            },
        }
