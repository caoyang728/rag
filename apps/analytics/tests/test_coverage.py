"""
apps.analytics.services.coverage_service 单元测试 —— 知识库覆盖率统计与反馈闭环

覆盖范围：
- _generate_gap_suggestion：知识空白建议（关键词提取 / 兜底文案）
- _suggest_resolution：差评标签 → 处理建议组合
- analyze_hot_query_coverage（DB）：热门问题覆盖率（命中/未命中计数与比例）
- detect_knowledge_gaps（DB）：拒答高频查询识别，min_count 阈值过滤
- detect_duplicate_chunks（DB）：Jaccard 相似度重复切片检测（含空库）
- analyze_domain_coverage（DB）：部门→团队 文档/切片/命中率嵌套结构
- auto_link_feedback_to_chunks（DB）：差评标签/长评论关联问题 chunk
- generate_coverage_report（DB）：mock 子分析后汇总落 CoverageReport

DB 用例复用真实 ORM（Test 库），QaRecord 创建依赖 Session/User/KnowledgeNode，
"""
import pytest
from unittest.mock import patch

from apps.analytics.services import coverage_service
from apps.users.models import User, Department, Team
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk
from apps.memory.models import Session
from apps.chat.models import QaRecord, QaFeedback


# ============================================================================
# 纯函数：建议生成
# ============================================================================
class TestSuggestions:
    """知识空白与差评处理建议生成测试"""

    @pytest.mark.unit
    def test_gap_suggestion_with_keywords(self):
        """查询包含 ≥2 字词 → 建议补充这些关键词相关文档（最多 5 个）"""
        assert coverage_service._generate_gap_suggestion('员工 请假 流程 说明') == \
            '建议补充关于"员工 请假 流程 说明"相关的文档'

    @pytest.mark.unit
    def test_gap_suggestion_short_words_filtered(self):
        """所有词都短于 2 字 → 回退通用建议"""
        assert coverage_service._generate_gap_suggestion('a b c') == \
            '建议检查知识库覆盖范围是否满足该查询需求'

    @pytest.mark.unit
    def test_gap_suggestion_empty_query(self):
        """空查询 → 通用建议"""
        assert coverage_service._generate_gap_suggestion('') == \
            '建议检查知识库覆盖范围是否满足该查询需求'

    @pytest.mark.unit
    def test_suggest_resolution_accuracy_tags(self):
        """不准确/不相关标签 → 检查切片相关性建议"""
        suggestion = coverage_service._suggest_resolution(['不准确'], '')
        assert '切片' in suggestion

    @pytest.mark.unit
    def test_suggest_resolution_outdated_tag(self):
        """过时标签 → 更新文档建议"""
        assert '更新' in coverage_service._suggest_resolution(['过时'], '')

    @pytest.mark.unit
    def test_suggest_resolution_citation_tag(self):
        """无引用/引用错误 → 优化引用逻辑建议"""
        suggestion = coverage_service._suggest_resolution(['无引用'], '')
        assert '引用' in suggestion

    @pytest.mark.unit
    def test_suggest_resolution_speed_tag(self):
        """回答慢/速度标签 → LLM 配置与缓存建议"""
        suggestion = coverage_service._suggest_resolution(['回答慢'], '')
        assert 'LLM' in suggestion

    @pytest.mark.unit
    def test_suggest_resolution_combined_tags(self):
        """多标签组合 → 多条建议用分号拼接"""
        suggestion = coverage_service._suggest_resolution(['不准确', '过时'], '')
        assert '；' in suggestion

    @pytest.mark.unit
    def test_suggest_resolution_generic_fallback(self):
        """无匹配标签 → 通用人工审核建议"""
        assert '人工审核' in coverage_service._suggest_resolution(['其他'], 'x')


