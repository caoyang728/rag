"""
apps.llm.embedding 单元测试 —— Embedding & Rerank 客户端

覆盖范围：
- ApiEmbeddingClient：embed（空入参/无 api_key/成功/429 重试/全失败）、rerank（空/无 key/无配置/成功）
- DockerEmbeddingClient：embed（空/无 url/成功/多种响应格式 list/embeddings/data）
- EmbeddingClient：统一客户端优先级（docker-first / api-first / 互为兜底 / 全失败抛异常）
- embed_one：单条向量化、空结果回退零向量
- get_embedding_client：单例

全部用 mock：
embedding 客户端在 __init__ 即读 SystemConfig/LLMModel/env，且 embed/rerank 会发起
HTTP 请求。本测试聚焦分支逻辑（优先级、降级、重试、响应解析），故统一 mock
EmbeddingConfig（避免 env/DB 依赖）、config_loader（避免 DB/Redis）、requests.post
（避免真实网络调用）。这样能精准验证“429 重试”“兜底切换”“响应格式分支”等契约。
"""
import pytest
from unittest.mock import patch, MagicMock

import requests

from apps.llm.embedding import (
    ApiEmbeddingClient,
    DockerEmbeddingClient,
    EmbeddingClient,
    EmbeddingException,
    get_embedding_client,
)
import apps.llm.embedding as emb_mod


# ----------------------------------------------------------------------------
# 构造辅助：用 mock 配置构造客户端，配置仅在 __init__ 阶段读取，构造完成后即释放
# ----------------------------------------------------------------------------
def _make_api_client(api_key='sk-test', embed_model='bge-m3',
                     base_url='https://api.x.com/v1', rerank_model='bge-reranker',
                     dim=1024):
    """构造 ApiEmbeddingClient，所有外部配置已 mock

    get_config_value 按 key 返回 embed_model / rerank_model；
    get_llm_model_config 统一返回 base_url，保证 embed 与 rerank 都有可用地址。
    """
    with patch('apps.llm.embedding.EmbeddingConfig') as mock_cfg, \
         patch('apps.system.config_loader.get_config_value') as mock_get_cfg, \
         patch('apps.system.config_loader.get_llm_model_config') as mock_llm_model:
        mock_cfg.api_key.return_value = api_key
        mock_cfg.dim.return_value = dim
        mock_get_cfg.side_effect = lambda key, default=None, value_type=None: {
            'EMBEDDING_MODEL': embed_model,
            'RERANK_MODEL': rerank_model,
        }.get(key, default)
        mock_llm_model.return_value = {'base_url': base_url}
        return ApiEmbeddingClient()


def _make_docker_client(url='http://docker:8080/embed', timeout=30, dim=1024):
    """构造 DockerEmbeddingClient，docker_url/timeout/dim 全部 mock"""
    with patch('apps.llm.embedding.EmbeddingConfig') as mock_cfg:
        mock_cfg.docker_url.return_value = url
        mock_cfg.docker_timeout.return_value = timeout
        mock_cfg.dim.return_value = dim
        return DockerEmbeddingClient()


def _make_embedding_client(provider='docker', dim=1024):
    """构造统一 EmbeddingClient，仅注入 provider/dim；子客户端由测试自行替换为 mock"""
    with patch('apps.llm.embedding.EmbeddingConfig') as mock_cfg:
        mock_cfg.dim.return_value = dim
        mock_cfg.provider.return_value = provider
        return EmbeddingClient()


