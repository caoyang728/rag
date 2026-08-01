"""
web_search 工具 - 联网搜索
默认使用 Tavily API（专为 LLM 设计，返回干净摘要），无 API Key 或调用失败时
自动降级到 DuckDuckGo（免费、无需 Key），保证工具可用性。

环境变量：
- TAVILY_API_KEY: Tavily API 密钥（https://tavily.com，免费 1000 次/月）
"""
import json
from typing import Any, Dict, List

from loguru import logger

from .base import BaseTool, ToolContext


class WebSearchTool(BaseTool):
    """联网搜索工具

    供 Agent 补足知识库盲区：当用户问题涉及实时信息、外部新闻、公开技术资料等
    知识库未覆盖的内容时，LLM 调用本工具获取最新网络信息。

    数据源优先级：Tavily（有 API Key 时）→ DuckDuckGo（兜底）。
    """

    name = 'web_search'
    description = (
        '在互联网上搜索最新信息。当用户问题涉及实时新闻、外部公开资料、'
        '技术文档、知识库未覆盖的内容时调用。返回搜索结果摘要与来源链接。'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '搜索关键词或问题',
            },
            'max_results': {
                'type': 'integer',
                'description': '返回结果数量，默认 5，范围 1-10',
                'default': 5,
            },
        },
        'required': ['query'],
    }

    def execute(self, ctx: ToolContext, query: str, max_results: int = 5,
                **kwargs) -> Dict[str, Any]:
        """执行联网搜索

        优先 Tavily，失败降级 DuckDuckGo，保证工具链可用性。
        两个数据源都不可用时返回明确错误，让 LLM 据此调整策略。

        Args:
            ctx: 执行上下文（本工具不使用 user，但保持接口一致）
            query: 搜索关键词
            max_results: 返回结果数量，限制 1-10

        Returns:
            {'result': str, 'ok': bool, 'meta': {'results': [...], 'source': str}}
        """
        max_results = max(1, min(int(max_results or 5), 10))

        # 优先尝试 Tavily
        result = self._search_tavily(query, max_results)
        if result['ok']:
            return result

        # Tavily 失败/无 Key，降级 DuckDuckGo
        logger.info('[WebSearchTool] Tavily unavailable, fallback to DuckDuckGo')
        result_ddg = self._search_duckduckgo(query, max_results)
        if result_ddg['ok']:
            return result_ddg

        # 两个数据源都失败
        return {
            'result': f'联网搜索暂时不可用。Tavily: {result.get("result", "")}; '
                      f'DuckDuckGo: {result_ddg.get("result", "")}',
            'ok': False,
            'meta': {'results': [], 'source': 'none'},
        }

    def _search_tavily(self, query: str, max_results: int) -> Dict[str, Any]:
        """Tavily API 搜索

        Tavily 专为 LLM 设计，返回包含 answer 摘要和结构化结果。
        需要 TAVILY_API_KEY 环境变量。
        """
        from django.conf import settings
        import urllib.request
        import urllib.error

        api_key = getattr(settings, 'TAVILY_API_KEY', '') or ''
        if not api_key:
            return {'result': '未配置 TAVILY_API_KEY', 'ok': False,
                    'meta': {'results': [], 'source': 'tavily'}}

        try:
            payload = json.dumps({
                'api_key': api_key,
                'query': query,
                'max_results': max_results,
                'search_depth': 'basic',
            }).encode('utf-8')
            req = urllib.request.Request(
                'https://api.tavily.com/search',
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode('utf-8'))

            results: List[Dict[str, str]] = []
            for item in (data.get('results') or [])[:max_results]:
                results.append({
                    'title': item.get('title', ''),
                    'url': item.get('url', ''),
                    'content': item.get('content', '')[:800],
                })

            # Tavily 可能直接给出 answer 字段
            answer = data.get('answer', '')
            lines = []
            if answer:
                lines.append(f'摘要: {answer}')
            for i, r in enumerate(results, 1):
                lines.append(f'[{i}] {r["title"]}\n   链接: {r["url"]}\n   内容: {r["content"]}')

            return {
                'result': '\n'.join(lines) if lines else '未找到相关结果',
                'ok': True,
                'meta': {'results': results, 'source': 'tavily'},
            }
        except Exception as e:
            logger.warning('[WebSearchTool] Tavily error: %s', e)
            return {'result': f'Tavily 调用失败: {e}', 'ok': False,
                    'meta': {'results': [], 'source': 'tavily'}}

    def _search_duckduckgo(self, query: str, max_results: int) -> Dict[str, Any]:
        """DuckDuckGo 搜索（兜底方案）

        使用 duckduckgo_search 库（无需 API Key），失败则返回明确错误。
        库未安装时给出安装提示。
        """
        try:
            # duckduckgo_search 是第三方库，可能未安装
            from duckduckgo_search import DDGS
        except ImportError:
            return {
                'result': 'DuckDuckGo 库未安装 (pip install duckduckgo_search)',
                'ok': False,
                'meta': {'results': [], 'source': 'duckduckgo'},
            }

        try:
            results: List[Dict[str, str]] = []
            with DDGS() as ddgs:
                for item in ddgs.text(query, max_results=max_results):
                    results.append({
                        'title': item.get('title', ''),
                        'url': item.get('href') or item.get('url', ''),
                        'content': (item.get('body') or item.get('content') or '')[:800],
                    })

            if not results:
                return {'result': 'DuckDuckGo 未找到相关结果', 'ok': True,
                        'meta': {'results': [], 'source': 'duckduckgo'}}

            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f'[{i}] {r["title"]}\n   链接: {r["url"]}\n   内容: {r["content"]}')

            return {
                'result': '\n'.join(lines),
                'ok': True,
                'meta': {'results': results, 'source': 'duckduckgo'},
            }
        except Exception as e:
            logger.warning('[WebSearchTool] DuckDuckGo error: %s', e)
            return {'result': f'DuckDuckGo 调用失败: {e}', 'ok': False,
                    'meta': {'results': [], 'source': 'duckduckgo'}}
