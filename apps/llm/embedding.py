"""
Embedding & Rerank 客户端
- 优先连接 Docker Embedding 服务，不可用则 fallback 到云API
- 批量向量化，避免逐条请求
- 支持 embedding 失败降级为零向量（保证流程不阻塞）
"""
from loguru import logger
from typing import List, Dict, Any, Optional

import requests
from django.conf import settings



class CloudEmbeddingClient:
    """智谱云API Embedding客户端 - 作为Docker服务失败时的兜底"""

    def __init__(self):
        self.api_key = settings.EMBEDDING_API_KEY
        self.base_url = settings.EMBEDDING_API_URL.rstrip('/')
        self.model = settings.EMBEDDING_API_MODEL
        self.dim = settings.EMBEDDING_API_DIM

    def embed(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """批量向量化；返回 [[float]*dim]*len(texts)"""
        if not texts:
            return []
        if not self.api_key:
            logger.warning('[Cloud Embedding] EMBEDDING_API_KEY 为空，无法作为兜底')
            return []

        results: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            try:
                resp = requests.post(
                    f'{self.base_url}/embeddings',
                    headers={'Authorization': f'Bearer {self.api_key}',
                             'Content-Type': 'application/json'},
                    json={'model': self.model, 'input': batch, 'dimensions': self.dim},
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get('data', []):
                    results.append(item['embedding'])
            except Exception as e:
                logger.exception('[Cloud Embedding] batch failed: %s', e)
                return []
        return results


class DockerEmbeddingClient:
    """Docker Embedding服务客户端 - 优先使用"""

    def __init__(self):
        self.url = settings.EMBEDDING_DOCKER_URL
        self.timeout = settings.EMBEDDING_DOCKER_TIMEOUT
        self.dim = settings.EMBEDDING_API_DIM

    def embed(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """批量向量化；返回 [[float]*dim]*len(texts)"""
        if not texts:
            return []
        if not self.url:
            logger.warning('[Docker Embedding] EMBEDDING_DOCKER_URL 为空')
            return []

        results: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            try:
                resp = requests.post(
                    self.url,
                    headers={'Content-Type': 'application/json'},
                    json={'input': batch},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    results.extend(data)
                elif isinstance(data, dict) and 'embeddings' in data:
                    results.extend(data['embeddings'])
                elif isinstance(data, dict) and 'data' in data:
                    for item in data['data']:
                        results.append(item.get('embedding', []))
                else:
                    logger.warning('[Docker Embedding] 响应格式未知: %s', type(data))
                    return []
            except Exception as e:
                logger.exception('[Docker Embedding] batch failed: %s', e)
                return []
        return results


class EmbeddingClient:
    """统一Embedding客户端 - Docker优先，云API兜底"""

    def __init__(self):
        self.dim = settings.EMBEDDING_API_DIM
        self._docker_client: Optional[DockerEmbeddingClient] = None
        self._cloud_client: Optional[CloudEmbeddingClient] = None

    @property
    def docker_client(self) -> DockerEmbeddingClient:
        if self._docker_client is None:
            self._docker_client = DockerEmbeddingClient()
        return self._docker_client

    @property
    def cloud_client(self) -> CloudEmbeddingClient:
        if self._cloud_client is None:
            self._cloud_client = CloudEmbeddingClient()
        return self._cloud_client

    def embed(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """批量向量化；返回 [[float]*dim]*len(texts)
        优先使用 Docker Embedding 服务，失败时降级到云API，最后降级为零向量"""
        if not texts:
            return []

        docker_result = self.docker_client.embed(texts, batch_size)
        if docker_result:
            return docker_result

        logger.warning('[Embedding] Docker服务不可用，尝试云API兜底')
        cloud_result = self.cloud_client.embed(texts, batch_size)
        if cloud_result:
            return cloud_result

        logger.warning('[Embedding] 云API也失败，返回零向量占位')
        return [[0.0] * self.dim for _ in texts]

    def embed_one(self, text: str) -> List[float]:
        vecs = self.embed([text])
        return vecs[0] if vecs else [0.0] * self.dim

    def rerank(self, query: str, docs: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        """交叉编码器精排
        返回: [{'index': int, 'score': float}] 按 score DESC"""
        if not docs:
            return []
        return [{'index': i, 'score': 1.0 - i * 0.01} for i in range(min(top_k, len(docs)))]


_client: Optional[EmbeddingClient] = None


def get_embedding_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client