# ============================================================================
# DB 基类：QA 数据准备
# ============================================================================
class CoverageDBTestBase:
    """覆盖率相关 DB 测试公共基类"""

    @pytest.fixture(autouse=True)
    def _db_env(self):
        """pytest fixture：注入用户/节点/会话"""
        self.user = User.objects.create_user(
            username='cov_user', password='pass12345', email='cov@test.com')
        self.node = KnowledgeNode.objects.create(
            name='cov_root', node_type='root', root_type='test_root',
            created_by=self.user)
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='Cov')

    def _create_qa(self, question, retrieval_hits=None, answer_type='rag',
                   is_hit_cache=False):
        """创建一条 QA 记录（retrieval_hits 为空数组表示无命中）"""
        return QaRecord.objects.create(
            session=self.session, user=self.user, question=question,
            answer='回答', answer_type=answer_type, root_type='test_root',
            is_hit_cache=is_hit_cache,
            retrieval_hits=retrieval_hits or [])

    def _create_doc(self, dept_id=None, team_id=None, name='doc'):
        """创建 status='done' 的文档（用于重复切片/领域覆盖分析）

        dept_id 缺省时补 1：Document 有 doc_owner_scope_required CHECK 约束
        （team_id 或 dept_id 至少一个非空），且 dept_id 是普通整数列非 FK，
        可安全使用哨兵值 1
        """
        return Document.objects.create(
            node=self.node, owner=self.user, title=name, file_name=f'{name}.txt',
            file_type='txt', file_hash=name, root_type='test_root',
            status='done', dept_id=dept_id if dept_id is not None else 1,
            team_id=team_id)

    def _create_chunk(self, doc, content, chunk_index=0):
        return DocumentChunk.objects.create(
            document=doc, chunk_index=chunk_index, content=content,
            chunk_type='text')


# ============================================================================
# 热门问题覆盖率
# ============================================================================
@pytest.mark.django_db
class TestAnalyzeHotQueryCoverage(CoverageDBTestBase):
    """analyze_hot_query_coverage 测试"""

    def test_coverage_rate(self):
        """命中检索的查询计入 covered，无命中计入 uncovered"""
        self._create_qa('热门问题A', retrieval_hits=[1, 2])
        self._create_qa('热门问题A', retrieval_hits=[3])
        self._create_qa('热门问题B')  # 无命中
        result = coverage_service.analyze_hot_query_coverage(days=7)
        assert result['total_hot_queries'] == 2
        assert result['covered_queries'] == 1
        assert result['uncovered_queries'] == 1
        assert result['hot_query_coverage_rate'] == 0.5
        assert len(result['uncovered_examples']) == 1
        assert result['uncovered_examples'][0]['query'] == '热门问题B'

    def test_cache_hit_excluded(self):
        """缓存命中记录不参与覆盖统计"""
        self._create_qa('Q', retrieval_hits=[1], is_hit_cache=True)
        result = coverage_service.analyze_hot_query_coverage(days=7)
        assert result['total_hot_queries'] == 0
        assert result['hot_query_coverage_rate'] == 0.0

    def test_empty_db(self):
        """无任何 QA → 空报告且不除零"""
        result = coverage_service.analyze_hot_query_coverage(days=7)
        assert result['total_hot_queries'] == 0
        assert result['hot_query_coverage_rate'] == 0.0


# ============================================================================
# 知识空白检测
# ============================================================================
@pytest.mark.django_db
class TestDetectKnowledgeGaps(CoverageDBTestBase):
    """detect_knowledge_gaps 测试"""

    def test_gaps_detected_by_frequency(self):
        """拒答查询达到 min_count 才被识别为知识空白"""
        for _ in range(3):
            self._create_qa('高频无资料问题', answer_type='refused')
        self._create_qa('低频无资料问题', answer_type='refused')
        gaps = coverage_service.detect_knowledge_gaps(days=7, min_count=3)
        assert len(gaps) == 1
        assert gaps[0]['query'] == '高频无资料问题'
        assert gaps[0]['count'] == 3
        assert '建议补充' in gaps[0]['suggestion']

    def test_non_refused_excluded(self):
        """非拒答查询不计入知识空白"""
        self._create_qa('正常问题', answer_type='rag')
        assert coverage_service.detect_knowledge_gaps(days=7, min_count=1) == []


