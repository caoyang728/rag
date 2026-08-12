#!/bin/bash
# =============================================================================
# 分批覆盖率测试脚本
# 背景：3100 个测试 + coverage 在 2GB 容器内单进程跑会 OOM。
# 方案：按 app 分批执行，每批独立进程（跑完释放内存），用 --cov-append 累积覆盖率。
#
# 用法：
#   docker compose -f docker-compose.core.yml exec django bash scripts/run_coverage_batch.sh
#   或在容器内直接执行：
#   bash scripts/run_coverage_batch.sh [选项]
#
# 选项：
#   --clean       清除旧覆盖率数据后重新开始（默认行为）
#   --append      追加到已有覆盖率数据
#   --app <name>  只跑指定 app（可多次指定）
#   --dry-run     只打印命令，不执行
# 说明：HTML 报告（static/coverage/，即 /coverage/ 页面）每次运行后都会重新生成，
#       不依赖选项（否则 --clean 删掉旧报告后页面会 404）。
#
# 输出：
#   - 每批日志：scripts/tmp/batch_<序号>_<名称>.log
#   - 汇总报告：scripts/tmp/coverage_report.md
# =============================================================================
set -euo pipefail

# --- 默认参数 ---
CLEAN=true
GENERATE_REPORT=false
DRY_RUN=false
TARGET_APPS=()

# --- 解析命令行参数 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --clean)  CLEAN=true; shift ;;
        --append) CLEAN=false; shift ;;
        --report) GENERATE_REPORT=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --app)    TARGET_APPS+=("$2"); shift 2 ;;
        *)        echo "未知参数: $1"; exit 1 ;;
    esac
done

