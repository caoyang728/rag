"""analytics urls - 包含 RAG 质量评估全套接口"""
from django.urls import path
from apps.analytics import views

urlpatterns = [
    # --- 关键词权重---
    path("keywords/", views.KeywordWeightListView.as_view()),
    path("keywords/<int:kw_id>/", views.KeywordWeightDetailView.as_view()),

    # --- 日报 & 趋势---
    path("daily/", views.DailyReportView.as_view()),
    path("trend/", views.TrendReportView.as_view()),
    path("overview/", views.OverviewStatsView.as_view()),
    path("qa-records/", views.QaRecordView.as_view()),

    # --- 差评反馈---
    path("bad-feedbacks/", views.BadFeedbackListView.as_view()),
    path("bad-feedbacks/<int:fb_id>/", views.BadFeedbackDetailView.as_view()),

    # --- 系统监控---
    path("system-metrics/", views.SystemMetricsReportView.as_view()),
    path("org-usage/", views.OrgUsageReportView.as_view()),
    path("queue-depth/", views.QueueDepthView.as_view()),
    path("realtime/", views.RealtimeSnapshotView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 黄金测试集管理
    # ========================================================================
    path("golden-datasets/", views.GoldenDatasetListView.as_view()),
    path("golden-datasets/<int:ds_id>/", views.GoldenDatasetDetailView.as_view()),
    path("golden-datasets/<int:ds_id>/import/", views.GoldenDatasetImportView.as_view()),
    path("golden-datasets/<int:ds_id>/export/", views.GoldenDatasetExportView.as_view()),
    path("golden-datasets/<int:ds_id>/questions/", views.GoldenQuestionView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 低分回归测试集(沉淀 + 全链路评估)
    # ========================================================================
    # 手动触发沉淀(同步,从生产低分对话取 top N 加入回归测试集)
    path("regression/siphon/", views.SiphonRegressionView.as_view()),
    # 手动触发全链路评估(异步,检索→生成→12 维,更新 pass_count)
    path("regression/eval/", views.RunRegressionEvalView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 离线评估执行
    # ========================================================================
    path("eval/retrieval/", views.RunRetrievalEvalView.as_view()),
    path("eval/answer/", views.RunAnswerEvalView.as_view()),
    path("eval/retrieval-reports/", views.RetrievalReportListView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 文档质量
    # ========================================================================
    path("doc-quality/", views.DocumentQualityReportView.as_view()),
    path("doc-quality/evaluate/", views.RunDocQualityEvalView.as_view()),
    path("doc-quality/reports/", views.DocumentQualityReportListView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 多维度评估
    # ========================================================================
    path("multi-dim-scores/", views.MultiDimensionScoreView.as_view()),
    path("multi-dim-eval/", views.RunMultiDimEvalView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 评估看板(DeepEval 12 维生产评估结果展示)
    # ========================================================================
    path("eval-dashboard/overview/", views.EvalDashboardOverviewView.as_view()),
    path("eval-dashboard/trend/", views.EvalDashboardTrendView.as_view()),
    path("eval-dashboard/low-score-qa/", views.EvalDashboardLowScoreView.as_view()),
    path("eval-dashboard/qa-detail/", views.EvalDashboardQaDetailView.as_view()),
    # 路由分析看板（四层命中率 + 各维均分对比，数据由每日聚合任务供数）
    path("eval-dashboard/route-analysis/", views.RouteAnalysisDashboardView.as_view()),
    # 手动触发路由分析聚合（可选 report_date 回补指定日期）
    path("route-analysis/aggregate/", views.RouteAnalysisAggregateView.as_view()),

    # ========================================================================
    # RAG 质量评估 - Wiki 页面质量（忠实度/完整性）
    # ========================================================================
    path("wiki-quality/", views.WikiQualityListView.as_view()),
    path("wiki-quality/evaluate/", views.WikiQualityEvaluateView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 覆盖率 & 反馈闭环
    # ========================================================================
    path("coverage/", views.CoverageReportView.as_view()),
    path("coverage/generate/", views.GenerateCoverageReportView.as_view()),
    path("coverage/reports/", views.CoverageReportListView.as_view()),
    path("coverage/reports/<int:report_id>/", views.CoverageReportDetailView.as_view()),
    path("coverage/reports/<int:report_id>/export/", views.CoverageReportExportView.as_view()),
    path("feedback-loop/", views.FeedbackLoopView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 低分对话归因分析
    # ========================================================================
    # 列表(支持 days/category/layer/status/root_type/limit 筛选)
    path("low-score-analysis/", views.LowScoreAnalysisListView.as_view()),
    # 单条详情(按 qa_record_id 查询,返回完整对话 + 归因结论 + 建议)
    path("low-score-analysis/detail/", views.LowScoreAnalysisDetailView.as_view()),
    # 手动触发归因(POST,异步执行,前端轮询 detail 查结果)
    path("low-score-analysis/run/", views.RunLowScoreAnalysisView.as_view()),
    # 归因分类统计(前端归因分布图用,一次 GROUP BY 拿全)
    path("low-score-analysis/stats/", views.LowScoreAnalysisStatsView.as_view()),
]