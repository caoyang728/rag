"""
apps.analytics.ragas_pipeline 单元测试 —— Ragas 部署前评估流水线

覆盖范围：
- 模块导入兼容：注入假 langchain_community 树，使顶层 _patch_ragas_langchain_compat 可执行
- _get_evaluator_llm：模型未配置抛 ValueError / base_url 反查 / 空 base_url 告警降级
- _get_evaluator_embeddings / _ProjectEmbeddings：embed_documents / embed_query 适配
- load_corpus_chunks：语料筛选（status=done / text 类型 / 最小长度）/ 无文档抛错
- generate_testset：TestsetGenerator 调用 / 无效样本过滤 / testset_id 生成
- save_testset / load_testset：JSON 持久化往返
- _get_eval_user：system 用户优先 / 超管兜底 / 无用户抛错
- run_rag_for_question：检索成功 / 检索失败 / 无上下文 / 生成失败
- _build_metrics：5 指标构造 + 旧版类名回退
- _evaluate_sync：全链路评估合并 / 全部检索失败 / scores_df 为 None
- _safe_score：NaN / None / 非法值处理
- generate_report：JSON + Markdown 报告生成与低分样本定位
- run_full_pipeline：全自动模式 / 复用测试集模式 / 空语料抛错

说明：测试环境未安装 ragas / langchain 系列包，本文件在导入被测试模块前向
sys.modules 注入假模块树（ragas.llms / ragas.metrics / ragas.testset /
ragas.dataset_schema / langchain_openai / langchain_core.documents /
langchain_community.*），函数内部局部导入因此可解析；所有外部对象用 MagicMock。
"""
import math
import re
import sys
import types
from datetime import datetime, timedelta

import pytest
from unittest.mock import patch, MagicMock

from django.utils import timezone

from apps.users.models import User


# ============================================================================
# 假模块树注入（必须在 import ragas_pipeline 之前执行）
# ============================================================================
def _make_module(name, **attrs):
    """构造一个假模块并注册到 sys.modules，返回模块对象"""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# langchain_community 树：顶层 _patch_ragas_langchain_compat() 需要
# import langchain_community.chat_models.vertexai 与
# from langchain_community.llms import VertexAI 均可解析（真实包未安装）
_make_module('langchain_community')
_make_module('langchain_community.chat_models', vertexai=None)
_make_module('langchain_community.chat_models.vertexai', ChatVertexAI=object)
_make_module('langchain_community.llms', VertexAI=object)

# langchain_openai / langchain_core：函数内局部导入用
_make_module('langchain_openai', ChatOpenAI=MagicMock(name='ChatOpenAI'))
_make_module('langchain_core')
_make_module('langchain_core.documents', Document=MagicMock(name='LCDocument'))

# ragas 树：函数内局部导入用
_make_module('ragas', evaluate=MagicMock(name='ragas_evaluate'))
_make_module('ragas.llms', LangchainLLMWrapper=MagicMock(name='LangchainLLMWrapper'))
_make_module('ragas.embeddings',
             LangchainEmbeddingsWrapper=MagicMock(name='LangchainEmbeddingsWrapper'))
_make_module('ragas.testset', TestsetGenerator=MagicMock(name='TestsetGenerator'))
_make_module('ragas.metrics',
             Faithfulness=MagicMock(name='Faithfulness'),
             AnswerRelevancy=MagicMock(name='AnswerRelevancy'),
             AnswerCorrectness=MagicMock(name='AnswerCorrectness'),
             LLMContextPrecisionWithReference=MagicMock(name='LLMContextPrecisionWithReference'),
             LLMContextRecall=MagicMock(name='LLMContextRecall'),
             ContextPrecision=MagicMock(name='ContextPrecision'),
             ContextRecall=MagicMock(name='ContextRecall'))
_make_module('ragas.dataset_schema',
             EvaluationDataset=MagicMock(name='EvaluationDataset'),
             SingleTurnSample=MagicMock(name='SingleTurnSample'))
_make_module('ragas.evaluate', evaluate=MagicMock(name='ragas_evaluate'))

# 假模块注入完成后才导入被测试模块（顶层兼容补丁需要上述模块可解析）
from apps.analytics import ragas_pipeline  # noqa: E402
from apps.knowledge.models import KnowledgeNode, Document as KDocument, DocumentChunk  # noqa: E402

TS_ID_RE = re.compile(r'^\d{8}_\d{6}_[0-9a-f]{6}$')


