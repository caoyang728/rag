"""
knowledge_search 工具 - 内部知识统一检索
内部走三层路由（Wiki → 知识图谱 → RAG 文档兜底），按置信度阈值逐层降级，
保证检索顺序固定为"先 Wiki、再图谱、最后文档兜底"，不依赖 LLM 自主安排工具调用顺序。
复用 apps.graph.router.orchestrate（与 wiki/graphrag/rag 路由模式同一套编排），
并在 RAG 兜底结果上执行与 executor.py 一致的二次权限过滤。
"""
from typing import Any, Dict

from loguru import logger

from .base import BaseTool, ToolContext


class KnowledgeSearchTool(BaseTool):
    """内部知识统一检索工具

    供 Agent 在 ReAct 循环中调用：当用户问题需要查询企业内部知识（Wiki/图谱/文档）时，
    LLM 调用本工具，传入检索 query，工具内部按「Wiki → 知识图谱 → 内部文档」顺序
    执行三层路由，返回首个达到置信度阈值的检索结果。

    说明：wiki_search / graph_search 已并入本工具（避免 LLM 乱序调用导致检索顺序
    不可控），但工具类仍保留注册，兼容工作流 planner 直接生成的 tool 节点。

    复用 orchestrate 全链路（Wiki/GraphRAG 检索 + 向量/BM25/RRF/Rerank），
    并对 RAG 兜底的文档片段执行二次权限过滤，确保不泄露无权访问的内容。
    """

    name = 'knowledge_search'
    description = (
        '在企业内部知识中检索相关资料（按 Wiki 知识页 → 知识图谱 → 内部文档 的顺序自动检索）。'
        '当用户问题涉及公司文档、规章制度、产品资料、技术文档、已整理的 Wiki 知识页'
        '或知识图谱中的实体关系时调用。返回匹配的知识内容及其来源信息。'
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
                'description': '文档兜底检索返回的片段数量，默认 5，范围 1-10',
                'default': 5,
            },
        },
        'required': ['query'],
    }

    def execute(self, ctx: ToolContext, query: str, top_k: int = 5,
                **kwargs) -> Dict[str, Any]:
        """执行统一内部知识检索

        流程：
        1. 调用 orchestrate 三层路由（Wiki → GraphRAG → RAG 兜底）
        2. 按命中来源返回对应内容（Wiki 页面 / 图谱上下文 / 文档片段）
        3. 文档片段二次权限过滤（与 executor.py 一致，过滤无权访问的文档）
        4. 截断 top_k，格式化为 LLM 易于理解的文本

        Args:
            ctx: 执行上下文（需要 user 做权限过滤、root_types/node_ids 限定检索范围）
            query: 检索查询
            top_k: 文档兜底返回片段数量，限制 1-10 防止过长

        Returns:
            {'result': str, 'ok': bool, 'meta': {'chunks': [...], 'chunk_ids': [...],
             'route_source': str, 'route_trace': [...]}}
            result 为格式化的检索结果文本，meta 保留原始 chunk 信息供引用溯源。
        """
        # 延迟导入避免循环依赖（orchestrate 链路较重）
        from apps.graph.router import orchestrate
        from apps.knowledge.access import filter_accessible_doc_ids
        from apps.llm.embedding import EmbeddingException

        # 参数防御：top_k 限制在 1-10，避免 LLM 传入异常值
        top_k = max(1, min(int(top_k or 5), 10))

        user = ctx.user
        root_types = ctx.root_types
        node_ids = ctx.node_ids

        try:
            route = orchestrate(query, user, node_ids=node_ids, root_types=root_types)
        except EmbeddingException as e:
            logger.warning(f'[KnowledgeSearchTool] embedding unavailable: {e}')
            return {
                'result': '知识库向量检索服务暂时不可用，请尝试其他方式回答。',
                'ok': False,
                'meta': {'chunks': [], 'chunk_ids': [], 'error': 'embedding_error'},
            }
        except Exception as e:
            logger.exception('[KnowledgeSearchTool] orchestrate error')
            return {
                'result': f'知识库检索失败: {e.__class__.__name__}: {str(e)[:200]}',
                'ok': False,
                'meta': {'chunks': [], 'chunk_ids': [], 'error': str(e)},
            }

        # 三层路由来源：wiki / graphrag_local / graphrag_global / rag
        source = route.get('source', 'rag')
        chunks = route.get('chunks', []) or []
        meta: Dict[str, Any] = {
            'route_source': source,
            # 三层路由每层置信度与耗时，供审计/评估展示
            'route_trace': route.get('route_trace') or [],
        }

        # 1. Wiki 命中：返回结构化知识页（无需文档权限过滤，Wiki 页可见性由 Wiki 检索器控制）
        if source == 'wiki':
            wiki_page = route.get('wiki_page') or {}
            content = route.get('context') or ''
            return {
                'result': content or '未找到相关的 Wiki 知识页面。',
                'ok': True,
                'meta': {
                    **meta,
                    'wiki_pages': [wiki_page] if wiki_page else [],
                    'hit': bool(wiki_page),
                },
            }

        # 2. 知识图谱命中：返回图谱上下文（实体/关系/社区）
        if source.startswith('graphrag'):
            return {
                'result': route.get('context') or '未检索到相关的知识图谱内容。',
                'ok': True,
                'meta': {
                    **meta,
                    'entities': route.get('entities') or [],
                    'relations': route.get('relations') or [],
                    'communities': route.get('communities') or [],
                    'hit': bool(route.get('context')),
                },
            }

        # 3. RAG 文档兜底：对片段执行二次权限过滤（与 executor.py 保持一致）
        if not chunks:
            return {
                'result': '未在知识库中检索到相关文档片段。',
                'ok': True,
                'meta': {
                    **meta, 'chunks': [], 'chunk_ids': [], 'doc_ids': [],
                },
            }

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
                'meta': {**meta, 'chunks': [], 'chunk_ids': [], 'doc_ids': [],
                         'permission_denied': True},
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
                **meta,
                'chunks': chunks,
                'chunk_ids': [c['chunk_id'] for c in chunks],
                'doc_ids': list({c['document_id'] for c in chunks}),
            },
        }