# --- 分批定义 ---
# 每批包含 1~3 个 app，按测试量分组，控制单批总用例数在 ~200 以内
# 大 app 单独一批（users/analytics/knowledge/agent/system），小 app 合并
BATCHES=(
    # 批次 1：用户模块（核心：权限/信号/审计/工单）
    "apps/users/tests/test_ticket_service.py apps/users/tests/test_perm_cache.py apps/users/tests/test_signals.py apps/users/tests/test_audit_service.py apps/users/tests/test_permissions.py apps/users/tests/test_checks.py"
    # 批次 2：用户视图测试
    "apps/users/tests/test_views_user.py apps/users/tests/test_views_ticket.py apps/users/tests/test_views_rbac.py apps/users/tests/test_views_ticket_center.py apps/users/tests/test_views.py apps/users/tests/test_views_org.py apps/users/tests/test_views_auth.py apps/users/tests/test_views_base.py apps/users/tests/test_views_extra.py apps/users/tests/test_apps_config.py"
    # 批次 3：知识库（核心：文档/解析/权限/存储）
    "apps/knowledge/tests/test_views_document.py apps/knowledge/tests/test_pdf_parser.py apps/knowledge/tests/test_views_helpers.py apps/knowledge/tests/test_access.py apps/knowledge/tests/test_storage.py"
    # 批次 4：知识库（视图/同步/脱敏/其他解析器）
    "apps/knowledge/tests/test_views.py apps/knowledge/tests/test_views_audit.py apps/knowledge/tests/test_views_access.py apps/knowledge/tests/test_views_document_version.py apps/knowledge/tests/test_views_preview.py apps/knowledge/tests/test_node_sync.py apps/knowledge/tests/test_desensitizer.py apps/knowledge/tests/test_tasks.py apps/knowledge/tests/test_views_coverage.py apps/knowledge/tests/test_views_helpers_more.py"
    # 批次 5：知识库（剩余小文件合并）
    "apps/knowledge/tests/test_spreadsheet_parser.py apps/knowledge/tests/test_code_parser.py apps/knowledge/tests/test_chunker.py apps/knowledge/tests/test_markdown_parser.py apps/knowledge/tests/test_presentation_parser.py apps/knowledge/tests/test_config_parser.py apps/knowledge/tests/test_docx_parser.py apps/knowledge/tests/test_base_parser.py"
    # 批次 6：分析模块（核心：评估/看板/任务）
    "apps/analytics/tests/test_views.py apps/analytics/tests/test_views_dashboard.py apps/analytics/tests/test_views_eval.py apps/analytics/tests/test_tasks.py apps/analytics/tests/test_production_eval.py"
    # 批次 7：分析模块（低分/RAG/离线/实时等）
    "apps/analytics/tests/test_low_score_analyzer.py apps/analytics/tests/test_utils.py apps/analytics/tests/test_ragas_pipeline.py apps/analytics/tests/test_feedback_loop.py apps/analytics/tests/test_doc_quality.py apps/analytics/tests/test_offline_eval.py apps/analytics/tests/test_realtime.py apps/analytics/tests/test_models.py apps/analytics/tests/test_coverage.py"
    # 批次 8：分析模块（剩余小文件）
    "apps/analytics/tests/test_regression_eval.py apps/analytics/tests/test_deepeval_metrics.py apps/analytics/tests/test_serializers.py apps/analytics/tests/test_wiki_eval.py apps/analytics/tests/test_ragas_eval.py apps/analytics/tests/test_urls.py"
    # 批次 9：Agent 模块（前半）
    "apps/agent/tests/test_executor.py apps/agent/tests/test_tools_text2sql.py apps/agent/tests/test_tools_calculator.py apps/agent/tests/test_react.py apps/agent/tests/test_executor_stream.py apps/agent/tests/test_tools_base.py apps/agent/tests/test_views.py"
    # 批次 10：Agent 模块（后半）
    "apps/agent/tests/test_workflow_planner.py apps/agent/tests/test_tools_knowledge_search.py apps/agent/tests/test_tools_web_search.py apps/agent/tests/test_tools_wiki_graph.py apps/agent/tests/test_streamer.py apps/agent/tests/test_task_splitter.py apps/agent/tests/test_tools_init.py apps/agent/tests/test_models.py apps/agent/tests/test_workflow_engine.py apps/agent/tests/test_workflow_hitl.py"
    # 批次 11：系统模块
    "apps/system/tests/test_init_commands.py apps/system/tests/test_views_config.py apps/system/tests/test_scheduler_registry.py apps/system/tests/test_config_loader.py apps/system/tests/test_task_signals.py apps/system/tests/test_views.py apps/system/tests/test_views_tasks.py apps/system/tests/test_scheduler_views.py apps/system/tests/test_views_ops.py apps/system/tests/test_middleware.py apps/system/tests/test_views_extra.py"
    # 批次 12：检索模块
    "apps/retrieval/tests/test_profile.py apps/retrieval/tests/test_query_transform.py apps/retrieval/tests/test_hybrid.py apps/retrieval/tests/test_permission.py apps/retrieval/tests/test_bm25.py apps/retrieval/tests/test_vector_store.py apps/retrieval/tests/test_rerank.py apps/retrieval/tests/test_views.py"
    # 批次 13：安全模块
    "apps/security/tests/test_views.py apps/security/tests/test_sensitive_filter.py apps/security/tests/test_views_captcha.py apps/security/tests/test_views_middleware.py apps/security/tests/test_tasks.py apps/security/tests/test_middleware_unit.py"
    # 批次 14：记忆 + 图谱
    "apps/memory/tests/test_manager.py apps/memory/tests/test_views.py apps/memory/tests/test_parser.py apps/memory/tests/test_short_term.py apps/memory/tests/test_models.py apps/memory/tests/test_tasks.py apps/memory/tests/test_signals.py apps/graph/tests/test_extractor.py apps/graph/tests/test_views.py apps/graph/tests/test_community.py apps/graph/tests/test_sync.py apps/graph/tests/test_router.py apps/graph/tests/test_retriever.py apps/graph/tests/test_vector_search.py apps/graph/tests/test_tasks.py apps/graph/tests/test_embedding.py"
    # 批次 15：Wiki + LLM + 审计 + 通知 + Chat
    "apps/wiki/tests/test_views.py apps/wiki/tests/test_generator.py apps/wiki/tests/test_retriever.py apps/wiki/tests/test_tasks.py apps/wiki/tests/test_sync.py apps/wiki/tests/test_access.py apps/llm/tests/test_prompts.py apps/llm/tests/test_providers.py apps/llm/tests/test_embedding.py apps/llm/tests/test_factory.py apps/llm/tests/test_models.py apps/audit/tests/test_middleware.py apps/audit/tests/test_views.py apps/audit/tests/test_models.py apps/notification/tests/test_views.py apps/notification/tests/test_models.py apps/chat/tests/test_views.py apps/chat/tests/test_serializers.py apps/chat/tests/test_models.py"
)

BATCH_NAMES=(
    "users-core"
    "users-views"
    "knowledge-docs"
    "knowledge-views"
    "knowledge-parsers"
    "analytics-core"
    "analytics-eval"
    "analytics-extra"
    "agent-core"
    "agent-workflow"
    "system"
    "retrieval"
    "security"
    "memory+graph"
    "wiki+llm+audit+notification+chat"
)