class _FakeILoc:
    """模拟 pandas DataFrame.iloc：按行号返回 dict 行"""

    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, i):
        return self._rows[i]


class _FakeScoresDF:
    """模拟 Ragas 评估结果的 to_pandas() DataFrame（pandas 未安装，自建最小实现）"""

    def __init__(self, rows, columns):
        self._rows = rows
        self.columns = columns
        self.iloc = _FakeILoc(rows)

    def __len__(self):
        return len(self._rows)


class _FakeTestsetDF:
    """模拟测试集 to_pandas()：iterrows 逐行产出 {字段: 值}"""

    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        for i, row in enumerate(self._rows):
            yield i, row


class RagasDBTestBase:
    """ragas 流水线 DB 测试公共 fixture（用户/节点/文档/切片）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/节点/文档"""
        self.user = User.objects.create_user(
            username='ragas_user', password='pass12345', email='ragas@test.com')
        self.node = KnowledgeNode.objects.create(
            name='ragas_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.doc = KDocument.objects.create(
            node=self.node, owner=self.user, title='语料文档',
            file_name='c.txt', file_type='txt', file_hash='h3',
            root_type='test_root', status='done', is_deleted=False, dept_id=1)

    def _chunk(self, content, chunk_index=0, chunk_type='text', content_length=None):
        """创建切片（content_length 默认按内容长度）"""
        return DocumentChunk.objects.create(
            document=self.doc, chunk_index=chunk_index, chunk_type=chunk_type,
            content=content,
            content_length=len(content) if content_length is None else content_length)


# ============================================================================
# _get_evaluator_llm —— Ragas 评估用 LLM 构建
# ============================================================================
class TestGetEvaluatorLLM:
    """评估 LLM 构建测试"""

    @pytest.mark.unit
    def test_missing_model_raises(self, settings):
        """SystemConfig.LLM_BASE_MODEL 未配置 → 抛 ValueError"""
        settings.LLM_API_KEY = 'sk-test'
        with patch('apps.system.config_loader.get_config_value', return_value=''):
            with pytest.raises(ValueError, match='LLM_BASE_MODEL'):
                ragas_pipeline._get_evaluator_llm()

    @pytest.mark.unit
    def test_constructed_with_base_url(self, settings):
        """配置齐全 → ChatOpenAI(DeepSeek 兼容) + LangchainLLMWrapper 包装"""
        settings.LLM_API_KEY = 'sk-test'
        fake_chat = MagicMock(name='chat')
        fake_wrapper = MagicMock(name='wrapper')
        with patch('apps.system.config_loader.get_config_value',
                   return_value='deepseek-chat'), \
             patch('apps.system.config_loader.get_llm_model_config',
                   return_value={'base_url': 'http://llm:8000/v1'}), \
             patch('langchain_openai.ChatOpenAI', return_value=fake_chat) as mock_cls, \
             patch('ragas.llms.LangchainLLMWrapper', return_value=fake_wrapper) as mock_wrap:
            result = ragas_pipeline._get_evaluator_llm()
        assert result is fake_wrapper
        mock_cls.assert_called_once_with(
            api_key='sk-test', base_url='http://llm:8000/v1',
            model='deepseek-chat', temperature=0, timeout=120)
        mock_wrap.assert_called_once_with(fake_chat)

    @pytest.mark.unit
    def test_missing_llm_row_base_url_empty(self, settings):
        """LLMModel 表未配置 base_url → 告警并降级为空 base_url"""
        settings.LLM_API_KEY = 'sk-test'
        fake_chat = MagicMock(name='chat')
        with patch('apps.system.config_loader.get_config_value',
                   return_value='deepseek-chat'), \
             patch('apps.system.config_loader.get_llm_model_config', return_value=None), \
             patch('langchain_openai.ChatOpenAI', return_value=fake_chat) as mock_cls, \
             patch('ragas.llms.LangchainLLMWrapper', return_value=MagicMock()):
            ragas_pipeline._get_evaluator_llm()
        _, kwargs = mock_cls.call_args
        assert kwargs['base_url'] == ''

    @pytest.mark.unit
    def test_explicit_model_overrides(self, settings):
        """显式传入 model 优先，不再读 SystemConfig"""
        settings.LLM_API_KEY = 'sk-test'
        with patch('apps.system.config_loader.get_config_value') as mock_cfg, \
             patch('apps.system.config_loader.get_llm_model_config', return_value=None), \
             patch('langchain_openai.ChatOpenAI') as mock_cls, \
             patch('ragas.llms.LangchainLLMWrapper', return_value=MagicMock()):
            ragas_pipeline._get_evaluator_llm(model='custom-model')
        mock_cfg.assert_not_called()
        _, kwargs = mock_cls.call_args
        assert kwargs['model'] == 'custom-model'


