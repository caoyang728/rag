"""
apps.analytics.offline_eval 单元测试 —— 离线评估 Pipeline（黄金测试集 + 检索/回答评估）

覆盖范围：
- 指标纯函数：_calc_recall_at_k / _calc_mrr / _calc_ndcg_at_k / _avg（含空输入）
- 黄金测试集管理（DB）：create_golden_dataset / import_questions_from_json
  （新增/更新/空问题跳过/数据集不存在） / export_dataset_to_json 往返
- run_retrieval_evaluation（DB）：mock hybrid_search 验证 Recall/MRR/NDCG 与各阶段增益落库
- run_answer_quality_evaluation（DB）：mock 检索/生成/评估全链路，成功与失败分支
- _generate_answer / _generate_route_answer：LLM 响应组装与异常降级
- evaluate_all_modes（三模式横向对比）：聚合统计与空输入

说明：检索与 LLM 全部在源模块导入处 mock（apps.retrieval.hybrid.hybrid_search、
apps.llm.factory.get_llm、apps.analytics.deepeval_metrics.evaluate_with_deepeval 等），
不依赖真实向量库与外部模型。
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from apps.analytics import offline_eval
from apps.users.models import User
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk
from apps.analytics.models import (
    GoldenDataset, GoldenQuestion, GoldenRelevantDoc, GoldenReferenceAnswer,
)


# ============================================================================
# 指标纯函数
# ============================================================================
class TestRetrievalMetrics:
    """Recall@K / MRR / NDCG@K / avg 计算测试"""

    @pytest.mark.unit
    def test_recall_at_k_basic(self):
        """Recall@K = 前 K 结果中相关文档数 / 相关文档总数"""
        relevant = {1, 2}
        retrieved = [1, 9, 2, 3]
        assert offline_eval._calc_recall_at_k(relevant, retrieved, k=5) == 1.0
        assert offline_eval._calc_recall_at_k(relevant, retrieved, k=1) == 0.5

    @pytest.mark.unit
    def test_recall_at_k_empty_relevant(self):
        """无相关文档标注 → 0（不参与分母为 0 的除零）"""
        assert offline_eval._calc_recall_at_k(set(), [1, 2], k=5) == 0.0

    @pytest.mark.unit
    def test_recall_at_k_k_greater_than_retrieved(self):
        """K 大于实际检索结果数时只统计已有结果"""
        relevant = {1, 2}
        retrieved = [1]
        assert offline_eval._calc_recall_at_k(relevant, retrieved, k=20) == 0.5

    @pytest.mark.unit
    def test_mrr_basic(self):
        """MRR = 1 / 首个相关文档的排名"""
        assert offline_eval._calc_mrr({3}, [1, 2, 3, 4]) == pytest.approx(1 / 3)
        assert offline_eval._calc_mrr({1}, [1, 2]) == 1.0

    @pytest.mark.unit
    def test_mrr_no_hit(self):
        """无相关命中 → 0"""
        assert offline_eval._calc_mrr({9}, [1, 2, 3]) == 0.0
        assert offline_eval._calc_mrr({9}, []) == 0.0

    @pytest.mark.unit
    def test_ndcg_perfect_ranking(self):
        """理想排序（相关文档全部靠前）→ NDCG@K = 1.0"""
        relevant = {1, 2}
        retrieved = [1, 2, 3, 4]
        assert offline_eval._calc_ndcg_at_k(relevant, retrieved, k=5) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_ndcg_no_hit(self):
        """无相关命中 → 0.0"""
        relevant = {9}
        assert offline_eval._calc_ndcg_at_k(relevant, [1, 2], k=5) == 0.0

    @pytest.mark.unit
    def test_ndcg_empty_relevant(self):
        """空相关文档集合 → 0.0"""
        assert offline_eval._calc_ndcg_at_k(set(), [1, 2], k=5) == 0.0

    @pytest.mark.unit
    def test_ndcg_partial_ranking_between_zero_and_one(self):
        """部分命中时 NDCG 介于 0~1 之间"""
        relevant = {1, 2}
        retrieved = [9, 1, 2]
        ndcg = offline_eval._calc_ndcg_at_k(relevant, retrieved, k=5)
        assert 0 < ndcg < 1

    @pytest.mark.unit
    def test_avg(self):
        """平均值：空列表 0，正常列表求均值"""
        assert offline_eval._avg([]) == 0.0
        assert offline_eval._avg([1.0, 2.0, 3.0]) == 2.0


# ============================================================================
# DB 测试：黄金测试集管理
# ============================================================================
@pytest.mark.django_db
class TestGoldenDatasetManagement:
    """黄金测试集创建/导入/导出测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/节点/文档"""
        self.user = User.objects.create_user(
            username='gds_user', password='pass12345', email='gds@test.com')
        self.node = KnowledgeNode.objects.create(
            name='gds_root', node_type='root', root_type='test_root',
            created_by=self.user)
        # GoldenRelevantDoc.document 是真实 FK，必须用落库的文档 ID
        self.docs = []
        for i in range(3):
            self.docs.append(Document.objects.create(
                node=self.node, owner=self.user, title=f'doc{i}',
                file_name=f'd{i}.txt', file_type='txt', file_hash=f'h{i}',
                root_type='test_root', status='done', dept_id=1))

    def test_create_golden_dataset(self):
        """创建测试集：字段与默认值正确"""
        ds = offline_eval.create_golden_dataset(
            name='HR 测试集', root_type='hr', description='描述', version='v2',
            created_by_id=self.user.id)
        assert ds.name == 'HR 测试集'
        assert ds.root_type == 'hr'
        assert ds.version == 'v2'
        assert ds.question_count == 0
        assert ds.created_by_id == self.user.id

    def test_import_questions_creates_records(self):
        """导入问题：创建 GoldenQuestion + 相关文档标注 + 参考答案，并更新 question_count"""
        ds = offline_eval.create_golden_dataset(name='ds', created_by_id=self.user.id)
        result = offline_eval.import_questions_from_json(ds.id, [
            {'question': '薪资政策是什么？', 'question_type': 'factoid',
             'difficulty': 'easy', 'tags': ['HR'],
             'relevant_doc_ids': [self.docs[0].id, self.docs[1].id],
             'reference_answer': '按职级分档',
             'key_points': ['分档', '职级']},
            {'question': '请假流程？', 'relevant_doc_ids': [self.docs[2].id],
             'reference_answer': '提交申请'},
        ], created_by_id=self.user.id)
        assert result == {'created': 2, 'updated': 0}
        ds.refresh_from_db()
        assert ds.question_count == 2
        q = GoldenQuestion.objects.get(dataset=ds, question='薪资政策是什么？')
        assert q.question_type == 'factoid'
        assert [rd.document_id for rd in q.relevant_docs.all()] == \
            [self.docs[0].id, self.docs[1].id]
        ref = GoldenReferenceAnswer.objects.get(question=q)
        assert ref.reference_answer == '按职级分档'
        assert ref.key_points == ['分档', '职级']

    def test_import_questions_updates_existing(self):
        """重复导入同一问题 → updated +1，相关文档标注被替换"""
        ds = offline_eval.create_golden_dataset(name='ds')
        first = offline_eval.import_questions_from_json(ds.id, [
            {'question': 'Q', 'relevant_doc_ids': [self.docs[0].id],
             'reference_answer': 'a'},
        ])
        second = offline_eval.import_questions_from_json(ds.id, [
            {'question': 'Q', 'relevant_doc_ids': [self.docs[1].id, self.docs[2].id],
             'reference_answer': 'b'},
        ])
        assert first == {'created': 1, 'updated': 0}
        assert second == {'created': 0, 'updated': 1}
        q = GoldenQuestion.objects.get(dataset=ds, question='Q')
        # 旧标注被删除、新标注写入
        assert sorted(rd.document_id for rd in q.relevant_docs.all()) == \
            sorted([self.docs[1].id, self.docs[2].id])
        assert GoldenReferenceAnswer.objects.get(question=q).reference_answer == 'b'

    def test_import_skips_empty_question(self):
        """空问题文本被跳过，不创建记录"""
        ds = offline_eval.create_golden_dataset(name='ds')
        result = offline_eval.import_questions_from_json(ds.id, [
            {'question': '   '},
            {'question': '有效问题'},
        ])
        assert result == {'created': 1, 'updated': 0}
        ds.refresh_from_db()
        assert ds.question_count == 1

    def test_import_dataset_not_found(self):
        """数据集不存在 → ValueError"""
        with pytest.raises(ValueError, match='not found'):
            offline_eval.import_questions_from_json(99999, [])

    def test_export_round_trip(self):
        """导出与导入字段往返一致；无参考答案时兜底为空"""
        ds = offline_eval.create_golden_dataset(name='ds')
        offline_eval.import_questions_from_json(ds.id, [
            {'question': 'Q1', 'question_type': 'reasoning', 'difficulty': 'hard',
             'tags': ['T'], 'relevant_doc_ids': [self.docs[0].id],
             'reference_answer': 'R', 'key_points': ['P']},
            {'question': 'Q2'},  # 无参考答案
        ])
        exported = offline_eval.export_dataset_to_json(ds.id)
        assert len(exported) == 2
        first = exported[0]
        assert first['question'] == 'Q1'
        assert first['reference_answer'] == 'R'
        assert first['key_points'] == ['P']
        assert first['relevant_doc_ids'] == [self.docs[0].id]
        assert exported[1]['reference_answer'] == ''
        assert exported[1]['key_points'] == []


# ============================================================================
# DB 测试：离线检索评估
# ============================================================================
@pytest.mark.django_db
class TestRunRetrievalEvaluation:
    """run_retrieval_evaluation 检索评估测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/节点/文档/黄金数据集"""
        self.user = User.objects.create_user(
            username='ret_user', password='pass12345', email='ret@test.com')
        self.node = KnowledgeNode.objects.create(
            name='ret_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.docs = []
        for i in range(3):
            self.docs.append(Document.objects.create(
                node=self.node, owner=self.user, title=f'doc{i}', file_name=f'd{i}.txt',
                file_type='txt', file_hash=f'h{i}', root_type='test_root',
                status='done', dept_id=1))
        self.dataset = offline_eval.create_golden_dataset(name='检索集', root_type='hr')

    def _add_question(self, text, doc_ids):
        q = GoldenQuestion.objects.create(dataset=self.dataset, question=text, order=0)
        for doc_id in doc_ids:
            GoldenRelevantDoc.objects.create(question=q, document_id=doc_id)
        return q

    def test_report_metrics_with_hits(self):
        """检索命中相关文档 → Recall/MRR 及各阶段增益写入报告"""
        self._add_question('Q', [self.docs[0].id, self.docs[1].id])
        search_result = {
            'chunks': [
                {'document_id': self.docs[0].id, 'chunk_id': 1},
                {'document_id': self.docs[1].id, 'chunk_id': 3},
                {'document_id': self.docs[2].id, 'chunk_id': 2},
            ],
            'raw': {
                'vector': [{'document_id': self.docs[0].id}],
                'bm25': [{'document_id': self.docs[0].id}],
                'rrf': [{'document_id': self.docs[0].id}],
            },
        }
        with patch('apps.retrieval.hybrid.hybrid_search', return_value=search_result):
            report = offline_eval.run_retrieval_evaluation(self.dataset.id, user=self.user)

        assert report.total_questions == 1
        assert report.recall_at_5 == 1.0
        assert report.mrr == 1.0
        assert report.ndcg_at_5 == pytest.approx(1.0)
        assert report.questions_with_hits == 1
        assert report.questions_without_hits == 0
        # 各阶段增益：vector/bm25/rrf 均命中
        assert report.vector_recall_at_10 == 1.0
        assert report.bm25_recall_at_10 == 1.0
        assert report.hybrid_recall_at_10 == 1.0
        assert report.config_snapshot['vector_top_k'] == 30

    def test_report_no_hits(self):
        """检索未命中相关文档 → 指标为 0，questions_without_hits 计数"""
        self._add_question('Q', [self.docs[0].id])
        search_result = {
            'chunks': [{'document_id': self.docs[2].id, 'chunk_id': 9}],
            'raw': {'vector': [], 'bm25': [], 'rrf': []},
        }
        with patch('apps.retrieval.hybrid.hybrid_search', return_value=search_result):
            report = offline_eval.run_retrieval_evaluation(self.dataset.id, user=self.user)
        assert report.recall_at_5 == 0.0
        assert report.mrr == 0.0
        assert report.questions_without_hits == 1
        assert report.status == 'completed'

    def test_no_questions_raises(self):
        """测试集无问题 → ValueError"""
        with pytest.raises(ValueError, match='no questions'):
            offline_eval.run_retrieval_evaluation(self.dataset.id)

    def test_search_failure_skipped(self):
        """单问题检索抛异常 → 跳过该问题，不中断整体评估"""
        self._add_question('Q', [self.docs[0].id])
        with patch('apps.retrieval.hybrid.hybrid_search',
                   side_effect=RuntimeError('search down')):
            report = offline_eval.run_retrieval_evaluation(self.dataset.id, user=self.user)
        assert report.total_questions == 1
        assert report.recall_at_5 == 0.0


# ============================================================================
# DB 测试：离线回答质量评估
# ============================================================================
@pytest.mark.django_db
class TestRunAnswerQualityEvaluation:
    """run_answer_quality_evaluation 回答评估测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/节点/黄金数据集"""
        self.user = User.objects.create_user(
            username='ans_user', password='pass12345', email='ans@test.com')
        self.node = KnowledgeNode.objects.create(
            name='ans_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.dataset = offline_eval.create_golden_dataset(name='回答集', root_type='hr')
        GoldenQuestion.objects.create(dataset=self.dataset, question='Q1', order=0)
        GoldenQuestion.objects.create(dataset=self.dataset, question='Q2', order=1)

    def test_happy_path_results(self):
        """全链路成功：每问题含 avg_score 与 dimension_scores"""
        search_result = {'chunks': [{'chunk_id': 10}, {'chunk_id': 11}]}
        eval_results = [
            {'dimension': 'clarity', 'score': 0.8},
            {'dimension': 'faithfulness', 'score': 0.6},
        ]
        with patch('apps.retrieval.hybrid.hybrid_search', return_value=search_result), \
             patch('apps.analytics.offline_eval._generate_answer', return_value='测试回答'), \
             patch('apps.analytics.deepeval_metrics.evaluate_with_deepeval',
                   return_value=eval_results):
            results = offline_eval.run_answer_quality_evaluation(self.dataset.id, user=self.user)

        assert len(results) == 2
        for r in results:
            assert r['avg_score'] == pytest.approx(0.7)
            assert r['dimension_scores'] == {'clarity': 0.8, 'faithfulness': 0.6}
            assert 'eval_batch_id' in r

    def test_search_failure_records_error(self):
        """单问题检索失败 → 该问题带 error 字段，不中断后续问题"""
        def flaky(query, **kwargs):
            if query == 'Q1':
                raise RuntimeError('boom')
            return {'chunks': []}

        with patch('apps.retrieval.hybrid.hybrid_search', side_effect=flaky), \
             patch('apps.analytics.offline_eval._generate_answer', return_value='a'), \
             patch('apps.analytics.deepeval_metrics.evaluate_with_deepeval',
                   return_value=[{'dimension': 'clarity', 'score': 0.9}]):
            results = offline_eval.run_answer_quality_evaluation(self.dataset.id, user=self.user)
        assert len(results) == 2
        failed = next(r for r in results if 'error' in r)
        assert 'boom' in failed['error']
        assert len([r for r in results if 'avg_score' in r]) == 1


# ============================================================================
# _generate_answer / _generate_route_answer —— 回答生成
# ============================================================================
class TestGenerateAnswer:
    """回答生成函数测试"""

    @pytest.mark.unit
    def test_generate_answer_success(self):
        """正常路径：按 chat.completions.create 接口组装并返回内容"""
        fake_llm = MagicMock()
        fake_llm.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='生成答案'))])
        with patch('apps.llm.factory.get_llm', return_value=fake_llm):
            answer = offline_eval._generate_answer('问题', '上下文', 'deepseek-chat')
        assert answer == '生成答案'
        _, kwargs = fake_llm.chat.completions.create.call_args
        assert kwargs['model'] == 'deepseek-chat'
        assert kwargs['max_tokens'] == 1000
        assert kwargs['temperature'] == 0.3

    @pytest.mark.unit
    def test_generate_answer_failure(self):
        """LLM 调用异常 → 返回失败占位文本，不抛异常"""
        fake_llm = MagicMock()
        fake_llm.chat.completions.create.side_effect = RuntimeError('llm down')
        with patch('apps.llm.factory.get_llm', return_value=fake_llm):
            answer = offline_eval._generate_answer('问题', '上下文', 'deepseek-chat')
        assert answer.startswith('[回答生成失败]')

    @pytest.mark.unit
    def test_generate_route_answer_success(self):
        """三层路由对比的回答生成：注入 QA_USER_TEMPLATE 后调用 llm.chat"""
        fake_llm = MagicMock()
        fake_llm.chat.return_value = {'content': '路由回答'}
        with patch('apps.llm.factory.get_llm', return_value=fake_llm), \
             patch('apps.llm.prompts.qa.QA_USER_TEMPLATE',
                   '{memory_block}|{context_block}|{question}'), \
             patch('apps.llm.prompts.qa.SYSTEM_PROMPT', 'sys'):
            answer = offline_eval._generate_route_answer(fake_llm, '问题', '上下文')
        assert answer == '路由回答'
        messages = fake_llm.chat.call_args[0][0]
        assert messages[1]['content'] == '（无历史记忆）|上下文|问题'

    @pytest.mark.unit
    def test_generate_route_answer_failure(self):
        """回答生成异常 → 返回空串，不影响其他模式"""
        fake_llm = MagicMock()
        fake_llm.chat.side_effect = RuntimeError('down')
        with patch('apps.llm.prompts.qa.QA_USER_TEMPLATE', '{question}'), \
             patch('apps.llm.prompts.qa.SYSTEM_PROMPT', 'sys'):
            assert offline_eval._generate_route_answer(fake_llm, 'q', 'c') == ''


# ============================================================================
# evaluate_all_modes —— 三模式横向对比
# ============================================================================
class TestEvaluateAllModes:
    """Wiki / GraphRAG / RAG 三层对比评估测试"""

    @pytest.mark.unit
    def test_aggregation(self):
        """三个检索层独立产出上下文并生成回答，按长度启发式打分"""
        questions = [{'question': 'q1'}, {'question': 'q2'}, {'question': ''}]
        with patch('apps.wiki.retriever.search_wiki',
                   return_value=[{'title': 't', 'content': '内容'}]), \
             patch('apps.graph.retriever.graphrag_search',
                   return_value={'context': '图上下文'}), \
             patch('apps.retrieval.hybrid.hybrid_search',
                   return_value={'chunks': [{'document_id': 1}]}), \
             patch('apps.graph.router._format_rag_context', return_value='rag 上下文'), \
             patch('apps.analytics.offline_eval._generate_route_answer',
                   return_value='这是一个长度超过十个字符的完整回答文本'), \
             patch('apps.llm.factory.get_llm'):
            results = offline_eval.evaluate_all_modes(questions, max_questions=10)

        assert results['wiki']['count'] == 2
        assert results['graphrag']['count'] == 2
        assert results['rag']['count'] == 2
        # 回答长度 > 10 → 1.0 分
        for mode in ('wiki', 'graphrag', 'rag'):
            assert results[mode]['avg_score'] == 1.0

    @pytest.mark.unit
    def test_empty_questions(self):
        """空问题列表（或全空字符串）→ 各模式 count=0、均分 0"""
        with patch('apps.wiki.retriever.search_wiki') as m1, \
             patch('apps.graph.retriever.graphrag_search') as m2, \
             patch('apps.retrieval.hybrid.hybrid_search') as m3, \
             patch('apps.analytics.offline_eval._generate_route_answer') as m4, \
             patch('apps.llm.factory.get_llm'):
            results = offline_eval.evaluate_all_modes([{'question': ''}], max_questions=10)
        m1.assert_not_called()
        m2.assert_not_called()
        m3.assert_not_called()
        m4.assert_not_called()
        for mode in ('wiki', 'graphrag', 'rag'):
            assert results[mode]['count'] == 0
            assert results[mode]['avg_score'] == 0.0
            assert results[mode]['avg_latency_ms'] == 0

    @pytest.mark.unit
    def test_retrieval_failure_isolated(self):
        """单个检索层失败不影响其他层与其他题目"""
        questions = [{'question': 'q'}]
        with patch('apps.wiki.retriever.search_wiki',
                   side_effect=RuntimeError('wiki down')), \
             patch('apps.graph.retriever.graphrag_search',
                   return_value={'context': '图上下文'}), \
             patch('apps.retrieval.hybrid.hybrid_search',
                   return_value={'chunks': [{'document_id': 1}]}), \
             patch('apps.graph.router._format_rag_context', return_value='rag 上下文'), \
             patch('apps.analytics.offline_eval._generate_route_answer', return_value='好的回答文本内容'), \
             patch('apps.llm.factory.get_llm'):
            results = offline_eval.evaluate_all_modes(questions, max_questions=10)
        # wiki 失败 → count 0；graphrag/rag 正常
        assert results['wiki']['count'] == 0
        assert results['graphrag']['count'] == 1
        assert results['rag']['count'] == 1