# ============================================================================
# ApiEmbeddingClient —— 云 API embed
# ============================================================================
class TestApiEmbed:
    """ApiEmbeddingClient.embed 行为测试"""

    @pytest.mark.unit
    def test_api_embed_empty(self):
        """空文本列表直接返回 []，不发起任何请求"""
        client = _make_api_client()
        with patch('apps.llm.embedding.requests.post') as mock_post:
            assert client.embed([]) == []
            mock_post.assert_not_called()

    @pytest.mark.unit
    def test_api_embed_no_api_key(self):
        """api_key 为空时记 warning 并返回 []，不发请求"""
        client = _make_api_client(api_key='')
        with patch('apps.llm.embedding.requests.post') as mock_post:
            assert client.embed(['x']) == []
            mock_post.assert_not_called()

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_api_embed_success(self, mock_post):
        """正常 200 响应：从 data[].embedding 解析向量列表"""
        client = _make_api_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'data': [
                {'embedding': [0.1, 0.2, 0.3]},
                {'embedding': [0.4, 0.5, 0.6]},
            ]
        }
        mock_post.return_value = mock_resp

        result = client.embed(['hello', 'world'])

        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    @pytest.mark.unit
    @patch('apps.llm.embedding.time.sleep')
    @patch('apps.llm.embedding.requests.post')
    def test_api_embed_429_retry(self, mock_post, mock_sleep):
        """首次 429 触发指数退避重试，重试成功后返回向量"""
        client = _make_api_client()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {'data': [{'embedding': [0.1]}]}
        mock_post.side_effect = [resp_429, resp_200]

        result = client.embed(['x'])

        # 应重试一次（共两次调用），并 sleep 过一次
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once()
        assert result == [[0.1]]

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_api_embed_all_fail(self, mock_post):
        """请求持续异常时最终返回 []（不向上抛出，调用方按空结果降级）"""
        client = _make_api_client()
        # 网络异常属 RequestException，命中 except 后 break 并返回 []
        mock_post.side_effect = requests.exceptions.ConnectionError('network down')

        assert client.embed(['x']) == []


# ============================================================================
# ApiEmbeddingClient —— 配置解析缺失分支
# ============================================================================
class TestApiConfigResolution:
    """_resolve_embedding_params / _resolve_rerank_params 配置缺失分支"""

    @pytest.mark.unit
    def test_embedding_model_not_in_llm_table(self):
        """LLMModel 表未配置 embedding model 时 base_url 留空（记 warning）"""
        with patch('apps.llm.embedding.EmbeddingConfig') as mock_cfg, \
             patch('apps.system.config_loader.get_config_value') as mock_get_cfg, \
             patch('apps.system.config_loader.get_llm_model_config') as mock_llm_model:
            mock_cfg.api_key.return_value = 'sk-test'
            mock_get_cfg.side_effect = lambda key, default=None, value_type=None: {
                'EMBEDDING_MODEL': 'bge-m3',
                'RERANK_MODEL': 'bge-reranker',
            }.get(key, default)
            # LLMModel 表未命中（返回 None）→ base_url 留空
            mock_llm_model.return_value = None
            client = ApiEmbeddingClient()

        assert client.base_url == ''
        assert client.embed_model == 'bge-m3'

    @pytest.mark.unit
    def test_rerank_reuses_embedding_base_url(self):
        """LLMModel 表未配置 rerank 模型时复用 embedding 的 base_url"""
        with patch('apps.llm.embedding.EmbeddingConfig') as mock_cfg, \
             patch('apps.system.config_loader.get_config_value') as mock_get_cfg, \
             patch('apps.system.config_loader.get_llm_model_config') as mock_llm_model:
            mock_cfg.api_key.return_value = 'sk-test'
            mock_cfg.dim.return_value = 1024
            mock_get_cfg.side_effect = lambda key, default=None, value_type=None: {
                'EMBEDDING_MODEL': 'bge-m3',
                'RERANK_MODEL': 'bge-reranker',
            }.get(key, default)
            # 第一次调用（embedding）返回 base_url；第二次调用（rerank）未配置
            mock_llm_model.side_effect = [
                {'base_url': 'https://api.x.com/v1'},
                None,
            ]
            client = ApiEmbeddingClient()

        assert client.embed_model == 'bge-m3'
        assert client.rerank_model == 'bge-reranker'
        assert client.base_url == 'https://api.x.com/v1'