# ============================================================================
# _get_evaluator_embeddings / _ProjectEmbeddings —— Embedding 适配
# ============================================================================
class TestGetEvaluatorEmbeddings:
    """评估 Embedding 构建测试"""

    @pytest.mark.unit
    def test_wraps_project_embeddings(self):
        """用 LangchainEmbeddingsWrapper 包装项目 Embedding 客户端"""
        fake_wrapper = MagicMock(name='emb_wrapper')
        with patch('ragas.embeddings.LangchainEmbeddingsWrapper',
                   return_value=fake_wrapper) as mock_wrap:
            result = ragas_pipeline._get_evaluator_embeddings()
        assert result is fake_wrapper
        # 传入的是 _ProjectEmbeddings 实例（适配层）
        instance = mock_wrap.call_args[0][0]
        assert isinstance(instance, ragas_pipeline._ProjectEmbeddings)


class TestProjectEmbeddings:
    """项目 Embedding 客户端 → LangChain Embeddings 接口适配"""

    @pytest.mark.unit
    def test_embed_documents(self):
        """embed_documents 委托给客户端 embed 批量向量化"""
        fake_client = MagicMock()
        fake_client.embed.return_value = [[0.1, 0.2]]
        with patch('apps.llm.embedding.get_embedding_client', return_value=fake_client):
            emb = ragas_pipeline._ProjectEmbeddings()
        assert emb.embed_documents(['文本']) == [[0.1, 0.2]]
        fake_client.embed.assert_called_once_with(['文本'])

    @pytest.mark.unit
    def test_embed_query(self):
        """embed_query 委托给客户端 embed_one 单条向量化"""
        fake_client = MagicMock()
        fake_client.embed_one.return_value = [0.3, 0.4]
        with patch('apps.llm.embedding.get_embedding_client', return_value=fake_client):
            emb = ragas_pipeline._ProjectEmbeddings()
        assert emb.embed_query('问题') == [0.3, 0.4]
        fake_client.embed_one.assert_called_once_with('问题')


# ============================================================================
# load_corpus_chunks —— 从知识库加载语料
# ============================================================================
@pytest.mark.django_db
class TestLoadCorpusChunks(RagasDBTestBase):
    """语料加载测试（知识 Document 用假 queryset，切片用真实 DB）"""

    def _fake_doc_model(self, doc_ids):
        """构造链式假 Document 模型：filter→order_by→切片→values_list"""
        fake_model = MagicMock(name='KDocument')
        filtered = MagicMock(name='filtered')
        ordered = MagicMock(name='ordered')
        fake_model.objects.filter.return_value = filtered
        filtered.order_by.return_value = ordered
        # 切片返回自身，避免 values_list 配置丢失
        ordered.__getitem__.return_value = ordered
        ordered.values_list.return_value = doc_ids
        ordered.filter.return_value = ordered
        return fake_model

    def test_loads_text_chunks_only(self):
        """只加载 status=done 文档的 text 切片且满足最小长度，metadata 完整"""
        ok = self._chunk('有效内容' * 50, chunk_index=0, content_length=200)
        self._chunk('短' * 30, chunk_index=1, content_length=60)      # 过短 → 过滤
        self._chunk('图片内容' * 50, chunk_index=2, chunk_type='image',
                    content_length=200)                               # 非 text → 过滤

        fake_model = self._fake_doc_model([self.doc.id])
        with patch('apps.knowledge.models.Document', fake_model):
            docs = ragas_pipeline.load_corpus_chunks(limit_docs=50, min_chunk_chars=80)

        assert len(docs) == 1
        # 本地 Document 名被 apps.knowledge.models.Document 遮蔽 → 返回假模型实例
        fake_model.assert_called_once()
        _, kwargs = fake_model.call_args
        assert kwargs['page_content'] == ok.content
        assert kwargs['metadata']['doc_id'] == self.doc.id
        assert kwargs['metadata']['filename'] == f'doc_{self.doc.id}_语料文档'
        assert kwargs['metadata']['section_path'] == ''

    def test_root_type_filter_applied(self):
        """指定 root_type → 传给文档过滤"""
        self._chunk('内容' * 50, content_length=200)
        fake_model = self._fake_doc_model([self.doc.id])
        with patch('apps.knowledge.models.Document', fake_model):
            ragas_pipeline.load_corpus_chunks(root_type='hr', limit_docs=10)
        fake_model.objects.filter.assert_called_once_with(
            status='done', is_deleted=False)
        fake_model.objects.filter.return_value.order_by.return_value.filter \
            .assert_called_once_with(root_type='hr')

    def test_no_docs_raises(self):
        """无 status=done 文档 → 抛 ValueError 提示先完成解析"""
        fake_model = self._fake_doc_model([])
        with patch('apps.knowledge.models.Document', fake_model):
            with pytest.raises(ValueError, match='status=done'):
                ragas_pipeline.load_corpus_chunks()


