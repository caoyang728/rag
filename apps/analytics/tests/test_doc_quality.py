"""
apps.analytics.doc_quality 单元测试 —— 文档解析/切分/向量化质量评估

覆盖范围：
- _calc_parse_score / _calc_chunk_score / _calc_embed_score：三项质量分计算（含上下界钳制）
- _estimate_expected_chars：OSS 路径 / 文件不存在 / 真实文件三种估算分支
- _collect_quality_issues：低提取率 / 切片过小过大 / 低向量化成功率的告警与错误分级
- evaluate_document_quality（DB）：文档不存在抛 ValueError / 无切片走 failed /
  正常路径综合评分与 update_or_create 语义 / partial 解析状态
- batch_evaluate_document_quality（DB）：批量汇总与单文档失败降级
- get_document_quality_summary（DB）：评分分布 / 常见问题 severity 聚合 / 空数据

说明：DocumentVector 是 pgvector 模型（向量字段难以在测试中构造真实数据），
统一在源模块导入处 mock 其 count()，避免依赖真实向量数据。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.analytics import doc_quality
from apps.users.models import User
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk


# ============================================================================
# 纯函数：三项质量分计算
# ============================================================================
class TestCalcScores:
    """解析/切分/向量化质量分计算测试"""

    @pytest.mark.unit
    def test_calc_parse_score(self):
        """解析分 = 提取率 * 100，上限 100（提取率超过 1.0 时钳制）"""
        assert doc_quality._calc_parse_score(0.0, None) == 0.0
        assert doc_quality._calc_parse_score(0.8, None) == 80.0
        assert doc_quality._calc_parse_score(1.0, None) == 100.0
        assert doc_quality._calc_parse_score(1.5, None) == 100.0

    @pytest.mark.unit
    def test_calc_chunk_score_empty(self):
        """无切片 → 切分分 0（解析失败场景）"""
        assert doc_quality._calc_chunk_score([], 0) == 0.0

    @pytest.mark.unit
    def test_calc_chunk_score_ideal(self):
        """大小均匀且在 100-2000 合理区间 → 满分 100"""
        assert doc_quality._calc_chunk_score([400, 500, 600], 3) == 100.0

    @pytest.mark.unit
    def test_calc_chunk_score_small_deduction(self):
        """过小切片(<100 字符)每个扣 5 分"""
        # [50, 60] 两个过小切片 → 100 - 10 = 90（cv 约 0.13，不触发波动扣分）
        assert doc_quality._calc_chunk_score([50, 60], 2) == 90.0

    @pytest.mark.unit
    def test_calc_chunk_score_large_deduction(self):
        """过大切片(>2000 字符)每个扣 5 分"""
        assert doc_quality._calc_chunk_score([3000, 4000], 2) == 90.0

    @pytest.mark.unit
    def test_calc_chunk_score_high_variance_deduction(self):
        """切片大小波动大(cv>0.8)额外扣 20 分"""
        # [100,100,100,5000]：1 个过大 → -5；cv≈1.85 > 0.8 → -20 → 75
        assert doc_quality._calc_chunk_score([100, 100, 100, 5000], 4) == 75.0

    @pytest.mark.unit
    def test_calc_chunk_score_medium_variance_deduction(self):
        """cv 在 0.5~0.8 之间扣 10 分"""
        # [100, 300]：均值 200、std≈141、cv≈0.707 → -10 → 90
        assert doc_quality._calc_chunk_score([100, 300], 2) == 90.0

    @pytest.mark.unit
    def test_calc_chunk_score_floor_zero(self):
        """大量过小切片扣分可跌到 0 下限，不允许负数"""
        assert doc_quality._calc_chunk_score([1] * 30, 30) == 0.0

    @pytest.mark.unit
    def test_calc_embed_score(self):
        """向量化分 = 成功率 * 100"""
        assert doc_quality._calc_embed_score(0.0) == 0.0
        assert doc_quality._calc_embed_score(0.5) == 50.0
        assert doc_quality._calc_embed_score(1.0) == 100.0


# ============================================================================
# 纯函数：预期字符数估算
# ============================================================================
class TestEstimateExpectedChars:
    """_estimate_expected_chars 估算分支测试"""

    @pytest.mark.unit
    def test_oss_path_returns_default(self):
        """OSS 路径无法直接取文件大小 → 返回默认估算 5000"""
        assert doc_quality._estimate_expected_chars('oss://bucket/doc.pdf') == 5000

    @pytest.mark.unit
    def test_missing_file_returns_default(self):
        """本地文件不存在(OSError) → 回退默认 5000，不抛异常"""
        assert doc_quality._estimate_expected_chars('/no/such/file.pdf') == 5000

    @pytest.mark.unit
    def test_real_file_size_based(self, tmp_path):
        """真实文件按 size*0.7 估算可提取字符数"""
        p = tmp_path / 'doc.txt'
        p.write_text('x' * 100)
        assert doc_quality._estimate_expected_chars(str(p)) == 70


# ============================================================================
# 纯函数：质量问题收集
# ============================================================================
class TestCollectQualityIssues:
    """_collect_quality_issues 分级告警测试"""

    @pytest.mark.unit
    def test_low_extraction_error(self):
        """提取率 < 0.5 → error 级低提取告警"""
        issues = doc_quality._collect_quality_issues(text_extraction_rate=0.3)
        assert any(i['level'] == 'error' and i['type'] == 'low_extraction' for i in issues)

    @pytest.mark.unit
    def test_moderate_extraction_warning(self):
        """提取率 0.5~0.8 → warning 级提示检查解析器"""
        issues = doc_quality._collect_quality_issues(text_extraction_rate=0.6)
        assert any(i['level'] == 'warning' and i['type'] == 'moderate_extraction' for i in issues)

    @pytest.mark.unit
    def test_good_extraction_no_issue(self):
        """提取率 ≥ 0.8 不产生提取类问题"""
        issues = doc_quality._collect_quality_issues(text_extraction_rate=0.9)
        assert not any('extraction' in i['type'] for i in issues)

    @pytest.mark.unit
    def test_chunk_size_issues(self):
        """切片过小/过大分别产生对应 warning"""
        small = doc_quality._collect_quality_issues(avg_chunk_chars=50)
        assert any(i['type'] == 'too_small_chunks' for i in small)
        large = doc_quality._collect_quality_issues(avg_chunk_chars=3000)
        assert any(i['type'] == 'too_large_chunks' for i in large)

    @pytest.mark.unit
    def test_low_embed_rate_error(self):
        """向量化成功率 < 0.8 → error 级（部分切片无法被检索）"""
        issues = doc_quality._collect_quality_issues(embedding_success_rate=0.5)
        assert any(i['level'] == 'error' and i['type'] == 'low_embed_rate' for i in issues)

    @pytest.mark.unit
    def test_all_good_no_issues(self):
        """所有指标健康 → 空问题列表"""
        issues = doc_quality._collect_quality_issues(
            text_extraction_rate=0.9, avg_chunk_chars=500,
            embedding_success_rate=0.95)
        assert issues == []


# ============================================================================
# DB 测试：单文档质量评估
# ============================================================================
@pytest.mark.django_db
class TestEvaluateDocumentQuality:
    """evaluate_document_quality 主流程测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入文档所有者与节点

        yield 后统一停止 _patch_vector_count 启动的 mock，避免泄漏到其他测试
        （测试结束后统一停止 mock）。
        """
        self.owner = User.objects.create_user(
            username='doc_owner', password='pass12345', email='doc_owner@test.com')
        self.node = KnowledgeNode.objects.create(
            name='qa_root', node_type='root', root_type='test_root',
            created_by=self.owner)
        yield
        patcher = getattr(self, '_vector_patcher', None)
        if patcher is not None:
            patcher.stop()
            self._vector_patcher = None

    def _create_doc(self, file_path='', **kwargs):
        """创建测试文档（默认空 file_path → 预期字符数回退 5000）

        dept_id 必填：Document 有 doc_owner_scope_required CHECK 约束
        （team_id 或 dept_id 至少一个非空，无个人级文档）
        """
        defaults = dict(
            node=self.node, owner=self.owner, title='测试文档',
            file_name='test.txt', file_type='txt', file_hash='hash1',
            root_type='test_root', status='done', file_path=file_path,
            dept_id=1,
        )
        defaults.update(kwargs)
        return Document.objects.create(**defaults)

    def _patch_vector_count(self, count):
        """mock DocumentVector 的 count()（向量字段无法在测试中构造真实数据）

        patcher 存入 self._vector_patcher，由 _env fixture 在测试结束后统一 stop
        （测试结束后统一停止 mock）。
        """
        patcher = patch('apps.retrieval.models.DocumentVector')
        mock_vec = patcher.start()
        self._vector_patcher = patcher
        mock_vec.objects.filter.return_value.count.return_value = count
        return mock_vec

    def test_document_not_found_raises(self):
        """文档不存在 → 抛 ValueError"""
        with pytest.raises(ValueError, match='not found'):
            doc_quality.evaluate_document_quality(99999)

    def test_zero_chunks_marks_failed(self):
        """无切片 → 解析失败报告（parse_status='failed'，质量分 0）"""
        doc = self._create_doc()
        self._patch_vector_count(0)
        report = doc_quality.evaluate_document_quality(doc.id)
        report.refresh_from_db()
        assert report.parse_status == 'failed'
        assert report.parse_error_rate == 1.0
        assert report.quality_score == 0.0
        assert report.chunk_count == 0
        assert report.quality_issues[0]['type'] == 'no_chunks'

    def test_success_path_scoring(self):
        """正常路径：切片统计 + 向量化率 + 综合加权分 + 各字段落库"""
        doc = self._create_doc()
        for idx, content in enumerate(['A' * 400, 'B' * 500, 'C' * 600]):
            DocumentChunk.objects.create(
                document=doc, chunk_index=idx, content=content,
                chunk_type='table' if idx == 1 else 'text')
        self._patch_vector_count(3)

        report = doc_quality.evaluate_document_quality(doc.id)
        report.refresh_from_db()

        assert report.parse_status == 'success'
        assert report.chunk_count == 3
        assert report.table_chunk_count == 1
        assert report.text_extraction_chars == 1500
        assert report.avg_chunk_chars == 500
        assert report.min_chunk_chars == 400
        assert report.max_chunk_chars == 600
        assert report.embedding_success_rate == 1.0
        assert report.failed_chunk_count == 0
        # 综合分 = round(parse*0.4 + chunk*0.3 + embed*0.3, 1)
        # parse=30（1500/5000）、chunk=100、embed=100 → 12+30+30=72
        assert report.quality_score == 72.0

    def test_partial_extraction_status(self):
        """提取率 ≤ 0.1 → parse_status='partial'"""
        doc = self._create_doc()
        DocumentChunk.objects.create(
            document=doc, chunk_index=0, content='abc', chunk_type='text')
        self._patch_vector_count(1)
        report = doc_quality.evaluate_document_quality(doc.id)
        report.refresh_from_db()
        assert report.parse_status == 'partial'
        # 3/5000 = 0.0006
        assert report.text_extraction_rate == pytest.approx(0.0006)

    def test_update_or_create_reuses_row(self):
        """同一文档重复评估不产生新行（update_or_create 语义）"""
        doc = self._create_doc()
        DocumentChunk.objects.create(
            document=doc, chunk_index=0, content='A' * 300, chunk_type='text')
        self._patch_vector_count(1)
        first = doc_quality.evaluate_document_quality(doc.id)
        second = doc_quality.evaluate_document_quality(doc.id)
        assert first.id == second.id
        # 通过 ORM 计数验证仅一条报告（update_or_create 复用同一行）
        from apps.analytics.models import DocumentQualityReport
        assert DocumentQualityReport.objects.filter(document=doc).count() == 1


# ============================================================================
# DB 测试：批量文档质量评估
# ============================================================================
@pytest.mark.django_db
class TestBatchEvaluateDocumentQuality:
    """batch_evaluate_document_quality 汇总测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入文档所有者与节点"""
        self.owner = User.objects.create_user(
            username='batch_owner', password='pass12345', email='b@test.com')
        self.node = KnowledgeNode.objects.create(
            name='batch_root', node_type='root', root_type='test_root',
            created_by=self.owner)

    def _create_doc(self, name='doc'):
        """创建测试文档（dept_id 满足 doc_owner_scope_required CHECK 约束）"""
        return Document.objects.create(
            node=self.node, owner=self.owner, title=name, file_name=f'{name}.txt',
            file_type='txt', file_hash=name, root_type='test_root',
            status='done', dept_id=1)

    @patch('apps.retrieval.models.DocumentVector')
    def test_batch_summary(self, mock_vec):
        """批量评估汇总：total/evaluated/avg/min/max 计算正确"""
        mock_vec.objects.filter.return_value.count.return_value = 1
        doc_a = self._create_doc('a')
        doc_b = self._create_doc('b')
        # 不同切片长度产生不同质量分：a 偏小，b 正常
        DocumentChunk.objects.create(
            document=doc_a, chunk_index=0, content='X' * 50, chunk_type='text')
        DocumentChunk.objects.create(
            document=doc_b, chunk_index=0, content='Y' * 500, chunk_type='text')

        summary = doc_quality.batch_evaluate_document_quality(days=7)
        assert summary['total_documents'] == 2
        assert summary['evaluated'] == 2
        assert summary['failed'] == 0
        assert 0 < summary['avg_quality_score'] <= 100
        assert summary['min_score'] <= summary['max_score']

    @patch('apps.retrieval.models.DocumentVector')
    def test_batch_ignores_non_done_docs(self, mock_vec):
        """status != 'done' 的文档不参与批量评估"""
        doc_a = self._create_doc('a')
        doc_a.status = 'failed'
        doc_a.save()
        doc_b = self._create_doc('b')
        doc_b.status = 'pending'
        doc_b.save()
        summary = doc_quality.batch_evaluate_document_quality(days=7)
        assert summary['total_documents'] == 0

    def test_batch_failure_degraded(self):
        """单文档评估抛异常 → failed +1，不影响其他文档"""
        doc = self._create_doc('f')
        real = doc_quality.evaluate_document_quality

        def flaky(doc_id):
            if doc_id == doc.id:
                raise RuntimeError('boom')
            return real(doc_id)

        with patch.object(doc_quality, 'evaluate_document_quality', side_effect=flaky):
            summary = doc_quality.batch_evaluate_document_quality(days=7)
        assert summary['total_documents'] == 1
        assert summary['evaluated'] == 0
        assert summary['failed'] == 1
        assert summary['avg_quality_score'] == 0


# ============================================================================
# DB 测试：文档质量汇总查询
# ============================================================================
@pytest.mark.django_db
class TestGetDocumentQualitySummary:
    """get_document_quality_summary 汇总查询测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入所有者/节点/报告模型"""
        from apps.analytics.models import DocumentQualityReport
        self.owner = User.objects.create_user(
            username='summary_owner', password='pass12345', email='s@test.com')
        self.node = KnowledgeNode.objects.create(
            name='summary_root', node_type='root', root_type='test_root',
            created_by=self.owner)
        self.report_model = DocumentQualityReport
        # 文档文件名序号（保证同节点下 file_name 唯一）
        self._report_seq = 0

    def _create_report(self, score, issues=None, **doc_kw):
        # file_name 需唯一：同节点下 (node, file_name, version_tag) 有唯一约束，
        # 多次调用必须用不同文件名，否则 unique_doc_node_name_version 冲突
        idx = self._report_seq
        self._report_seq += 1
        doc = Document.objects.create(
            node=self.node, owner=self.owner, title='doc', file_name=f'd{idx}.txt',
            file_type='txt', file_hash=doc_kw.pop('file_hash', f'h{idx}'),
            root_type='test_root', status='done',
            dept_id=doc_kw.pop('dept_id', 1), **doc_kw)
        return self.report_model.objects.create(
            document=doc, quality_score=score, quality_issues=issues or [],
            parse_status='success')

    def test_distribution_and_avg(self):
        """评分分布四档与均值计算正确"""
        self._create_report(90)
        self._create_report(75)
        self._create_report(40)
        self._create_report(60)
        summary = doc_quality.get_document_quality_summary()
        assert summary['total_docs'] == 4
        assert summary['avg_score'] == pytest.approx(66.2, abs=0.1)  # (90+75+40+60)/4=66.25→66.2
        dist = summary['score_distribution']
        assert dist == {'excellent': 1, 'good': 1, 'fair': 1, 'poor': 1}

    def test_common_issues_severity_aggregation(self):
        """同类问题跨报告聚合计数，error 级别优先于 warning"""
        self._create_report(80, issues=[{'type': 'low_extraction', 'level': 'error'}])
        self._create_report(85, issues=[{'type': 'low_extraction', 'level': 'warning'}])
        self._create_report(90, issues=[{'type': 'low_embed_rate', 'level': 'error'}])
        summary = doc_quality.get_document_quality_summary()
        by_type = {i['type']: i for i in summary['common_issues']}
        assert by_type['low_extraction']['count'] == 2
        # error 级别映射为 high
        assert by_type['low_extraction']['severity'] == 'high'
        assert by_type['low_embed_rate']['severity'] == 'high'

    def test_org_filters(self):
        """按 team_id / dept_id 过滤报告"""
        self._create_report(90, dept_id=1, team_id=2)
        self._create_report(80, dept_id=1, team_id=3)
        self._create_report(70, dept_id=9, team_id=None, file_hash='h3')
        assert doc_quality.get_document_quality_summary(team_id=2)['total_docs'] == 1
        assert doc_quality.get_document_quality_summary(dept_id=1)['total_docs'] == 2
        assert doc_quality.get_document_quality_summary(dept_id=9)['total_docs'] == 1

    def test_empty_data(self):
        """无报告时返回空汇总，不抛 ZeroDivisionError"""
        summary = doc_quality.get_document_quality_summary()
        assert summary['total_docs'] == 0
        assert summary['avg_score'] == 0
        assert summary['score_distribution'] == {'excellent': 0, 'good': 0, 'fair': 0, 'poor': 0}
        assert summary['common_issues'] == []
