"""
LLM Provider 抽象基类
LLM 适配层——业务代码不感知具体供应商，通过 factory 切换
支持：chat（同步）/ stream（SSE 流式）/ embed（向量化）/ rerank
"""
from abc import ABC, abstractmethod
from typing import Iterator, List, Dict, Any, Optional


class BaseLLMProvider(ABC):
    """所有 Provider 必须实现以下接口"""

    name: str = 'base'
    default_model: str = ''

    def __init__(self, api_key: str, base_url: str = '', model: str = '',
                 timeout: int = 60, **kwargs):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model or self.default_model
        self.timeout = timeout
        self.extra = kwargs

    @abstractmethod
    def chat(self, messages: List[Dict[str, str]],
             temperature: float = 0.3,
             max_tokens: int = 2048,
             **kwargs) -> Dict[str, Any]:
        """同步 chat
        返回: {'content': str, 'prompt_tokens': int, 'completion_tokens': int,
              'total_tokens': int, 'latency_ms': int, 'model': str, 'cost': float}
        """
        raise NotImplementedError

    @abstractmethod
    def stream(self, messages: List[Dict[str, str]],
               temperature: float = 0.3,
               max_tokens: int = 2048,
               **kwargs) -> Iterator[Dict[str, Any]]:
        """流式 chat，yield {'delta': str, 'finish': bool, ...}"""
        raise NotImplementedError

    def embed(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """默认走 embedding.py，Provider 可选覆盖"""
        raise NotImplementedError

    def rerank(self, query: str, docs: List[str], top_k: int = 5,
               model: Optional[str] = None) -> List[Dict[str, Any]]:
        """默认走 rerank，Provider 可选覆盖，返回 [{index, score}]"""
        raise NotImplementedError