# ============================================================================
# 重复切片检测
# ============================================================================
@pytest.mark.django_db
class TestDetectDuplicateChunks(CoverageDBTestBase):
    """detect_duplicate_chunks 测试"""

    def test_duplicates_detected(self):
        """内容高度相似（Jaccard ≥ 阈值）的切片被识别为重复"""
        doc = self._create_doc(name='dup_doc')
        # 内容需空格分词 ≥5 词才会参与检测（Jaccard 按 split() 分词）
        content = '员工 请假 流程 审批 规则 详细 说明 文本 段落'
        self._create_chunk(doc, content, chunk_index=0)
        self._create_chunk(doc, content, chunk_index=1)  # 完全相同
        self._create_chunk(doc, '完全 不同 的 内容 主题', chunk_index=2)
        result = coverage_service.detect_duplicate_chunks(similarity_threshold=0.9)
        assert result['total_chunks_checked'] == 3
        assert result['duplicate_count'] == 1
        assert result['duplicate_examples'][0]['chunk_a_id'] != \
            result['duplicate_examples'][0]['chunk_b_id']

    def test_short_content_skipped(self):
        """词数 < 5 的短切片不参与相似度检测（避免误判）"""
        doc = self._create_doc(name='short_doc')
        self._create_chunk(doc, '仅 两 个 词', chunk_index=0)
        self._create_chunk(doc, '仅 两 个 词', chunk_index=1)
        result = coverage_service.detect_duplicate_chunks()
        assert result['duplicate_count'] == 0

    def test_empty_db(self):
        """无切片 → total=0 且 duplicate_rate=0"""
        result = coverage_service.detect_duplicate_chunks()
        assert result['total'] == 0
        assert result['duplicate_rate'] == 0.0
        assert result['duplicate_groups'] == []


# ============================================================================
# 领域覆盖分析
# ============================================================================
@pytest.mark.django_db
class TestAnalyzeDomainCoverage(CoverageDBTestBase):
    """analyze_domain_coverage 测试"""

    def test_domain_structure(self):
        """部门→团队 嵌套结构、文档/切片计数、全局命中率正确"""
        dept = Department.objects.create(name='研发部')
        team_a = Team.objects.create(name='平台组', department=dept)
        team_b = Team.objects.create(name='算法组', department=dept)

        doc1 = self._create_doc(dept_id=dept.id, team_id=team_a.id, name='d1')
        self._create_chunk(doc1, '内容A' * 50, chunk_index=0)
        self._create_chunk(doc1, '内容B' * 50, chunk_index=1)
        self._create_doc(dept_id=dept.id, team_id=team_b.id, name='d2')

        # 命中率：1 条有命中，1 条无命中
        self._create_qa('q1', retrieval_hits=[1])
        self._create_qa('q2')

        result = coverage_service.analyze_domain_coverage(days=30)
        assert result['total_docs'] == 2
        assert result['total_queries'] == 2
        assert result['global_hit_rate'] == 0.5

        assert len(result['domain_coverage']) == 1
        domain = result['domain_coverage'][0]
        assert domain['name'] == '研发部'
        assert domain['doc_count'] == 2
        # chunk_count 经 distinct 计数 → 2（一个文档 2 片，另一个 0 片）
        assert domain['chunk_count'] == 2
        teams = dict(domain['teams'])
        # teams 值是 {team_id, doc_count, chunk_count} 字典，直接按键取值
        assert teams['平台组']['doc_count'] == 1
        assert teams['算法组']['doc_count'] == 1


