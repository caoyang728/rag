"""
Embedding & Rerank 客户端
- 通过 EMBEDDING_PROVIDER 控制优先使用 Docker 还是云 API
- 批量向量化，避免逐条请求
- 云API支持429限流重试与指数退避
- 全部失败时抛出 EmbeddingException
"""
from loguru import logger
from typing import List, Dict, Any, Optional
import time
import random

import requests
from django.conf import settings
from rag_project.config import EmbeddingConfig


class EmbeddingException(Exception):
    """Embedding服务异常 - 当所有embedding服务均不可用时抛出"""
    pass


class ApiEmbeddingClient:
    """云API Embedding & Rerank 客户端 - 使用通用变量配置
    API兼容OpenAI格式，支持embedding和rerank"""

    def __init__(self):
        self.api_key = EmbeddingConfig.api_key()
        self.base_url = EmbeddingConfig.base_url().rstrip('/')
        self.embed_model = EmbeddingConfig.model()
        self.rerank_model = EmbeddingConfig.rerank_model()
        self.dim = EmbeddingConfig.dim()
        # 调试日志：确认API配置
        logger.info('[API Embedding] initialized - api_key_set: %s, base_url: %s, embed_model: %s, rerank_model: %s, dim: %d',
                    bool(self.api_key), self.base_url, self.embed_model, self.rerank_model, self.dim)

    def embed(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """批量向量化；返回 [[float]*dim]*len(texts)"""
        if not texts:
            return []
        if not self.api_key:
            logger.warning('[API Embedding] EMBEDDING_API_KEY 为空')
            return []

        results: List[List[float]] = []
        max_retries = 3
        # 调试日志：显示完整请求信息
        logger.info('[API Embedding] 请求 - base_url: %s, model: %s, texts: %d',
                    self.base_url, self.embed_model, len(texts))

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    url = f'{self.base_url}/embeddings'
                    # SiliconFlow API 不支持 dimensions 参数，会返回 400 错误
                    # 其他平台如 OpenAI 支持，所以这里不传递该参数
                    payload = {'model': self.embed_model, 'input': batch}
                    logger.debug('[API Embedding] 发送请求 - url: %s, batch_size: %d', url, len(batch))

                    resp = requests.post(
                        url,
                        headers={'Authorization': f'Bearer {self.api_key}',
                                 'Content-Type': 'application/json'},
                        json=payload,
                        timeout=60,
                    )
                    logger.debug('[API Embedding] 响应状态码: %d', resp.status_code)

                    if resp.status_code == 429 and attempt < max_retries:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning('[API Embedding] 429 Too Many Requests, '
                                       'retry %d/%d after %.1fs', attempt + 1, max_retries, wait_time)
                        time.sleep(wait_time)
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    logger.debug('[API Embedding] 响应数据: %s', str(data)[:500])

                    for item in data.get('data', []):
                        results.append(item['embedding'])
                    logger.info('[API Embedding] 批次处理成功，生成%d个向量', len(results))
                    break

                except requests.exceptions.HTTPError as e:
                    last_error = e
                    # 记录详细错误信息
                    error_detail = ''
                    try:
                        error_detail = resp.json().get('error', {}).get('message', str(resp.text))
                    except:
                        error_detail = resp.text[:200]
                    logger.error('[API Embedding] HTTP错误 %d: %s', resp.status_code, error_detail)
                    if resp.status_code == 429:
                        continue
                    break

                except requests.exceptions.RequestException as e:
                    last_error = e
                    logger.error('[API Embedding] 请求异常: %s', str(e))
                    break

                except Exception as e:
                    last_error = e
                    logger.exception('[API Embedding] 未知异常: %s', e)
                    break

            else:
                logger.error('[API Embedding] 所有%d次重试均失败', max_retries)
                return []

            if last_error:
                return []

        logger.info('[API Embedding] 全部处理完成，共生成%d个向量', len(results))
        return results

    def rerank(self, query: str, docs: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        """交叉编码器精排"""
        if not docs or not query:
            return []
        if not self.api_key:
            logger.warning('[API Rerank] EMBEDDING_API_KEY 为空')
            return []

        try:
            resp = requests.post(
                f'{self.base_url}/rerank',
                headers={'Authorization': f'Bearer {self.api_key}',
                         'Content-Type': 'application/json'},
                json={
                    'model': self.rerank_model,
                    'query': query,
                    'documents': docs,
                    'top_n': top_k
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            hits = []
            for item in data.get('results', []):
                hits.append({
                    'index': item.get('index', 0),
                    'score': float(item.get('relevance_score', 0.0))
                })
            logger.info('[API Rerank] returned=%d', len(hits))
            return hits
        except Exception as e:
            logger.exception('[API Rerank] failed: %s', e)
            return []


class DockerEmbeddingClient:
    """Docker Embedding服务客户端 - 本地部署优先使用"""

    def __init__(self):
        self.url = EmbeddingConfig.docker_url()
        self.timeout = EmbeddingConfig.docker_timeout()
        self.dim = EmbeddingConfig.dim()
        # 调试日志：确认Docker服务配置
        logger.info('[Docker Embedding] initialized - url: %s, timeout: %d, dim: %d',
                    self.url or 'NOT SET', self.timeout, self.dim)

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
    """统一Embedding客户端
    通过 EMBEDDING_PROVIDER 控制优先级：
    - docker: 优先使用 Docker Embedding 服务，失败降级到云API
    - api:    优先使用云API，失败降级到 Docker Embedding 服务
    """

    def __init__(self):
        self.dim = EmbeddingConfig.dim()
        self.provider = EmbeddingConfig.provider()  # docker / api
        self._api_client: Optional[ApiEmbeddingClient] = None
        self._docker_client: Optional[DockerEmbeddingClient] = None

    @property
    def api_client(self) -> ApiEmbeddingClient:
        if self._api_client is None:
            self._api_client = ApiEmbeddingClient()
        return self._api_client

    @property
    def docker_client(self) -> DockerEmbeddingClient:
        if self._docker_client is None:
            self._docker_client = DockerEmbeddingClient()
        return self._docker_client

    def embed(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """批量向量化；返回 [[float]*dim]*len(texts)
        优先级由 EMBEDDING_PROVIDER 控制，全部失败时抛出异常"""
        if not texts:
            return []

        # 根据配置决定优先级
        if self.provider == 'docker':
            # Docker优先，API兜底
            return self._embed_docker_first(texts, batch_size)
        else:
            # API优先，Docker兜底
            return self._embed_api_first(texts, batch_size)

    def _embed_docker_first(self, texts: List[str], batch_size: int) -> List[List[float]]:
        """Docker优先，API兜底"""
        # 优先尝试 Docker Embedding 服务
        docker_result = self.docker_client.embed(texts, batch_size)
        if docker_result:
            logger.info('[Embedding] 使用Docker服务成功，生成%d个向量', len(docker_result))
            return docker_result

        logger.warning('[Embedding] Docker服务不可用或返回空结果，尝试云API兜底')

        # 降级到云API
        api_result = self.api_client.embed(texts, batch_size)
        if api_result:
            logger.info('[Embedding] 使用云API兜底成功，生成%d个向量', len(api_result))
            return api_result

        # 全部失败，抛出异常
        raise EmbeddingException("所有embedding服务均不可用：Docker服务失败，云API也失败")

    def _embed_api_first(self, texts: List[str], batch_size: int) -> List[List[float]]:
        """API优先，Docker兜底"""
        # 优先尝试云API
        api_result = self.api_client.embed(texts, batch_size)
        if api_result:
            logger.info('[Embedding] 使用云API成功，生成%d个向量', len(api_result))
            return api_result

        logger.warning('[Embedding] 云API不可用或返回空结果，尝试Docker服务兜底')

        # 降级到 Docker Embedding 服务
        docker_result = self.docker_client.embed(texts, batch_size)
        if docker_result:
            logger.info('[Embedding] 使用Docker服务兜底成功，生成%d个向量', len(docker_result))
            return docker_result

        # 全部失败，抛出异常
        raise EmbeddingException("所有embedding服务均不可用：云API失败，Docker服务也失败")

    def embed_one(self, text: str) -> List[float]:
        vecs = self.embed([text])
        return vecs[0] if vecs else [0.0] * self.dim

    def rerank(self, query: str, docs: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        """交叉编码器精排 - 使用云API
        返回: [{'index': int, 'score': float}] 按 score DESC"""
        if not docs:
            return []
        return self.api_client.rerank(query, docs, top_k)


_client: Optional[EmbeddingClient] = None


def get_embedding_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client