# ============================================================================
# generate_testset —— 测试集自动生成
# ============================================================================
class TestGenerateTestset:
    """测试集生成测试"""

    def _fake_generator(self, rows):
        """构造假 TestsetGenerator：generate_with_langchain_docs 返回假测试集"""
        gen_cls = MagicMock(name='TestsetGenerator')
        gen_instance = gen_cls.return_value
        fake_testset = MagicMock(name='testset')
        fake_testset.to_pandas.return_value = _FakeTestsetDF(rows)
        gen_instance.generate_with_langchain_docs.return_value = fake_testset
        return gen_cls

    @pytest.mark.unit
    def test_generates_samples_and_filters_invalid(self):
        """生成样本并过滤问题/参考答案为空的无效行"""
        rows = [
            {'user_input': '什么是社保', 'reference': '社保是国家强制性保险',
             'retrieved_contexts': ['片段1', '片段2']},
            {'user_input': '', 'reference': '空问题', 'retrieved_contexts': []},
            {'user_input': '无参考', 'reference': '', 'retrieved_contexts': []},
        ]
        gen_cls = self._fake_generator(rows)
        with patch('apps.analytics.ragas_pipeline._get_evaluator_llm',
                   return_value='fake-llm'), \
             patch('apps.analytics.ragas_pipeline._get_evaluator_embeddings',
                   return_value='fake-emb'), \
             patch('ragas.testset.TestsetGenerator', gen_cls):
            samples, testset_id = ragas_pipeline.generate_testset(
                corpus_docs=['doc1'], testset_size=3, model='deepseek-chat')

        assert len(samples) == 1
        assert samples[0]['user_input'] == '什么是社保'
        assert samples[0]['reference'] == '社保是国家强制性保险'
        assert samples[0]['retrieved_contexts'] == ['片段1', '片段2']
        assert TS_ID_RE.match(testset_id)
        # 生成器按语料 + 目标数量调用
        gen_cls.assert_called_once_with(llm='fake-llm', embedding_model='fake-emb')
        gen_cls.return_value.generate_with_langchain_docs.assert_called_once_with(
            ['doc1'], testset_size=3)

    @pytest.mark.unit
    def test_legacy_column_names(self):
        """老版本列名（question / ground_truth / contexts）兼容"""
        rows = [{'question': '旧列问题', 'ground_truth': '旧列参考', 'contexts': ['c']}]
        gen_cls = self._fake_generator(rows)
        with patch('apps.analytics.ragas_pipeline._get_evaluator_llm', return_value='llm'), \
             patch('apps.analytics.ragas_pipeline._get_evaluator_embeddings', return_value='e'), \
             patch('ragas.testset.TestsetGenerator', gen_cls):
            samples, _ = ragas_pipeline.generate_testset(corpus_docs=['d'], testset_size=1)
        assert samples[0]['user_input'] == '旧列问题'
        assert samples[0]['reference'] == '旧列参考'
        assert samples[0]['retrieved_contexts'] == ['c']


# ============================================================================
# save_testset / load_testset —— 测试集持久化
# ============================================================================
class TestSaveLoadTestset:
    """测试集 JSON 持久化测试"""

    @pytest.mark.unit
    def test_save_and_load_roundtrip(self, tmp_path):
        """保存后可从 JSON 完整读回（ensure_ascii=False 保留中文）"""
        samples = [{'user_input': '问题', 'reference': '参考', 'retrieved_contexts': ['c']}]
        path = ragas_pipeline.save_testset(samples, '20260101_000000_abc123', str(tmp_path))
        assert path == str(tmp_path / 'testset_20260101_000000_abc123.json')
        loaded = ragas_pipeline.load_testset(path)
        assert loaded == samples

    @pytest.mark.unit
    def test_save_creates_dir(self, tmp_path):
        """输出目录不存在时自动创建"""
        nested = tmp_path / 'a' / 'b'
        path = ragas_pipeline.save_testset([], 'ts1', str(nested))
        assert nested.exists()
        assert path.endswith('testset_ts1.json')