# ============================================================================
# ApiEmbeddingClient —— 云 API embed 异常分支
# ============================================================================
class TestApiEmbedErrorBranches:
    """ApiEmbeddingClient.embed 的 HTTP 错误 / 未知异常 / 重试耗尽分支"""

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_api_embed_http_error_with_detail(self, mock_post):
        """HTTP 400：从响应 error.message 提取错误详情并返回 []（非 429 不重试）"""
        client = _make_api_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = 'plain text'
        mock_resp.json.return_value = {'error': {'message': 'invalid request'}}
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError('400 Client Error')
        mock_post.return_value = mock_resp

        assert client.embed(['x']) == []
        assert mock_post.call_count == 1

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_api_embed_http_error_json_parse_fail(self, mock_post):
        """HTTP 错误且响应体解析失败时回退到 resp.text[:200]"""
        client = _make_api_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = 'raw body'
        mock_resp.json.side_effect = ValueError('no json')
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError('400 Client Error')
        mock_post.return_value = mock_resp

        assert client.embed(['x']) == []

    @pytest.mark.unit
    @patch('apps.llm.embedding.time.sleep')
    @patch('apps.llm.embedding.requests.post')
    def test_api_embed_429_retries_exhausted(self, mock_post, mock_sleep):
        """持续 429 时耗尽全部重试机会并返回 []"""
        client = _make_api_client()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        # 最后一次 429 走 HTTPError 分支（raise_for_status 抛错）并 continue
        resp_429.raise_for_status.side_effect = requests.exceptions.HTTPError('429 Too Many Requests')
        mock_post.return_value = resp_429

        assert client.embed(['x']) == []
        # 1 次初始请求 + 3 次指数退避重试，最后一次 429 走 HTTPError 分支的 continue
        assert mock_post.call_count == 4
        assert mock_sleep.call_count == 3

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_api_embed_unknown_exception(self, mock_post):
        """非 requests 异常（如响应解析异常）记 error 并返回 []"""
        client = _make_api_client()
        mock_post.side_effect = ValueError('unexpected')

        assert client.embed(['x']) == []


# ============================================================================
# ApiEmbeddingClient —— 云 API rerank
# ============================================================================
class TestApiRerank:
    """ApiEmbeddingClient.rerank 行为测试"""

    @pytest.mark.unit
    def test_api_rerank_no_docs(self):
        """空 docs 或空 query 直接返回 []"""
        client = _make_api_client()
        with patch('apps.llm.embedding.requests.post') as mock_post:
            assert client.rerank('q', []) == []
            mock_post.assert_not_called()

    @pytest.mark.unit
    def test_api_rerank_no_api_key(self):
        """api_key 为空时返回 []，避免发无效请求"""
        client = _make_api_client(api_key='')
        with patch('apps.llm.embedding.requests.post') as mock_post:
            assert client.rerank('q', ['d1']) == []
            mock_post.assert_not_called()

    @pytest.mark.unit
    def test_api_rerank_no_config(self):
        """base_url 或 rerank_model 未配置时提前降级返回 []"""
        # rerank_model 为空 -> _resolve_rerank_params 早返回 ('', '')
        client = _make_api_client(rerank_model='')
        with patch('apps.llm.embedding.requests.post') as mock_post:
            assert client.rerank('q', ['d1']) == []
            mock_post.assert_not_called()

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_api_rerank_success(self, mock_post):
        """正常响应：从 results[] 解析 index 与 relevance_score"""
        client = _make_api_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'results': [
                {'index': 1, 'relevance_score': 0.9},
                {'index': 0, 'relevance_score': 0.7},
            ]
        }
        mock_post.return_value = mock_resp

        hits = client.rerank('query', ['d0', 'd1'], top_k=2)

        assert hits == [
            {'index': 1, 'score': 0.9},
            {'index': 0, 'score': 0.7},
        ]

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_api_rerank_error(self, mock_post):
        """rerank 请求异常时返回 []（不向上抛出，调用方按空结果降级）"""
        client = _make_api_client()
        mock_post.side_effect = requests.exceptions.ConnectionError('down')

        assert client.rerank('q', ['d1']) == []


