"""analytics urls - 包含 RAG 质量评估全套接口"""
from django.urls import path

from apps.analytics.views_coverage import (
    CoverageReportDetailView, CoverageReportExportView,
    CoverageReportListView, CoverageReportView,
    FeedbackLoopView, GenerateCoverageReportView,
)
from apps.analytics.views_dashboard import (
    BadFeedbackDetailView, BadFeedbackListView,
    DailyReportView, OrgUsageReportView, QaRecordView,
    QueueDepthView, RealtimeSnapshotView, SystemMetricsReportView,
    TrendReportView,
)
from apps.analytics.views_doc_quality import (
    DocumentQualityReportListView, DocumentQualityReportView,
    MultiDimensionScoreView, RunDocQualityEvalView, RunMultiDimEvalView,
)
from apps.analytics.views_eval_dashboard import (
    EvalDashboardLowScoreView, EvalDashboardOverviewView,
    EvalDashboardQaDetailView, EvalDashboardTrendView,
)
from apps.analytics.views_keywords import (
    ChunkClickLogView, KeywordFeedbackAggListView,
    KeywordFeedbackApplyView, KeywordWeightDetailView,
    KeywordWeightListView, RunFeedbackLoopView,
)
from apps.analytics.views_low_score import (
    LowScoreAnalysisDetailView, LowScoreAnalysisListView,
    LowScoreAnalysisStatsView, RunLowScoreAnalysisView,
)
from apps.analytics.views_offline_eval import (
    GoldenDatasetDetailView, GoldenDatasetExportView,
    GoldenDatasetImportView, GoldenDatasetListView,
    GoldenQuestionView, RetrievalReportListView, RunAnswerEvalView,
    RunRegressionEvalView, RunRetrievalEvalView, SiphonRegressionView,
)
from apps.analytics.views_route_wiki import (
    RouteAnalysisAggregateView, RouteAnalysisDashboardView,
    WikiQualityEvaluateView, WikiQualityListView,
)

urlpatterns = [
    # --- 关键词权重---
    path("keywords/", KeywordWeightListView.as_view()),
    path("keywords/<int:kw_id>/", KeywordWeightDetailView.as_view()),

    # --- 日报 & 趋势---
    path("daily/", DailyReportView.as_view()),
    path("trend/", TrendReportView.as_view()),
    path("qa-records/", QaRecordView.as_view()),

    # --- 差评反馈---
    path("bad-feedbacks/", BadFeedbackListView.as_view()),
    path("bad-feedbacks/<int:fb_id>/", BadFeedbackDetailView.as_view()),

    # --- 系统监控---
    path("system-metrics/", SystemMetricsReportView.as_view()),
    path("org-usage/", OrgUsageReportView.as_view()),
    path("queue-depth/", QueueDepthView.as_view()),
    path("realtime/", RealtimeSnapshotView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 黄金测试集管理
    # ========================================================================
    path("golden-datasets/", GoldenDatasetListView.as_view()),
    path("golden-datasets/<int:ds_id>/", GoldenDatasetDetailView.as_view()),
    path("golden-datasets/<int:ds_id>/import/", GoldenDatasetImportView.as_view()),
    path("golden-datasets/<int:ds_id>/export/", GoldenDatasetExportView.as_view()),
    path("golden-datasets/<int:ds_id>/questions/", GoldenQuestionView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 低分回归测试集(沉淀 + 全链路评估)
    # ========================================================================
    # 手动触发沉淀(同步,从生产低分对话取 top N 加入回归测试集)
    path("regression/siphon/", SiphonRegressionView.as_view()),
    # 手动触发全链路评估(异步,检索→生成→12 维,更新 pass_count)
    path("regression/eval/", RunRegressionEvalView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 离线评估执行
    # ========================================================================
    path("eval/retrieval/", RunRetrievalEvalView.as_view()),
    path("eval/answer/", RunAnswerEvalView.as_view()),
    path("eval/retrieval-reports/", RetrievalReportListView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 文档质量
    # ========================================================================
    path("doc-quality/", DocumentQualityReportView.as_view()),
    path("doc-quality/evaluate/", RunDocQualityEvalView.as_view()),
    path("doc-quality/reports/", DocumentQualityReportListView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 多维度评估
    # ========================================================================
    path("multi-dim-scores/", MultiDimensionScoreView.as_view()),
    path("multi-dim-eval/", RunMultiDimEvalView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 评估看板(DeepEval 12 维生产评估结果展示)
    # ========================================================================
    path("eval-dashboard/overview/", EvalDashboardOverviewView.as_view()),
    path("eval-dashboard/trend/", EvalDashboardTrendView.as_view()),
    path("eval-dashboard/low-score-qa/", EvalDashboardLowScoreView.as_view()),
    path("eval-dashboard/qa-detail/", EvalDashboardQaDetailView.as_view()),
    # 路由分析看板（四层命中率 + 各维均分对比，数据由每日聚合任务供数）
    path("eval-dashboard/route-analysis/", RouteAnalysisDashboardView.as_view()),
    # 手动触发路由分析聚合（可选 report_date 回补指定日期）
    path("route-analysis/aggregate/", RouteAnalysisAggregateView.as_view()),

    # ========================================================================
    # RAG 质量评估 - Wiki 页面质量（忠实度/完整性）
    # ========================================================================
    path("wiki-quality/", WikiQualityListView.as_view()),
    path("wiki-quality/evaluate/", WikiQualityEvaluateView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 覆盖率 & 反馈闭环
    # ========================================================================
    path("coverage/", CoverageReportView.as_view()),
    path("coverage/generate/", GenerateCoverageReportView.as_view()),
    path("coverage/reports/", CoverageReportListView.as_view()),
    path("coverage/reports/<int:report_id>/", CoverageReportDetailView.as_view()),
    path("coverage/reports/<int:report_id>/export/", CoverageReportExportView.as_view()),
    path("feedback-loop/", FeedbackLoopView.as_view()),
    # --- 检索反馈闭环自动化（点击埋点 + 聚合记录 + 人工复核 + 手动触发）---
    path("chunk-clicks/", ChunkClickLogView.as_view()),
    path("feedback-loop/aggregations/", KeywordFeedbackAggListView.as_view()),
    path("feedback-loop/apply/", KeywordFeedbackApplyView.as_view()),
    path("feedback-loop/run/", RunFeedbackLoopView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 低分对话归因分析
    # ========================================================================
    # 列表(支持 days/category/layer/status/root_type/limit 筛选)
    path("low-score-analysis/", LowScoreAnalysisListView.as_view()),
    # 单条详情(按 qa_record_id 查询,返回完整对话 + 归因结论 + 建议)
    path("low-score-analysis/detail/", LowScoreAnalysisDetailView.as_view()),
    # 手动触发归因(POST,异步执行,前端轮询 detail 查结果)
    path("low-score-analysis/run/", RunLowScoreAnalysisView.as_view()),
    # 归因分类统计(前端归因分布图用,一次 GROUP BY 拿全)
    path("low-score-analysis/stats/", LowScoreAnalysisStatsView.as_view()),
]