# ============================================================================
# _get_eval_user —— 评估用系统用户
# ============================================================================
@pytest.mark.django_db
class TestGetEvalUser:
    """评估用户选择测试"""

    def test_no_user_raises(self):
        """无 system 用户也无超管 → 抛 ValueError"""
        with pytest.raises(ValueError, match='system'):
            ragas_pipeline._get_eval_user()

    def test_system_user_preferred(self):
        """username='system' 优先于超管"""
        # create_superuser 自动绑定 super_admin 角色（自定义 User 无 is_superuser 字段）
        User.objects.create_superuser(
            username='su_backup', password='pass12345', email='su@test.com')
        sys_user = User.objects.create_user(
            username='system', password='pass12345', email='sys@test.com')
        assert ragas_pipeline._get_eval_user() == sys_user

    def test_superuser_fallback(self):
        """无 system 用户 → 回退到超管"""
        su = User.objects.create_superuser(
            username='su_only', password='pass12345', email='su2@test.com')
        assert ragas_pipeline._get_eval_user() == su


# ============================================================================
# run_rag_for_question —— 单问题全链路 RAG
# ============================================================================
class TestRunRagForQuestion:
    """单问题检索+生成测试（user 用普通对象，检索/LLM 全 mock）"""

    @pytest.mark.unit
    def test_success(self):
        """检索成功 → 上下文截断 500 字、回答来自 LLM、统计透传"""
        with patch('apps.retrieval.hybrid.hybrid_search', return_value={
                'chunks': [{'content': '甲' * 600}, {'content': ''}, {'content': '乙'}],
                'stats': {'hits': 2}}), \
             patch('apps.llm.factory.get_llm') as mock_get:
            llm = MagicMock(name='llm')
            llm.chat.return_value = {'content': '回答内容'}
            mock_get.return_value = llm
            result = ragas_pipeline.run_rag_for_question('问题', user='u')
        assert result['answer'] == '回答内容'
        assert result['error'] is None
        assert result['retrieval_stats'] == {'hits': 2}
        # 空 content 跳过；600 字截断到 500；短内容原样保留（不截断）
        assert len(result['contexts']) == 2
        assert len(result['contexts'][0]) == 500
        assert result['contexts'][1] == '乙'
        mock_get.assert_called_once_with(None)
        assert llm.chat.call_args.kwargs['temperature'] == 0.3

    @pytest.mark.unit
    def test_retrieval_failure(self):
        """检索抛异常 → 返回 retrieval_failed 错误，不调用 LLM"""
        with patch('apps.retrieval.hybrid.hybrid_search',
                   side_effect=RuntimeError('retrieval down')), \
             patch('apps.llm.factory.get_llm') as mock_get:
            result = ragas_pipeline.run_rag_for_question('问题', user='u')
        assert result['answer'] == ''
        assert result['error'].startswith('retrieval_failed:')
        mock_get.assert_not_called()

    @pytest.mark.unit
    def test_no_contexts_placeholder(self):
        """检索无相关内容 → 返回占位回答，不调用 LLM"""
        with patch('apps.retrieval.hybrid.hybrid_search',
                   return_value={'chunks': [{'content': ''}]}), \
             patch('apps.llm.factory.get_llm') as mock_get:
            result = ragas_pipeline.run_rag_for_question('问题', user='u')
        assert result['answer'] == '（检索未返回相关内容，无法回答）'
        assert result['contexts'] == []
        mock_get.assert_not_called()

    @pytest.mark.unit
    def test_generation_failure(self):
        """LLM 生成失败 → 返回 generation_failed 错误与失败占位回答"""
        with patch('apps.retrieval.hybrid.hybrid_search',
                   return_value={'chunks': [{'content': '上下文'}]}), \
             patch('apps.llm.factory.get_llm') as mock_get:
            llm = MagicMock(name='llm')
            llm.chat.side_effect = RuntimeError('llm down')
            mock_get.return_value = llm
            result = ragas_pipeline.run_rag_for_question('问题', user='u')
        assert result['error'].startswith('generation_failed:')
        assert result['answer'].startswith('[回答生成失败]')