# ============================================================================
# DockerEmbeddingClient —— Docker embed
# ============================================================================
class TestDockerEmbed:
    """DockerEmbeddingClient.embed 行为与响应格式分支测试"""

    @pytest.mark.unit
    def test_docker_embed_empty(self):
        """空文本列表直接返回 []"""
        client = _make_docker_client()
        with patch('apps.llm.embedding.requests.post') as mock_post:
            assert client.embed([]) == []
            mock_post.assert_not_called()

    @pytest.mark.unit
    def test_docker_embed_no_url(self):
        """docker_url 为空时返回 []，不发请求"""
        client = _make_docker_client(url='')
        with patch('apps.llm.embedding.requests.post') as mock_post:
            assert client.embed(['x']) == []
            mock_post.assert_not_called()

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_docker_embed_success(self, mock_post):
        """正常响应（data[].embedding 格式）解析为向量列表"""
        client = _make_docker_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'data': [{'embedding': [0.1, 0.2]}]}
        mock_post.return_value = mock_resp

        assert client.embed(['x']) == [[0.1, 0.2]]

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_docker_embed_list_response(self, mock_post):
        """响应为 list 时直接 extend 进结果集"""
        client = _make_docker_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [[0.1, 0.2], [0.3, 0.4]]
        mock_post.return_value = mock_resp

        assert client.embed(['a', 'b']) == [[0.1, 0.2], [0.3, 0.4]]

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_docker_embed_dict_embeddings(self, mock_post):
        """响应 dict 含 'embeddings' key 时取该字段"""
        client = _make_docker_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'embeddings': [[0.1], [0.2]]}
        mock_post.return_value = mock_resp

        assert client.embed(['a', 'b']) == [[0.1], [0.2]]

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_docker_embed_dict_data(self, mock_post):
        """响应 dict 含 'data' key 时逐项取 item['embedding']"""
        client = _make_docker_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'data': [{'embedding': [0.1]}, {'embedding': [0.2]}]}
        mock_post.return_value = mock_resp

        assert client.embed(['a', 'b']) == [[0.1], [0.2]]

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_docker_embed_unknown_format(self, mock_post):
        """响应既非 list 也不含 embeddings/data 时记 warning 并返回 []"""
        client = _make_docker_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'unexpected': True}
        mock_post.return_value = mock_resp

        assert client.embed(['x']) == []

    @pytest.mark.unit
    @patch('apps.llm.embedding.requests.post')
    def test_docker_embed_request_exception(self, mock_post):
        """Docker 请求异常时返回 []（不向上抛出）"""
        client = _make_docker_client()
        mock_post.side_effect = requests.exceptions.ConnectionError('docker down')

        assert client.embed(['x']) == []


# ============================================================================
# EmbeddingClient —— 统一客户端优先级与兜底
# ============================================================================
class TestEmbeddingClientPriority:
    """EmbeddingClient 优先级、降级与全失败测试"""

    @pytest.mark.unit
    def test_embedding_client_docker_first(self):
        """provider=docker 时优先 Docker，成功则不再调用云 API"""
        client = _make_embedding_client(provider='docker')
        client._docker_client = MagicMock()
        client._docker_client.embed.return_value = [[0.1, 0.2]]
        client._api_client = MagicMock()

        assert client.embed(['x']) == [[0.1, 0.2]]
        client._docker_client.embed.assert_called_once()
        client._api_client.embed.assert_not_called()

    @pytest.mark.unit
    def test_embedding_client_api_first(self):
        """provider=api 时优先云 API，成功则不再调用 Docker"""
        client = _make_embedding_client(provider='api')
        client._api_client = MagicMock()
        client._api_client.embed.return_value = [[0.1]]
        client._docker_client = MagicMock()

        assert client.embed(['x']) == [[0.1]]
        client._api_client.embed.assert_called_once()
        client._docker_client.embed.assert_not_called()

    @pytest.mark.unit
    def test_embedding_client_fallback(self):
        """主路失败（返回空）时降级到备选路并返回其结果"""
        # provider=docker：docker 返回空 -> 降级到 api
        client = _make_embedding_client(provider='docker')
        client._docker_client = MagicMock()
        client._docker_client.embed.return_value = []  # 主路失败
        client._api_client = MagicMock()
        client._api_client.embed.return_value = [[0.5]]  # 备选路兜底

        assert client.embed(['x']) == [[0.5]]
        client._docker_client.embed.assert_called_once()
        client._api_client.embed.assert_called_once()

    @pytest.mark.unit
    def test_embedding_client_all_fail(self):
        """主备双路均失败时应抛 EmbeddingException，提示调用方不可用"""
        client = _make_embedding_client(provider='docker')
        client._docker_client = MagicMock()
        client._docker_client.embed.return_value = []
        client._api_client = MagicMock()
        client._api_client.embed.return_value = []

        with pytest.raises(EmbeddingException):
            client.embed(['x'])

    @pytest.mark.unit
    def test_embedding_client_api_first_docker_fallback(self):
        """provider=api：云 API 失败（返回空）时降级到 Docker 兜底并返回其结果"""
        client = _make_embedding_client(provider='api')
        client._api_client = MagicMock()
        client._api_client.embed.return_value = []  # 主路失败
        client._docker_client = MagicMock()
        client._docker_client.embed.return_value = [[0.5]]  # 兜底成功

        assert client.embed(['x']) == [[0.5]]
        client._api_client.embed.assert_called_once()
        client._docker_client.embed.assert_called_once()


