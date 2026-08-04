"""
Embedding & Rerank 客户端
- 通过 EMBEDDING_PROVIDER 控制优先使用 Docker 还是云 API
- 批量向量化，避免逐条请求
- 云API支持429限流重试与指数退避
- 全部失败时抛出 EmbeddingException

配置来源：
- model_name：SystemConfig.EMBEDDING_MODEL / RERANK_MODEL
- base_url：LLMModel 表按 model_name 反查
- api_key：从 env 读取（敏感凭证不入库）
- dim / provider / docker_url / docker_timeout：由 env 控制
  （dim 与向量索引强相关，改了需重建索引；docker_url 与部署拓扑强相关）
"""
from loguru import logger
from typing import List, Dict, Any, Optional
import time
import random

import requests
from rag_project.config import EmbeddingConfig


class EmbeddingException(Exception):
    """Embedding服务异常 - 当所有embedding服务均不可用时抛出"""
    pass


class ApiEmbeddingClient:
    """云API Embedding & Rerank 客户端 - 使用通用变量配置
    API兼容OpenAI格式，支持embedding和rerank

    配置读取：DB SystemConfig/LLMModel；
    每次实例化时读取最新配置（含 5min 缓存），无需重启即可生效。
    """

    def __init__(self):
        # api_key 从 env 取（敏感凭证不入库）
        self.api_key = EmbeddingConfig.api_key()
        # model_name / base_url：从 DB 读取
        self.embed_model, self.base_url = self._resolve_embedding_params()
        self.rerank_model, _ = self._resolve_rerank_params()
        self.dim = EmbeddingConfig.dim()
        logger.info(f'[API Embedding] initialized - api_key_set={bool(self.api_key)}, base_url={self.base_url}, embed_model={self.embed_model}, rerank_model={self.rerank_model}, dim={self.dim}')

    def _resolve_embedding_params(self):
        """解析 Embedding 模型与 base_url

        Returns: (embed_model, base_url)
        - embed_model：SystemConfig.EMBEDDING_MODEL
        - base_url：LLMModel 表按 model_name 反查；未命中时记 warning，base_url 留空
        """
        from apps.system.config_loader import get_config_value, get_llm_model_config
        embed_model = get_config_value('EMBEDDING_MODEL', default='', value_type='string')
        if not embed_model:
            logger.warning('[API Embedding] SystemConfig.EMBEDDING_MODEL 未配置，请在系统配置页面设置')
            return '', ''
        llm_row = get_llm_model_config(embed_model, model_type='embedding')
        if llm_row and llm_row.get('base_url'):
            base_url = llm_row['base_url'].rstrip('/')
        else:
            logger.warning(f'[API Embedding] LLMModel 表未配置 embedding model={embed_model}，请在模型管理中添加')
            base_url = ''
        return embed_model, base_url

    def _resolve_rerank_params(self):
        """解析 Rerank 模型与 base_url

        Returns: (rerank_model, base_url)
        - rerank_model：SystemConfig.RERANK_MODEL
        - base_url：LLMModel 表按 model_name 反查；未命中时复用 embedding 的 base_url
          （rerank 与 embedding 通常共用同一 base_url，如 SiliconFlow）
        """
        from apps.system.config_loader import get_config_value, get_llm_model_config
        rerank_model = get_config_value('RERANK_MODEL', default='', value_type='string')
        if not rerank_model:
            # Rerank 可选，未配置时返回空，调用方按业务策略跳过 rerank
            return '', ''
        llm_row = get_llm_model_config(rerank_model, model_type='rerank')
        if llm_row and llm_row.get('base_url'):
            base_url = llm_row['base_url'].rstrip('/')
        else:
            # 复用 embedding 的 base_url（共用场景），避免 rerank 调用断裂
            base_url = self.base_url
        return rerank_model, base_url

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
        logger.info(f'[API Embedding] 请求 - base_url: {self.base_url}, model: {self.embed_model}, texts: {len(texts)}')

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    url = f'{self.base_url}/embeddings'
                    # SiliconFlow API 不支持 dimensions 参数，会返回 400 错误
                    # 其他平台如 OpenAI 支持，所以这里不传递该参数
                    payload = {'model': self.embed_model, 'input': batch}
                    # debug 日志仅记录批次数量与总字符数，避免把用户原始文本打印到日志中
                    total_chars = sum(len(t or '') for t in batch)
                    logger.debug(f'[API Embedding] 发送请求 - url: {url}, batch_size: {len(batch)}, total_chars: {total_chars}')

                    resp = requests.post(
                        url,
                        headers={'Authorization': f'Bearer {self.api_key}',
                                 'Content-Type': 'application/json'},
                        json=payload,
                        timeout=60,
                    )
                    logger.debug(f'[API Embedding] 响应状态码: {resp.status_code}')

                    if resp.status_code == 429 and attempt < max_retries:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(f'[API Embedding] 429 Too Many Requests, retry {attempt + 1}/{max_retries} after {wait_time:.1f}s')
                        time.sleep(wait_time)
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    logger.debug(f'[API Embedding] 响应数据: {str(data)[:500]}')

                    for item in data.get('data', []):
                        results.append(item['embedding'])
                    logger.info(f'[API Embedding] 批次处理成功，生成{len(results)}个向量')
                    break

                except requests.exceptions.HTTPError as e:
                    last_error = e
                    # 记录详细错误信息
                    error_detail = ''
                    try:
                        error_detail = resp.json().get('error', {}).get('message', str(resp.text))
                    except Exception:
                        error_detail = resp.text[:200]
                    logger.error(f'[API Embedding] HTTP错误 {resp.status_code}: {error_detail}')
                    if resp.status_code == 429:
                        continue
                    break

                except requests.exceptions.RequestException as e:
                    last_error = e
                    logger.error(f'[API Embedding] 请求异常: {e}')
                    break

                except Exception as e:
                    last_error = e
                    logger.exception(f'[API Embedding] 未知异常: {e}')
                    break

            else:
                logger.error(f'[API Embedding] 所有{max_retries}次重试均失败')
                return []

            if last_error:
                return []

        logger.info(f'[API Embedding] 全部处理完成，共生成{len(results)}个向量')
        return results

    def rerank(self, query: str, docs: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        """交叉编码器精排"""
        if not docs or not query:
            return []
        if not self.api_key:
            logger.warning('[API Rerank] EMBEDDING_API_KEY 为空')
            return []
        # base_url / rerank_model 未配置时提前降级，避免发空请求到无效 URL 抛异常再捕获
        if not self.base_url or not self.rerank_model:
            logger.warning(f'[API Rerank] 跳过 - base_url={bool(self.base_url)} rerank_model={bool(self.rerank_model)}')
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
            logger.info(f'[API Rerank] returned={len(hits)}')
            return hits
        except Exception as e:
            logger.exception(f'[API Rerank] failed: {e}')
            return []


class DockerEmbeddingClient:
    """Docker Embedding服务客户端 - 本地部署优先使用"""

    def __init__(self):
        self.url = EmbeddingConfig.docker_url()
        self.timeout = EmbeddingConfig.docker_timeout()
        self.dim = EmbeddingConfig.dim()
        # 调试日志：确认Docker服务配置
        logger.info(f'[Docker Embedding] initialized - url: {self.url or "NOT SET"}, timeout: {self.timeout}, dim: {self.dim}')

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
                    logger.warning(f'[Docker Embedding] 响应格式未知: {type(data)}')
                    return []
            except Exception as e:
                logger.exception(f'[Docker Embedding] batch failed: {e}')
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
            logger.info(f'[Embedding] 使用Docker服务成功，生成{len(docker_result)}个向量')
            return docker_result

        logger.warning('[Embedding] Docker服务不可用或返回空结果，尝试云API兜底')

        # 降级到云API
        api_result = self.api_client.embed(texts, batch_size)
        if api_result:
            logger.info(f'[Embedding] 使用云API兜底成功，生成{len(api_result)}个向量')
            return api_result

        # 全部失败，抛出异常
        raise EmbeddingException("所有embedding服务均不可用：Docker服务失败，云API也失败")

    def _embed_api_first(self, texts: List[str], batch_size: int) -> List[List[float]]:
        """API优先，Docker兜底"""
        # 优先尝试云API
        api_result = self.api_client.embed(texts, batch_size)
        if api_result:
            logger.info(f'[Embedding] 使用云API成功，生成{len(api_result)}个向量')
            return api_result

        logger.warning('[Embedding] 云API不可用或返回空结果，尝试Docker服务兜底')

        # 降级到 Docker Embedding 服务
        docker_result = self.docker_client.embed(texts, batch_size)
        if docker_result:
            logger.info(f'[Embedding] 使用Docker服务兜底成功，生成{len(docker_result)}个向量')
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