# ============================================================================
# _build_metrics —— 评估指标集合
# ============================================================================
class TestBuildMetrics:
    """指标构造测试"""

    @pytest.mark.unit
    def test_builds_five_metrics(self):
        """构造 5 个指标：Faithfulness / AnswerRelevancy / AnswerCorrectness /
        LLMContextPrecisionWithReference / LLMContextRecall，参数正确"""
        llm, emb = MagicMock(), MagicMock()
        with patch('ragas.metrics.Faithfulness') as m1, \
             patch('ragas.metrics.AnswerRelevancy') as m2, \
             patch('ragas.metrics.AnswerCorrectness') as m3, \
             patch('ragas.metrics.LLMContextPrecisionWithReference') as m4, \
             patch('ragas.metrics.LLMContextRecall') as m5:
            metrics = ragas_pipeline._build_metrics(llm, emb)
        assert len(metrics) == 5
        m1.assert_called_once_with(llm=llm)
        m2.assert_called_once_with(llm=llm, embeddings=emb)
        m3.assert_called_once_with(llm=llm, embeddings=emb)
        m4.assert_called_once_with(llm=llm)
        m5.assert_called_once_with(llm=llm)

    @pytest.mark.unit
    def test_fallback_to_legacy_names(self):
        """老版本 ragas 无 WithReference 类名 → 回退 ContextPrecision/ContextRecall"""
        metrics_mod = sys.modules['ragas.metrics']
        saved = {name: getattr(metrics_mod, name)
                 for name in ('LLMContextPrecisionWithReference', 'LLMContextRecall')}
        try:
            delattr(metrics_mod, 'LLMContextPrecisionWithReference')
            delattr(metrics_mod, 'LLMContextRecall')
            llm, emb = MagicMock(), MagicMock()
            with patch('ragas.metrics.Faithfulness'), \
                 patch('ragas.metrics.AnswerRelevancy'), \
                 patch('ragas.metrics.AnswerCorrectness'), \
                 patch('ragas.metrics.ContextPrecision') as m4, \
                 patch('ragas.metrics.ContextRecall') as m5:
                metrics = ragas_pipeline._build_metrics(llm, emb)
            assert len(metrics) == 5
            m4.assert_called_once_with(llm=llm)
            m5.assert_called_once_with(llm=llm)
        finally:
            # 恢复假模块属性，避免影响其他用例
            setattr(metrics_mod, 'LLMContextPrecisionWithReference',
                    saved['LLMContextPrecisionWithReference'])
            setattr(metrics_mod, 'LLMContextRecall', saved['LLMContextRecall'])