# --- 日志目录 ---
LOG_DIR="scripts/tmp"
mkdir -p "$LOG_DIR"
REPORT_FILE="${LOG_DIR}/coverage_report.md"

# --- 清除旧覆盖率数据 ---
if [ "$CLEAN" = true ]; then
    echo ">>> 清除旧覆盖率数据和日志..."
    rm -f .coverage
    rm -rf static/coverage/
    rm -f "${LOG_DIR}"/batch_*.log
fi

# --- 执行函数 ---
# 返回值：0=全部通过，1=有失败
run_batch() {
    local batch_name="$1"
    local batch_files="$2"
    local batch_idx="$3"
    local log_file="${LOG_DIR}/batch_$(printf '%02d' ${batch_idx})_${batch_name}.log"

    echo ""
    echo "================================================================="
    echo ">>> 批次 [${batch_idx}]: ${batch_name}"
    echo ">>> 日志: ${log_file}"
    echo "================================================================="

    # 将文件路径转为 pytest 可接受的参数（空格分隔）
    local pytest_args=()
    for f in $batch_files; do
        pytest_args+=("$f")
    done

    if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] pytest --cov=apps --cov-append -v --tb=short --reuse-db ${pytest_args[*]}"
        return 0
    fi

    # 核心命令：
    # - 不用 -n auto（避免多 worker 占内存）
    # - --cov-append 累积覆盖率
    # - --reuse-db 复用测试库（避免每批重建）
    # - 输出同时写终端和日志文件（tee）
    local exit_code=0
    pytest \
        --cov=apps \
        --cov-append \
        --cov-config=.coveragerc \
        -v --tb=short --reuse-db \
        "${pytest_args[@]}" 2>&1 | tee "${log_file}" || exit_code=$?

    if [ $exit_code -ne 0 ]; then
        echo "!!! 批次 ${batch_name} 有测试失败（exit code: ${exit_code}），继续下一批..."
        return 1
    fi

    echo ">>> 批次 ${batch_name} 完成"
    return 0
}

# --- 生成 Markdown 报告 ---
generate_report() {
    local total_batches="$1"
    shift
    local batch_results=("$@")

    echo ">>> 生成汇总报告: ${REPORT_FILE}"

    {
        echo "# 覆盖率测试报告"
        echo ""
        echo "> 生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
        echo ""

        # ---- 1. 批次结果汇总 ----
        echo "## 1. 批次执行结果"
        echo ""
        echo "| 批次 | 名称 | 状态 | 日志文件 |"
        echo "|------|------|------|----------|"

        local fail_count=0
        for entry in "${batch_results[@]}"; do
            local idx="${entry%%|*}"
            local rest="${entry#*|}"
            local name="${rest%%|*}"
            local status="${rest#*|}"
            local log_file="batch_$(printf '%02d' ${idx})_${name}.log"
            local icon="PASSED"
            if [ "$status" = "FAIL" ]; then
                icon="FAILED"
                fail_count=$((fail_count + 1))
            fi
            echo "| ${idx} | ${name} | ${icon} | \`${log_file}\` |"
        done

        echo ""

        # ---- 2. 失败用例详情 ----
        echo "## 2. 失败用例详情"
        echo ""

        if [ $fail_count -eq 0 ]; then
            echo "全部通过，无失败用例。"
        else
            local has_failures=false
            for entry in "${batch_results[@]}"; do
                local idx="${entry%%|*}"
                local rest="${entry#*|}"
                local name="${rest%%|*}"
                local status="${rest#*|}"
                if [ "$status" = "FAIL" ]; then
                    local log_file="${LOG_DIR}/batch_$(printf '%02d' ${idx})_${name}.log"
                    if [ -f "$log_file" ]; then
                        # 提取 FAILED 行和短回溯
                        local failed_lines
                        failed_lines=$(grep -E "^FAILED|^apps/.*FAILED|short test summary|ERRORS" "$log_file" 2>/dev/null || true)
                        if [ -n "$failed_lines" ]; then
                            echo "### 批次 ${idx}: ${name}"
                            echo ""
                            echo '```'
                            echo "$failed_lines"
                            echo '```'
                            echo ""
                            has_failures=true
                        fi
                    fi
                fi
            done

            if [ "$has_failures" = false ]; then
                echo "有批次退出码非零，但未匹配到 FAILED 关键字（可能是导入错误或收集失败）。"
                echo ""
                echo "请检查对应日志文件获取详情。"
            fi
        fi

        # ---- 3. 覆盖率报告（按文件） ----
        echo ""
        echo "## 3. 各文件覆盖率"
        echo ""

        if [ -f .coverage ]; then
            # coverage report --show-missing 输出格式：
            # Name                              Stmts   Miss  Cover   Missing
            # ---------------------------------------------------------------
            # apps/chat/views.py                   100     20    80%   40-60
            echo '```'
            coverage report --show-missing 2>/dev/null || echo "(coverage report 生成失败，请确认 .coverage 文件完整)"
            echo '```'
        else
            echo ".coverage 文件不存在，未生成覆盖率报告。"
        fi

        # ---- 4. 低覆盖率文件（< 85%）----
        echo ""
        echo "## 4. 低覆盖率文件（< 85%）"
        echo ""

        if [ -f .coverage ]; then
            local low_cov
            low_cov=$(coverage report 2>/dev/null | grep -E "^\s*(apps/|TOTAL)" | awk '{
                for (i=1; i<=NF; i++) {
                    if ($i ~ /%$/) {
                        gsub(/%/, "", $i)
                        if ($i+0 < 85 && $1 != "TOTAL") {
                            print $0
                        }
                        break
                    }
                }
            }' || true)

            if [ -n "$low_cov" ]; then
                echo "| 文件 | 覆盖率 |"
                echo "|------|--------|"
                # 注意：不能用 local（管道子 shell 中报错），直接赋值即可
                echo "$low_cov" | while IFS= read -r line; do
                    file_name=$(echo "$line" | awk '{print $1}')
                    cov_pct=$(echo "$line" | grep -oE '[0-9]+%' | head -1)
                    echo "| \`${file_name}\` | ${cov_pct} |"
                done
            else
                echo "所有文件覆盖率均 >= 85%。"
            fi
        else
            echo "无法生成（.coverage 文件不存在）。"
        fi

        echo ""
        echo "---"
        echo ""
        echo "*报告由 \`scripts/run_coverage_batch.sh\` 自动生成*"

    } > "${REPORT_FILE}"

    echo ">>> 报告已写入: ${REPORT_FILE}"
}