# ============================================================================
# EmbeddingClient.rerank —— 统一客户端转发
# ============================================================================
class TestEmbeddingClientRerank:
    """EmbeddingClient.rerank 空入参拦截与转发测试"""

    @pytest.mark.unit
    def test_rerank_empty_docs(self):
        """docs 为空时直接返回 []，不调用 api_client"""
        client = _make_embedding_client(provider='api')
        client._api_client = MagicMock()

        assert client.rerank('q', []) == []
        client._api_client.rerank.assert_not_called()

    @pytest.mark.unit
    def test_rerank_delegates_to_api_client(self):
        """非空 docs 时透传给 api_client.rerank 并透传返回结果"""
        client = _make_embedding_client(provider='api')
        client._api_client = MagicMock()
        client._api_client.rerank.return_value = [{'index': 0, 'score': 0.9}]

        assert client.rerank('q', ['d1'], top_k=1) == [{'index': 0, 'score': 0.9}]
        client._api_client.rerank.assert_called_once_with('q', ['d1'], 1)


# ============================================================================
# embed_one —— 单条向量化
# ============================================================================
class TestEmbedOne:
    """embed_one 单条向量化与空结果回退测试"""

    @pytest.mark.unit
    def test_embed_one(self):
        """单条文本返回首个向量"""
        client = _make_embedding_client(provider='docker')
        client._docker_client = MagicMock()
        client._docker_client.embed.return_value = [[0.1, 0.2, 0.3]]

        assert client.embed_one('hello') == [0.1, 0.2, 0.3]
        # embed_one 内部以 [text] 调用 embed
        client._docker_client.embed.assert_called_once_with(['hello'], 32)

    @pytest.mark.unit
    def test_embed_one_empty(self):
        """embed 返回空时回退为零向量 [0.0]*dim

        这里直接 patch embed 方法返回 []，专门验证 `vecs[0] if vecs else [0.0]*self.dim`
        的兜底分支；真实 embed 在双路全失败时会抛异常，不会走到此处。
        """
        client = _make_embedding_client(provider='docker', dim=1024)
        with patch.object(client, 'embed', return_value=[]) as mock_embed:
            assert client.embed_one('hello') == [0.0] * 1024
            mock_embed.assert_called_once_with(['hello'])


# ============================================================================
# get_embedding_client —— 单例
# ============================================================================
class TestGetEmbeddingClient:
    """get_embedding_client 单例测试"""

    @pytest.mark.unit
    def test_get_embedding_client_singleton(self):
        """多次调用返回同一实例，EmbeddingClient 仅构造一次"""
        original = emb_mod._client
        emb_mod._client = None  # 重置模块级缓存，避免受其它测试污染
        try:
            with patch('apps.llm.embedding.EmbeddingClient') as mock_cls:
                c1 = get_embedding_client()
                c2 = get_embedding_client()
            assert c1 is c2
            assert mock_cls.call_count == 1
        finally:
            # 恢复缓存，避免污染后续测试
            emb_mod._client = original