# ============================================================================
# _evaluate_sync —— 同步执行 Ragas 评估
# ============================================================================
class TestEvaluateSync:
    """同步评估合并测试"""

    SAMPLES = [
        {'user_input': 'q1', 'reference': 'r1'},
        {'user_input': 'q2', 'reference': 'r2'},
    ]

    def _rag_ok(self, idx):
        """构造第 idx 条样本的检索+回答结果"""
        return {'answer': f'a{idx}', 'contexts': [f'c{idx}'],
                'error': None, 'retrieval_stats': {}}

    @pytest.mark.unit
    def test_success_merges_scores(self):
        """评估成功 → 各指标分数合并回 enriched"""
        rag_results = [self._rag_ok(1), self._rag_ok(2)]
        eval_result = MagicMock(name='eval_result')
        eval_result.to_pandas.return_value = _FakeScoresDF(
            rows=[{'faithfulness': 0.8, 'answer_relevancy': 0.9},
                  {'faithfulness': 0.7, 'answer_relevancy': 0.6}],
            columns=['faithfulness', 'answer_relevancy'])

        with patch('apps.analytics.ragas_pipeline.run_rag_for_question',
                   side_effect=rag_results), \
             patch('apps.analytics.ragas_pipeline._get_eval_user',
                   return_value='sys_user'), \
             patch('apps.analytics.ragas_pipeline._build_metrics',
                   return_value=['m1', 'm2']), \
             patch('ragas.dataset_schema.SingleTurnSample') as mock_sample, \
             patch('ragas.dataset_schema.EvaluationDataset') as mock_ds, \
             patch('ragas.evaluate', return_value=eval_result) as mock_eval:
            enriched = ragas_pipeline._evaluate_sync(self.SAMPLES, 'llm', 'emb')

        assert len(enriched) == 2
        assert enriched[0]['response'] == 'a1'
        assert enriched[0]['retrieved_contexts'] == ['c1']
        assert enriched[0]['faithfulness'] == 0.8
        assert enriched[1]['answer_relevancy'] == 0.6
        # 评估调用：仅对检索成功的样本构造 SingleTurnSample
        assert mock_sample.call_count == 2
        mock_ds.assert_called_once()
        mock_eval.assert_called_once_with(
            mock_ds.return_value, metrics=['m1', 'm2'],
            show_progress=True, raise_exceptions=False)

    @pytest.mark.unit
    def test_all_retrieval_failed(self):
        """全部检索失败 → 不执行 Ragas 评估，返回带错误标记的样本"""
        rag_results = [
            {'answer': '', 'contexts': [], 'error': 'retrieval_failed: x',
             'retrieval_stats': {}},
            {'answer': '', 'contexts': [], 'error': None, 'retrieval_stats': {}},
        ]
        with patch('apps.analytics.ragas_pipeline.run_rag_for_question',
                   side_effect=rag_results), \
             patch('apps.analytics.ragas_pipeline._get_eval_user',
                   return_value='sys_user'), \
             patch('ragas.evaluate') as mock_eval:
            enriched = ragas_pipeline._evaluate_sync(self.SAMPLES, 'llm', 'emb')
        assert len(enriched) == 2
        assert enriched[0]['_rag_error'] == 'retrieval_failed: x'
        assert 'faithfulness' not in enriched[0]
        mock_eval.assert_not_called()

    @pytest.mark.unit
    def test_scores_df_none_skips_merge(self):
        """result.to_pandas() 抛异常 → 跳过分数合并，样本仍返回"""
        rag_results = [self._rag_ok(1)]
        eval_result = MagicMock(name='eval_result')
        eval_result.to_pandas.side_effect = RuntimeError('pandas broken')
        with patch('apps.analytics.ragas_pipeline.run_rag_for_question',
                   side_effect=rag_results), \
             patch('apps.analytics.ragas_pipeline._get_eval_user',
                   return_value='sys_user'), \
             patch('apps.analytics.ragas_pipeline._build_metrics',
                   return_value=['m1']), \
             patch('ragas.dataset_schema.SingleTurnSample'), \
             patch('ragas.dataset_schema.EvaluationDataset'), \
             patch('ragas.evaluate', return_value=eval_result) as mock_eval:
            enriched = ragas_pipeline._evaluate_sync(self.SAMPLES[:1], 'llm', 'emb')
        assert enriched[0]['response'] == 'a1'
        assert 'faithfulness' not in enriched[0]
        mock_eval.assert_called_once()


# ============================================================================
# _safe_score —— 分数安全提取
# ============================================================================
class TestSafeScore:
    """分数提取测试（NaN / None / 非法值）"""

    @pytest.mark.unit
    def test_values(self):
        """正常值四舍五入到 4 位小数"""
        assert ragas_pipeline._safe_score(0.81234) == 0.8123
        assert ragas_pipeline._safe_score(1.0) == 1.0

    @pytest.mark.unit
    def test_none_and_nan(self):
        """None 与 NaN → None（不参与均值计算）"""
        assert ragas_pipeline._safe_score(None) is None
        assert ragas_pipeline._safe_score(float('nan')) is None

    @pytest.mark.unit
    def test_invalid_returns_none(self):
        """非法类型 → None"""
        assert ragas_pipeline._safe_score('not-a-number') is None
        assert ragas_pipeline._safe_score(object()) is None


# ============================================================================
# generate_report —— 评估报告生成
# ============================================================================
class TestGenerateReport:
    """JSON + Markdown 报告生成测试"""

    def _enriched(self):
        """两条样本：一条高分、一条低分（带 RAG 错误）"""
        return [
            {'user_input': '好问题', 'response': '好回答', 'reference': '好参考',
             'faithfulness': 0.9, 'answer_relevancy': 0.8,
             'context_precision': 0.7, 'context_recall': 0.6,
             'answer_correctness': 0.5},
            {'user_input': '低分问题', 'response': '差回答', 'reference': '参考',
             'faithfulness': 0.1, 'answer_relevancy': 0.2,
             '_rag_error': 'retrieval_failed: x'},
        ]

    @pytest.mark.unit
    def test_generates_json_and_markdown(self, tmp_path):
        """生成 JSON 明细 + Markdown 摘要，低分样本进入人工排查区"""
        import json
        paths = ragas_pipeline.generate_report(
            self._enriched(), 'ts_report', str(tmp_path), meta={'model': 'm'})
        assert paths['json'].endswith('report_ts_report.json')
        assert paths['markdown'].endswith('report_ts_report.md')

        with open(paths['json'], encoding='utf-8') as f:
            data = json.load(f)
        assert data['testset_id'] == 'ts_report'
        assert data['meta'] == {'model': 'm'}
        assert data['summary']['total_samples'] == 2
        assert data['summary']['rag_errors'] == 1
        assert data['summary']['metrics']['faithfulness'] == pytest.approx(0.5)
        assert len(data['samples']) == 2

        with open(paths['markdown'], encoding='utf-8') as f:
            md = f.read()
        assert 'Ragas 评估报告 ts_report' in md
        assert '低分问题' in md          # 低分样本定位
        assert 'RAG错误' in md

    @pytest.mark.unit
    def test_all_metrics_none(self, tmp_path):
        """全部指标为空 → 均值 None，Markdown 显示 N/A，文件仍生成"""
        enriched = [{'user_input': 'q', 'response': 'a', 'reference': 'r'}]
        paths = ragas_pipeline.generate_report(enriched, 'ts_none', str(tmp_path))
        with open(paths['markdown'], encoding='utf-8') as f:
            md = f.read()
        assert 'N/A' in md
        assert '低分样本' in md  # 低分区正常渲染