# ============================================================================
# 反馈闭环自动化
# ============================================================================
@pytest.mark.django_db
class TestAutoLinkFeedbackToChunks(CoverageDBTestBase):
    """auto_link_feedback_to_chunks 测试"""

    def test_tag_triggered_link(self):
        """差评标签含'不准确'且 QA 有命中 chunk → 自动关联"""
        qa = self._create_qa('问题', retrieval_hits=[11, 22])
        QaFeedback.objects.create(
            qa_record=qa, user=self.user, rating=-1,
            tags=['不准确'], status='pending')
        result = coverage_service.auto_link_feedback_to_chunks(days=7)
        assert result['total_bad_feedbacks'] == 1
        assert result['linked_count'] == 1
        issue = result['issue_chunks'][0]
        assert issue['chunk_ids'] == [11, 22]
        assert '切片' in issue['suggestion']

    def test_long_comment_triggers_link(self):
        """评论超 20 字即使无标签也触发关联"""
        qa = self._create_qa('问题', retrieval_hits=[5])
        QaFeedback.objects.create(
            qa_record=qa, user=self.user, rating=-1,
            tags=[], comment='这个回答非常不准确且完全无法解决我的实际问题', status='pending')
        result = coverage_service.auto_link_feedback_to_chunks(days=7)
        assert result['linked_count'] == 1

    def test_no_hits_not_linked(self):
        """差评但 QA 无命中 chunk → 不关联，仅计入总数"""
        qa = self._create_qa('问题')
        QaFeedback.objects.create(
            qa_record=qa, user=self.user, rating=-1,
            tags=['不准确'], status='pending')
        result = coverage_service.auto_link_feedback_to_chunks(days=7)
        assert result['total_bad_feedbacks'] == 1
        assert result['linked_count'] == 0


# ============================================================================
# 覆盖率报告生成
# ============================================================================
@pytest.mark.django_db
class TestGenerateCoverageReport(CoverageDBTestBase):
    """generate_coverage_report 测试"""

    def test_report_aggregation(self):
        """mock 子分析后汇总各指标落 CoverageReport（update_or_create 单行）"""
        from apps.analytics.models import CoverageReport
        fake_coverage = {
            'total_hot_queries': 10, 'covered_queries': 8,
            'uncovered_queries': 2, 'hot_query_coverage_rate': 0.8,
        }
        with patch.object(coverage_service, 'analyze_hot_query_coverage', return_value=fake_coverage), \
             patch.object(coverage_service, 'detect_knowledge_gaps', return_value=[{'query': 'g', 'count': 3, 'suggestion': 's'}]), \
             patch.object(coverage_service, 'detect_duplicate_chunks', return_value={'duplicate_rate': 0.1, 'duplicate_count': 2}), \
             patch.object(coverage_service, 'analyze_domain_coverage', return_value={'domain_coverage': [{'name': '研发部'}]}), \
             patch.object(coverage_service, 'auto_link_feedback_to_chunks', return_value={'linked_count': 5}):
            report = coverage_service.generate_coverage_report(days=7)

        assert report.total_hot_queries == 10
        assert report.covered_queries == 8
        assert report.hot_query_coverage_rate == 0.8
        assert report.gap_count == 1
        assert report.duplicate_chunk_rate == 0.1
        assert report.duplicate_chunk_count == 2
        assert report.domain_coverage == [{'name': '研发部'}]
        assert report.feedback_loop_count == 5
        # update_or_create：同一天重复生成复用同一行
        with patch.object(coverage_service, 'analyze_hot_query_coverage', return_value=fake_coverage), \
             patch.object(coverage_service, 'detect_knowledge_gaps', return_value=[]), \
             patch.object(coverage_service, 'detect_duplicate_chunks', return_value={'duplicate_rate': 0.0, 'duplicate_count': 0}), \
             patch.object(coverage_service, 'analyze_domain_coverage', return_value={'domain_coverage': []}), \
             patch.object(coverage_service, 'auto_link_feedback_to_chunks', return_value={'linked_count': 0}):
            again = coverage_service.generate_coverage_report(days=7)
        assert again.id == report.id
        assert CoverageReport.objects.count() == 1
