"""
apps.analytics.low_score_analyzer 单元测试 —— 低分对话规则归因分析

覆盖范围（仅规则归因部分，不含真实 LLM 调用）：
- _get_low_dimensions：阈值过滤 / 升序排序 / None 分处理 / reason 截断
- _get_retrieval_signal：无命中 / rerank 与 rerank_score 字段兼容 / 非法值跳过
- _rule_based_root_cause：10 条决策树规则逐条命中（safety 优先 → unknown 兜底）
- _should_trigger_llm：safety/question_side/unknown 不走 LLM，关键维度/多维触发
- _build_template_suggestions：模板建议结构与未知分类兜底
- _parse_llm_response：JSON / markdown 代码块 / 非法响应 / 字段截断与条数限制
- _build_llm_prompt：contexts 截断 Top3、低分维度 reason 拼接
- analyze_low_score_qa（DB）：无分数抛错 / 无低分兜底 / 规则模式 / hybrid 模式降级

说明：LLM 建议部分（_llm_generate_suggestions）在 analyze_low_score_qa 测试中
于源模块层面 mock，避免真实调用外部模型。
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, ANY as unittest_mock_ANY

from apps.analytics import low_score_analyzer
from apps.users.models import User
from apps.knowledge.models import KnowledgeNode
from apps.memory.models import Session
from apps.chat.models import QaRecord


def _qa_record(answer_type='rag', retrieval_scores=None, question='问题', answer='回答'):
    """构造假 QaRecord（规则归因只访问 answer_type/retrieval_scores/question/answer）"""
    return SimpleNamespace(
        answer_type=answer_type,
        retrieval_scores=retrieval_scores or [],
        question=question,
        answer=answer,
    )


def _sig(**kwargs):
    """构造检索信号 dict 快捷方式"""
    base = {'hit_count': 0, 'max_rerank': 0.0, 'avg_rerank': 0.0, 'has_context': False}
    base.update(kwargs)
    return base


def _scores(*items):
    """构造 12 维评分列表：[('dimension', score, 'reason'), ...]"""
    return [{'dimension': d, 'score': s, 'reason': r} for d, s, r in items]


# ============================================================================
# _get_low_dimensions —— 低分维度筛选
# ============================================================================
class TestGetLowDimensions:
    """低分维度筛选测试"""

    @pytest.mark.unit
    def test_filters_and_sorts_ascending(self):
        """低于阈值的维度被筛出，并按分数升序排列"""
        scores = _scores(
            ('clarity', 0.9, 'ok'),
            ('toxicity', 0.1, 'bad'),
            ('faithfulness', 0.4, 'bad'),
            ('bias', 0.5, '边界'),
        )
        low = low_score_analyzer._get_low_dimensions(scores, threshold=0.5)
        # 0.5 不 < 0.5，被排除；0.9 高于阈值被排除
        assert [d['dimension'] for d in low] == ['toxicity', 'faithfulness']

    @pytest.mark.unit
    def test_none_score_treated_zero(self):
        """score 缺失/None 按 0 处理（低于任意正阈值 → 进入低分列表）"""
        scores = [{'dimension': 'clarity', 'score': None, 'reason': 'x'}]
        low = low_score_analyzer._get_low_dimensions(scores, threshold=0.5)
        assert low[0]['score'] == 0.0

    @pytest.mark.unit
    def test_reason_truncated_and_score_rounded(self):
        """reason 截断到 200 字符，score 保留 4 位小数"""
        scores = [{'dimension': 'clarity', 'score': 0.12345678, 'reason': '长' * 300}]
        low = low_score_analyzer._get_low_dimensions(scores, threshold=0.5)
        assert low[0]['score'] == 0.1235
        assert len(low[0]['reason']) == 200

    @pytest.mark.unit
    def test_empty_input(self):
        """空评分列表 → 空低分列表"""
        assert low_score_analyzer._get_low_dimensions([], 0.5) == []


# ============================================================================
# _get_retrieval_signal —— 检索链路信号提取
# ============================================================================
class TestGetRetrievalSignal:
    """检索信号提取测试"""

    @pytest.mark.unit
    def test_no_hits(self):
        """无检索命中 → 全零信号且 has_context=False"""
        sig = low_score_analyzer._get_retrieval_signal(_qa_record(retrieval_scores=[]))
        assert sig == {'hit_count': 0, 'max_rerank': 0.0, 'avg_rerank': 0.0, 'has_context': False}

    @pytest.mark.unit
    def test_rerank_field_used(self):
        """优先使用 rerank 字段计算 max/avg"""
        hits = [{'chunk_id': 1, 'rerank': 0.3}, {'chunk_id': 2, 'rerank': 0.7}]
        sig = low_score_analyzer._get_retrieval_signal(_qa_record(retrieval_scores=hits))
        assert sig['hit_count'] == 2
        assert sig['max_rerank'] == 0.7
        assert sig['avg_rerank'] == 0.5
        assert sig['has_context'] is True

    @pytest.mark.unit
    def test_rerank_score_fallback(self):
        """旧数据只有 rerank_score 字段时兼容读取"""
        hits = [{'chunk_id': 1, 'rerank_score': 0.9}]
        sig = low_score_analyzer._get_retrieval_signal(_qa_record(retrieval_scores=hits))
        assert sig['max_rerank'] == 0.9
        assert sig['has_context'] is True

    @pytest.mark.unit
    def test_invalid_rerank_values_skipped(self):
        """rerank 为非法值时跳过，不影响命中数统计"""
        hits = [{'chunk_id': 1, 'rerank': 'abc'}, {'chunk_id': 2, 'rerank': None}]
        sig = low_score_analyzer._get_retrieval_signal(_qa_record(retrieval_scores=hits))
        assert sig['hit_count'] == 2
        assert sig['max_rerank'] == 0.0
        # 有命中切片但无有效 rerank 分 → 仍视为有上下文
        assert sig['has_context'] is True

    @pytest.mark.unit
    def test_all_rerank_values_invalid(self):
        """全部 rerank 值非法 → 无有效 rerank 分可统计，走空分兜底分支"""
        hits = [{'chunk_id': 1, 'rerank': 'abc'}, {'chunk_id': 2, 'rerank': [1, 2]}]
        sig = low_score_analyzer._get_retrieval_signal(_qa_record(retrieval_scores=hits))
        assert sig['hit_count'] == 2
        assert sig['max_rerank'] == 0.0
        assert sig['avg_rerank'] == 0.0
        # 有命中切片 → 仍视为有上下文（max/avg 为 0）
        assert sig['has_context'] is True


# ============================================================================
# _rule_based_root_cause —— 规则归因决策树
# ============================================================================
class TestRuleBasedRootCause:
    """归因决策树逐条命中测试"""

    def _call(self, scores, retrieval_signal, answer_type='rag'):
        qa = _qa_record(answer_type=answer_type)
        low = low_score_analyzer._get_low_dimensions(scores, 0.5)
        return low_score_analyzer._rule_based_root_cause(
            scores, low, qa, retrieval_signal, 0.5)

    @pytest.mark.unit
    def test_safety_highest_priority(self):
        """safety（toxicity/bias 低分）优先级最高，即使同时存在其他低分维度"""
        category, detail = self._call(
            _scores(('toxicity', 0.1, 'bad'), ('faithfulness', 0.2, 'bad')),
            _sig(hit_count=5, max_rerank=0.9, has_context=True))
        assert category == 'safety'
        assert 'toxicity' in detail

    @pytest.mark.unit
    def test_content_gap_when_refused_no_context(self):
        """无检索上下文 + 拒答 → 知识盲区"""
        category, _ = self._call(
            _scores(('answer_relevancy', 0.2, '低')),
            _sig(), answer_type='refused')
        assert category == 'content_gap'

    @pytest.mark.unit
    def test_question_side_no_context_low_relevancy(self):
        """无上下文 + 未拒答 + answer_relevancy 低 → 问题超纲"""
        category, _ = self._call(
            _scores(('answer_relevancy', 0.2, '低')),
            _sig())
        assert category == 'question_side'

    @pytest.mark.unit
    def test_generation_hallucination(self):
        """faithfulness 低 + rerank 高 → 检索好、生成层幻觉"""
        category, detail = self._call(
            _scores(('faithfulness', 0.3, '幻觉')),
            _sig(hit_count=4, max_rerank=0.8, has_context=True))
        assert category == 'generation_hallucination'
        assert 'rerank' in detail

    @pytest.mark.unit
    def test_retrieval_recall(self):
        """context_relevancy 低 + 命中数 < 3 → 召回不足"""
        category, _ = self._call(
            _scores(('context_relevancy', 0.2, '低')),
            _sig(hit_count=2, max_rerank=0.8, has_context=True))
        assert category == 'retrieval_recall'

    @pytest.mark.unit
    def test_retrieval_rank(self):
        """context_relevancy 低 + 命中多但 rerank 低 → 排序失效"""
        category, _ = self._call(
            _scores(('context_relevancy', 0.2, '低')),
            _sig(hit_count=5, max_rerank=0.2, has_context=True))
        assert category == 'retrieval_rank'

    @pytest.mark.unit
    def test_generation_offtopic(self):
        """answer_relevancy 低 + 有上下文 → 生成跑题"""
        category, _ = self._call(
            _scores(('answer_relevancy', 0.2, '低')),
            _sig(hit_count=4, max_rerank=0.8, has_context=True))
        assert category == 'generation_offtopic'

    @pytest.mark.unit
    def test_generation_incomplete(self):
        """completeness 低 → 回答不完整"""
        category, _ = self._call(
            _scores(('completeness', 0.3, '漏要点')),
            _sig(hit_count=4, max_rerank=0.8, has_context=True))
        assert category == 'generation_incomplete'

    @pytest.mark.unit
    def test_generation_format(self):
        """clarity/conciseness 低 → 表达类问题"""
        category, _ = self._call(
            _scores(('clarity', 0.3, '模糊')),
            _sig(hit_count=4, max_rerank=0.8, has_context=True))
        assert category == 'generation_format'

    @pytest.mark.unit
    def test_unknown_fallback(self):
        """无任何规则命中 → unknown 兜底"""
        category, _ = self._call(
            _scores(('professionalism', 0.3, '口语化')),
            _sig(hit_count=4, max_rerank=0.8, has_context=True))
        assert category == 'unknown'

    @pytest.mark.unit
    def test_priority_hallucination_over_recall(self):
        """faithfulness 低 + rerank 高，优先判为幻觉而非召回问题"""
        category, _ = self._call(
            _scores(('faithfulness', 0.2, '幻觉'), ('context_relevancy', 0.2, '低')),
            _sig(hit_count=2, max_rerank=0.9, has_context=True))
        assert category == 'generation_hallucination'


# ============================================================================
# _should_trigger_llm —— 是否触发 LLM 个性化建议
# ============================================================================
class TestShouldTriggerLLM:
    """LLM 触发策略测试"""

    @pytest.mark.unit
    def test_no_llm_for_safety_question_unknown(self):
        """safety/question_side/unknown 三类不走 LLM（模板告警即可）"""
        for cat in ('safety', 'question_side', 'unknown'):
            assert low_score_analyzer._should_trigger_llm(cat, []) is False

    @pytest.mark.unit
    def test_critical_dimension_triggers(self):
        """关键维度（faithfulness/context_relevancy/answer_relevancy/hallucination）低分触发 LLM"""
        low = [{'dimension': 'faithfulness', 'score': 0.2, 'reason': ''}]
        assert low_score_analyzer._should_trigger_llm('generation_hallucination', low) is True

    @pytest.mark.unit
    def test_multi_dimension_triggers(self):
        """≥3 个低分维度触发 LLM（综合问题需深度分析）"""
        low = [{'dimension': d, 'score': 0.3, 'reason': ''}
               for d in ('clarity', 'conciseness', 'professionalism')]
        assert low_score_analyzer._should_trigger_llm('generation_format', low) is True

    @pytest.mark.unit
    def test_single_edge_dimension_no_llm(self):
        """单一边缘维度低分（clarity 等）仅模板建议"""
        low = [{'dimension': 'clarity', 'score': 0.3, 'reason': ''}]
        assert low_score_analyzer._should_trigger_llm('generation_format', low) is False


# ============================================================================
# _build_template_suggestions —— 模板建议库
# ============================================================================
class TestBuildTemplateSuggestions:
    """模板建议构建测试"""

    @pytest.mark.unit
    def test_known_category(self):
        """已知分类返回 short_term + long_term 结构建议"""
        suggestions = low_score_analyzer._build_template_suggestions('retrieval_recall')
        assert len(suggestions) > 0
        assert all(s['type'] in ('short_term', 'long_term') and s['action'] for s in suggestions)

    @pytest.mark.unit
    def test_unknown_category_fallback(self):
        """未知分类回退到 unknown 模板（兜底建议）"""
        suggestions = low_score_analyzer._build_template_suggestions('no_such_cat')
        assert len(suggestions) > 0
        assert any('规则' in s['action'] for s in suggestions)

    @pytest.mark.unit
    def test_category_to_layer_mapping(self):
        """归因分类到影响层级的映射完整"""
        assert low_score_analyzer.CATEGORY_TO_LAYER['safety'] == 'safety'
        assert low_score_analyzer.CATEGORY_TO_LAYER['retrieval_recall'] == 'retrieval'
        assert low_score_analyzer.CATEGORY_TO_LAYER['generation_hallucination'] == 'generation'


# ============================================================================
# _parse_llm_response —— LLM JSON 响应解析
# ============================================================================
class TestParseLLMResponse:
    """LLM 响应解析测试"""

    @pytest.mark.unit
    def test_plain_json(self):
        """标准 JSON 正常解析"""
        result = low_score_analyzer._parse_llm_response(
            '{"diagnosis": "召回不足", "short_term_actions": ["调大TopK"], '
            '"long_term_actions": ["补文档"]}')
        assert result['diagnosis'] == '召回不足'
        assert result['short_term_actions'] == ['调大TopK']
        assert result['long_term_actions'] == ['补文档']

    @pytest.mark.unit
    def test_markdown_fenced_json(self):
        """LLM 用 ```json 代码块包裹时先剥离再解析"""
        content = '```json\n{"diagnosis": "幻觉", "short_term_actions": ["降温度"], "long_term_actions": []}\n```'
        result = low_score_analyzer._parse_llm_response(content)
        assert result['diagnosis'] == '幻觉'

    @pytest.mark.unit
    def test_invalid_json_returns_none(self):
        """非法 JSON → None（调用方降级模板建议）"""
        assert low_score_analyzer._parse_llm_response('not json') is None
        assert low_score_analyzer._parse_llm_response('') is None

    @pytest.mark.unit
    def test_non_dict_returns_none(self):
        """JSON 是数组等非 dict → None"""
        assert low_score_analyzer._parse_llm_response('[1, 2, 3]') is None

    @pytest.mark.unit
    def test_missing_fields_defaulted(self):
        """缺少字段时用空值兜底，不抛 KeyError"""
        result = low_score_analyzer._parse_llm_response('{"diagnosis": "x"}')
        assert result['short_term_actions'] == []
        assert result['long_term_actions'] == []

    @pytest.mark.unit
    def test_action_count_and_length_limited(self):
        """短期建议最多 3 条、长期最多 2 条，每条截断 100 字"""
        result = low_score_analyzer._parse_llm_response(
            '{"diagnosis": "x", "short_term_actions": ["a", "b", "c", "d"], '
            '"long_term_actions": ["1", "2", "3"]}')
        assert len(result['short_term_actions']) == 3
        assert len(result['long_term_actions']) == 2


# ============================================================================
# _build_llm_prompt —— 构建 LLM 归因建议 prompt
# ============================================================================
class TestBuildLLMPrompt:
    """prompt 构建测试"""

    @pytest.mark.unit
    def test_contexts_truncated_to_top3(self):
        """contexts 最多取前 3 片，每片截断 300 字"""
        qa = _qa_record(retrieval_scores=[{'chunk_id': i} for i in range(4)])
        with patch('apps.analytics.production_eval._build_context_list',
                   return_value=['c1', 'c2', 'c3', 'c4']):
            messages = low_score_analyzer._build_llm_prompt(
                qa, [{'dimension': 'clarity', 'score': 0.3, 'reason': '模糊'}],
                'generation_format', 'detail', _sig(hit_count=4, max_rerank=0.8, has_context=True))
        assert len(messages) == 2
        assert messages[0]['role'] == 'system'
        assert messages[1]['role'] == 'user'
        user_content = messages[1]['content']
        # 归因分类与命中规则出现在 prompt 中
        assert 'generation_format' in user_content
        assert 'detail' in user_content
        # 只包含前 3 片
        assert 'c1' in user_content and 'c4' not in user_content


# ============================================================================
# _llm_generate_suggestions —— LLM 个性化建议生成（get_llm 全部 mock）
# ============================================================================
class TestLLMGenerateSuggestions:
    """LLM 建议生成三态：成功 / 响应不可用降级模板 / 调用异常降级模板"""

    @pytest.fixture
    def qa(self):
        return _qa_record(retrieval_scores=[{'chunk_id': 1, 'rerank': 0.8}])

    def _llm_resp(self, content, total_tokens=10, cost=0.01, latency_ms=5):
        return {'content': content, 'total_tokens': total_tokens,
                'cost': cost, 'latency_ms': latency_ms}

    @pytest.mark.unit
    def test_success_returns_parsed_suggestions(self, qa):
        """LLM 返回可用 JSON → 诊断 + LLM 建议 + token/cost/latency"""
        fake_llm = MagicMock()
        fake_llm.chat.return_value = self._llm_resp(
            '{"diagnosis": "召回不足", "short_term_actions": ["调大TopK"], '
            '"long_term_actions": ["补文档"]}')
        with patch('apps.llm.factory.get_llm', return_value=fake_llm) as mock_get, \
             patch('apps.analytics.production_eval._build_context_list', return_value=[]):
            diagnosis, suggestions, tokens, cost, latency = \
                low_score_analyzer._llm_generate_suggestions(
                    qa, [{'dimension': 'clarity', 'score': 0.3, 'reason': '模糊'}],
                    'generation_format', 'detail',
                    _sig(hit_count=1, max_rerank=0.0, has_context=True), 'test-llm')
        assert diagnosis == '召回不足'
        assert suggestions == [
            {'type': 'short_term', 'action': '调大TopK'},
            {'type': 'long_term', 'action': '补文档'},
        ]
        assert tokens == 10
        assert cost == 0.01
        assert latency == 5
        mock_get.assert_called_once_with(model='test-llm')
        fake_llm.chat.assert_called_once_with(
            unittest_mock_ANY, temperature=0, max_tokens=800)

    @pytest.mark.unit
    def test_unusable_response_falls_back_to_template(self, qa):
        """LLM 响应解析后无可用建议 → 降级为模板建议"""
        fake_llm = MagicMock()
        fake_llm.chat.return_value = self._llm_resp(
            '{"diagnosis": "x", "short_term_actions": [], "long_term_actions": []}')
        with patch('apps.llm.factory.get_llm', return_value=fake_llm), \
             patch('apps.analytics.production_eval._build_context_list', return_value=[]):
            diagnosis, suggestions, tokens, cost, latency = \
                low_score_analyzer._llm_generate_suggestions(
                    qa, [{'dimension': 'clarity', 'score': 0.3, 'reason': '模糊'}],
                    'generation_format', 'detail', _sig(), 'test-llm')
        assert diagnosis == ''
        assert suggestions, '降级后应有模板建议'
        assert all(s['type'] in ('short_term', 'long_term') for s in suggestions)
        assert tokens == 0
        assert cost == 0.0

    @pytest.mark.unit
    def test_llm_exception_falls_back_to_template(self, qa):
        """get_llm/chat 抛异常 → 降级模板建议，不阻断归因流程"""
        with patch('apps.llm.factory.get_llm',
                   side_effect=RuntimeError('llm down')):
            diagnosis, suggestions, tokens, cost, latency = \
                low_score_analyzer._llm_generate_suggestions(
                    qa, [{'dimension': 'clarity', 'score': 0.3, 'reason': '模糊'}],
                    'generation_format', 'detail', _sig(), 'test-llm')
        assert diagnosis == ''
        assert suggestions
        assert tokens == 0
        assert cost == 0.0


# ============================================================================
# DB 测试：analyze_low_score_qa 主入口
# ============================================================================
@pytest.mark.django_db
class TestAnalyzeLowScoreQA:
    """analyze_low_score_qa 主流程测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/节点/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='lsa_user', password='pass12345', email='lsa@test.com')
        self.node = KnowledgeNode.objects.create(
            name='lsa_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='LS')
        self.qa = QaRecord.objects.create(
            session=self.session, user=self.user, question='问题',
            answer='回答', answer_type='rag', root_type='test_root',
            retrieval_scores=[{'chunk_id': 1, 'rerank': 0.8}])

    def _scores(self, *items):
        return [{'dimension': d, 'score': s, 'reason': r} for d, s, r in items]

    def test_no_scores_raises(self):
        """无评估分数 → 抛 ValueError（调用方捕获落 failed 状态）"""
        with pytest.raises(ValueError, match='无评估分数'):
            low_score_analyzer.analyze_low_score_qa(self.qa.id, scores=[])

    def test_no_low_dims_returns_unknown(self):
        """无低分维度 → unknown 兜底，不触发任何建议"""
        result = low_score_analyzer.analyze_low_score_qa(
            self.qa.id, scores=self._scores(('clarity', 0.9, 'ok')))
        assert result['category'] == 'unknown'
        assert result['method'] == 'rule'
        assert result['suggestions'] == []
        assert result['low_dimensions'] == []

    def test_rule_only_path(self):
        """边缘维度低分（clarity）→ 规则归因 + 模板建议，method='rule'"""
        with patch('apps.analytics.low_score_analyzer._llm_generate_suggestions') as mock_llm:
            result = low_score_analyzer.analyze_low_score_qa(
                self.qa.id, scores=self._scores(('clarity', 0.3, '模糊')))
        assert result['category'] == 'generation_format'
        assert result['affected_layer'] == 'generation'
        assert result['method'] == 'rule'
        assert result['model'] == ''
        assert result['tokens'] == 0
        assert result['suggestions'], '规则路径应有模板建议'
        # 边缘维度不应触发 LLM
        mock_llm.assert_not_called()

    def test_hybrid_path_with_llm(self):
        """关键维度低分（faithfulness + rerank 高）→ 触发 LLM，method='hybrid'"""
        fake_llm_result = ('生成层幻觉', [{'type': 'short_term', 'action': '加强约束'}], 100, 0.01, 50)
        with patch('apps.analytics.low_score_analyzer._llm_generate_suggestions',
                   return_value=fake_llm_result) as mock_llm:
            result = low_score_analyzer.analyze_low_score_qa(
                self.qa.id,
                scores=self._scores(('faithfulness', 0.2, '幻觉')),
                model='test-llm')
        assert result['category'] == 'generation_hallucination'
        assert result['method'] == 'hybrid'
        assert result['model'] == 'deepeval-test-llm'
        assert result['tokens'] == 100
        assert result['cost'] == 0.01
        assert result['diagnosis'] == '生成层幻觉'
        mock_llm.assert_called_once()

    def test_avg_score_computed(self):
        """avg_score 为所有维度（含高分）的均值"""
        result = low_score_analyzer.analyze_low_score_qa(
            self.qa.id, scores=self._scores(
                ('clarity', 0.3, '模糊'), ('professionalism', 0.9, 'ok')),
            model='test-llm')
        assert result['avg_score'] == 0.6
        assert [d['dimension'] for d in result['low_dimensions']] == ['clarity']

    def test_scores_from_db_when_not_provided(self):
        """scores=None 时从 DB 查询 MultiDimensionScore（避免调用方重复查询）"""
        from apps.analytics.models import MultiDimensionScore
        MultiDimensionScore.objects.create(
            qa_record=self.qa, dimension='clarity', score=0.3, reason='模糊')
        MultiDimensionScore.objects.create(
            qa_record=self.qa, dimension='professionalism', score=0.9, reason='ok')
        with patch('apps.analytics.low_score_analyzer._llm_generate_suggestions') as mock_llm:
            result = low_score_analyzer.analyze_low_score_qa(self.qa.id)
        assert result['category'] == 'generation_format'
        assert result['method'] == 'rule'
        assert [d['dimension'] for d in result['low_dimensions']] == ['clarity']
        assert result['avg_score'] == 0.6
        mock_llm.assert_not_called()