# ============================================================================
# run_full_pipeline —— 全自动评估流水线入口
# ============================================================================
class TestRunFullPipeline:
    """流水线入口测试"""

    SAMPLES = [{'user_input': 'q', 'reference': 'r', 'retrieved_contexts': ['c']}]
    ENRICHED = [{'user_input': 'q', 'response': 'a', 'reference': 'r',
                 'faithfulness': 0.9}]

    def _mocks(self):
        """全自动模式所需 mock 集合"""
        return [
            patch('apps.analytics.ragas_pipeline._get_evaluator_llm',
                  return_value='llm'),
            patch('apps.analytics.ragas_pipeline._get_evaluator_embeddings',
                  return_value='emb'),
            patch('apps.analytics.ragas_pipeline._evaluate_sync',
                  return_value=self.ENRICHED),
            patch('apps.analytics.ragas_pipeline.generate_report',
                  return_value={'json': 'r.json', 'markdown': 'r.md'}),
        ]

    @pytest.mark.unit
    def test_full_auto_mode(self, tmp_path):
        """全自动：加载语料 → 生成测试集 → 保存 → 评估 → 报告"""
        mocks = self._mocks()
        with mocks[0], mocks[1], mocks[2], mocks[3], \
             patch('apps.analytics.ragas_pipeline.load_corpus_chunks',
                   return_value=['doc1']), \
             patch('apps.analytics.ragas_pipeline.generate_testset',
                   return_value=(self.SAMPLES, 'ts_auto')), \
             patch('apps.analytics.ragas_pipeline.save_testset',
                   return_value='/tmp/x.json') as mock_save:
            result = ragas_pipeline.run_full_pipeline(
                testset_size=5, limit_docs=10, root_type='hr', model='m',
                output_dir=str(tmp_path))
        assert result['testset_id'] == 'ts_auto'
        assert result['samples'] == self.ENRICHED
        assert result['report_paths'] == {'json': 'r.json', 'markdown': 'r.md'}
        assert result['summary']['faithfulness'] == 0.9
        mock_save.assert_called_once_with(self.SAMPLES, 'ts_auto', str(tmp_path))

    @pytest.mark.unit
    def test_reuse_samples_mode(self, tmp_path):
        """复用测试集：跳过语料加载 / 生成 / 保存，直接评估"""
        mocks = self._mocks()
        with mocks[0], mocks[1], mocks[2], mocks[3], \
             patch('apps.analytics.ragas_pipeline.load_corpus_chunks') as mock_load, \
             patch('apps.analytics.ragas_pipeline.generate_testset') as mock_gen, \
             patch('apps.analytics.ragas_pipeline.save_testset') as mock_save:
            result = ragas_pipeline.run_full_pipeline(
                samples=self.SAMPLES, output_dir=str(tmp_path))
        assert '_reuse_' in result['testset_id']
        mock_load.assert_not_called()
        mock_gen.assert_not_called()
        mock_save.assert_not_called()

    @pytest.mark.unit
    def test_empty_corpus_raises(self, tmp_path):
        """语料为空 → 抛 ValueError 中断"""
        with patch('apps.analytics.ragas_pipeline.load_corpus_chunks',
                   return_value=[]):
            with pytest.raises(ValueError, match='语料为空'):
                ragas_pipeline.run_full_pipeline(output_dir=str(tmp_path))