# --- 主循环 ---
TOTAL=${#BATCHES[@]}
echo "================================================================="
echo "  分批覆盖率测试：共 ${TOTAL} 个批次"
echo "  模式: $([ "$CLEAN" = true ] && echo '清除旧数据重新开始' || echo '追加到已有数据')"
echo "  报告: ${REPORT_FILE}"
echo "================================================================="

# 记录每批结果：idx|name|status
BATCH_RESULTS=()

for i in "${!BATCHES[@]}"; do
    idx=$((i + 1))

    # 如果指定了 --app，只跑匹配的批次
    if [ ${#TARGET_APPS[@]} -gt 0 ]; then
        match=false
        for app in "${TARGET_APPS[@]}"; do
            if [[ "${BATCHES[$i]}" == *"${app}"* ]]; then
                match=true
                break
            fi
        done
        if [ "$match" = false ]; then
            continue
        fi
    fi

    echo ""
    echo "===================== [${idx}/${TOTAL}] ====================="

    batch_status="PASS"
    run_batch "${BATCH_NAMES[$i]}" "${BATCHES[$i]}" "${idx}" || batch_status="FAIL"
    BATCH_RESULTS+=("${idx}|${BATCH_NAMES[$i]}|${batch_status}")
done

# --- 生成报告（始终生成，不依赖 --report 参数）---
if [ "$DRY_RUN" = false ]; then
    generate_report "${#BATCH_RESULTS[@]}" "${BATCH_RESULTS[@]}"
fi

# --- 生成 HTML 报告（始终生成）---
# 注意：--clean 模式会删除 static/coverage/，若这里不重建，/coverage/ 页面会 404。
if [ "$DRY_RUN" = false ]; then
    echo ""
    echo ">>> 生成覆盖率 HTML 报告..."
    coverage html --directory=static/coverage/
    # 同步到 staticfiles/（生产环境 DEBUG=False 时 WhiteNoise 服务 STATIC_ROOT）
    if [ -d staticfiles ]; then
        rm -rf staticfiles/coverage
        cp -r static/coverage staticfiles/coverage
        echo ">>> 已同步到 staticfiles/coverage/"
    fi
    echo ">>> HTML 报告已生成: static/coverage/index.html"
fi

echo ""
echo "================================================================="
echo "  全部批次执行完毕！"
echo "  汇总报告: ${REPORT_FILE}"
echo "  查看覆盖率: coverage report"
echo "  覆盖率页面: /coverage/  (静态文件: static/coverage/)"
echo "================================================================="
