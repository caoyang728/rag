"""
apps.analytics.services.wiki_eval_service 测试 —— Wiki 页面质量评估（LLM-as-Judge）

覆盖范围：
- build_wiki_source_chunks：node 挂载型收集源切片（截断/上限）、community 型返回空
- evaluate_wiki_page 跳过分支：页面不存在 / community 型 / 无源切片 / 正文为空
- evaluate_wiki_page 成功：两个维度 measure 后落 WikiPageQualityScore 记录
- 维度评估失败：failed 记录落库，失败维度进入 failed 列表

用 pytest-django + fake deepeval 模块（sys.modules 注入）：
evaluate_wiki_page 内部从 deepeval 导入指标，测试注入假模块避免依赖真实
deepeval/LLM 服务；其余（get_deepeval_model / AnalyticsConfig）直接 patch。
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from apps.analytics.models import WikiPageQualityScore
from apps.analytics.services.wiki_eval_service import (
    build_wiki_source_chunks, evaluate_wiki_page, MAX_SOURCE_CHUNKS, MAX_CHUNK_CHARS,
)
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk
from apps.users.models import User
from apps.wiki.models import WikiPage


def _install_deepeval(monkeypatch):
    """向 sys.modules 注入 fake deepeval，返回可配置的指标类工厂"""
    metrics_mod = types.ModuleType('deepeval.metrics')
    metrics_mod.FaithfulnessMetric = MagicMock()
    metrics_mod.GEval = MagicMock()
    test_case_mod = types.ModuleType('deepeval.test_case')
    test_case_mod.LLMTestCase = MagicMock
    # 实例化 mock：代码里访问 SingleTurnParams.INPUT 等枚举属性
    test_case_mod.SingleTurnParams = MagicMock()
    monkeypatch.setitem(sys.modules, 'deepeval.metrics', metrics_mod)
    monkeypatch.setitem(sys.modules, 'deepeval.test_case', test_case_mod)
    monkeypatch.setitem(sys.modules, 'deepeval', types.ModuleType('deepeval'))
    return metrics_mod


def _make_env():
    """构造：用户 → 节点 → 文档 → 切片，返回 (node, doc)"""
    user = User.objects.create_user(
        username='wiki_user', password='x', email='wiki@test.com')
    node = KnowledgeNode.objects.create(
        name='Wiki 节点', root_type='company_doc', node_type='folder',
        node_kind='FOLDER', node_level=4, depth=0, path='/0002/',
        created_by=user)
    doc = Document.objects.create(
        node=node, title='源文档', file_name='s.txt', file_type='txt',
        file_size=10, file_hash='h2', file_path='/tmp/s.txt',
        mime_type='text/plain', owner=user, dept_id=1,
        visibility_level='PUBLIC', status='done')
    return user, node, doc


# ============================================================================
# build_wiki_source_chunks
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestBuildWikiSourceChunks:
    """源文档切片收集测试"""

    def test_community_page_returns_empty(self):
        """community 挂载型页面（node 为空）应返回空列表"""
        page = WikiPage.objects.create(title='社区页', content='x', status='published')
        assert build_wiki_source_chunks(page) == []

    def test_collects_and_truncates_chunks(self):
        """node 挂载型应收集切片并截断到 MAX_CHUNK_CHARS"""
        _, node, doc = _make_env()
        long_content = '长' * (MAX_CHUNK_CHARS + 100)
        DocumentChunk.objects.create(document=doc, chunk_index=0,
                                     chunk_type='text', content=long_content)
        page = WikiPage.objects.create(title='页', node=node, content='正文',
                                       status='published')

        chunks = build_wiki_source_chunks(page)

        assert len(chunks) == 1
        assert len(chunks[0]) == MAX_CHUNK_CHARS

    def test_chunk_limit_capped(self):
        """源切片数量应受 MAX_SOURCE_CHUNKS 上限约束（单文档最多取 10 片）"""
        _, node, doc = _make_env()
        doc2 = Document.objects.create(
            node=node, title='源文档2', file_name='s2.txt', file_type='txt',
            file_size=10, file_hash='h3', file_path='/tmp/s2.txt',
            mime_type='text/plain', owner=doc.owner, dept_id=1,
            visibility_level='PUBLIC', status='done')
        for d in (doc, doc2):
            for i in range(12):
                DocumentChunk.objects.create(document=d, chunk_index=i,
                                             chunk_type='text', content=f'c{i}')
        page = WikiPage.objects.create(title='页', node=node, content='正文',
                                       status='published')

        chunks = build_wiki_source_chunks(page)

        assert len(chunks) == MAX_SOURCE_CHUNKS

    def test_skips_empty_chunk_content(self):
        """content 为空的切片应跳过"""
        _, node, doc = _make_env()
        DocumentChunk.objects.create(document=doc, chunk_index=0,
                                     chunk_type='text', content='')
        DocumentChunk.objects.create(document=doc, chunk_index=1,
                                     chunk_type='text', content='有效')
        page = WikiPage.objects.create(title='页', node=node, content='正文',
                                       status='published')

        chunks = build_wiki_source_chunks(page)

        assert chunks == ['有效']


# ============================================================================
# evaluate_wiki_page
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestEvaluateWikiPage:
    """Wiki 页面评估主流程测试（fake deepeval）"""

    @pytest.fixture(autouse=True)
    def _deepeval(self, monkeypatch):
        """注入 fake deepeval 模块并 patch 模型接入"""
        self._metrics_mod = _install_deepeval(monkeypatch)
        monkeypatch.setattr('apps.analytics.services.deepeval_service.get_deepeval_model',
                            MagicMock(return_value=MagicMock()))
        monkeypatch.setattr('rag_project.config.AnalyticsConfig.eval_model',
                            classmethod(lambda cls: 'test-model'))

    def _make_page_with_chunks(self):
        """构造带源切片和正文的 node 挂载页面"""
        _, node, doc = _make_env()
        DocumentChunk.objects.create(document=doc, chunk_index=0,
                                     chunk_type='text', content='源内容')
        return WikiPage.objects.create(title='评估页', node=node,
                                       content='页面正文', status='published')

    def test_page_not_found_skipped(self):
        """页面不存在返回 skipped=page_not_found"""
        result = evaluate_wiki_page(999999)
        assert result['skipped'] == 'page_not_found'
        assert result['ok'] is False

    def test_community_page_skipped(self):
        """community 型页面（无 node）应跳过评估"""
        page = WikiPage.objects.create(title='社区页', content='x', status='published')
        result = evaluate_wiki_page(page.id)
        assert result['skipped'] == 'community_page'

    def test_no_source_chunks_skipped(self):
        """无源切片的 node 页面应跳过"""
        _, node, _ = _make_env()
        page = WikiPage.objects.create(title='页', node=node, content='正文',
                                       status='published')
        result = evaluate_wiki_page(page.id)
        assert result['skipped'] == 'no_source_chunks'

    def test_empty_content_skipped(self):
        """正文为空应跳过评估"""
        _, node, doc = _make_env()
        DocumentChunk.objects.create(document=doc, chunk_index=0,
                                     chunk_type='text', content='源内容')
        page = WikiPage.objects.create(title='页', node=node, content='  ',
                                       status='published')
        result = evaluate_wiki_page(page.id)
        assert result['skipped'] == 'empty_content'

    def test_success_saves_scores(self):
        """两个维度评估成功应各自落库"""
        page = self._make_page_with_chunks()
        fm = MagicMock(); fm.score = 0.85; fm.reason = '忠实'; fm.measure = MagicMock()
        gm = MagicMock(); gm.score = 0.9; gm.reason = '完整'; gm.measure = MagicMock()
        self._metrics_mod.FaithfulnessMetric.return_value = fm
        self._metrics_mod.GEval.return_value = gm

        result = evaluate_wiki_page(page.id)

        assert result['ok'] is True
        assert result['evaluated'] == ['faithfulness', 'completeness']
        scores = WikiPageQualityScore.objects.filter(page=page)
        assert scores.count() == 2
        f = scores.get(dimension='faithfulness')
        assert f.score == 0.85
        assert f.status == 'completed'
        assert f.eval_model == 'deepeval-test-model'

    def test_metric_failure_saves_failed_record(self):
        """completeness 评估失败应落 failed 记录并进入 failed 列表"""
        page = self._make_page_with_chunks()
        fm = MagicMock(); fm.score = 0.85; fm.reason = 'ok'; fm.measure = MagicMock()
        gm = MagicMock(); gm.measure = MagicMock(side_effect=RuntimeError('LLM 失败'))
        self._metrics_mod.FaithfulnessMetric.return_value = fm
        self._metrics_mod.GEval.return_value = gm

        with patch('apps.analytics.services.wiki_eval_service.logger'):
            result = evaluate_wiki_page(page.id)

        assert result['ok'] is False
        assert result['evaluated'] == ['faithfulness']
        assert result['failed'] == ['completeness']
        failed = WikiPageQualityScore.objects.get(page=page, dimension='completeness')
        assert failed.status == 'failed'
        assert 'LLM 失败' in failed.error_message
