"""chat serializers"""
from django.db.models import OuterRef, Subquery
from rest_framework import serializers
from apps.chat.models import QaRecord, QaFeedback
from apps.memory.models import Session


class SessionSerializer(serializers.ModelSerializer):
    preview = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Session
        fields = ["id", "title", "root_type", "is_archived", "turn_count",
                  "last_active_at", "created_at", "preview"]
        read_only_fields = ["turn_count", "last_active_at", "created_at"]

    def get_preview(self, obj):
        first_question = getattr(obj, '_first_question', None)
        if first_question:
            return first_question[:50] + '...' if len(first_question) > 50 else first_question
        return ''


class QaRecordSerializer(serializers.ModelSerializer):
    # Agent 模式工具调用链：从 AgentTrace 表关联读取
    # 配合视图层的 prefetch_related('agent_traces') 避免 N+1 查询
    tool_traces = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = QaRecord
        fields = [
            "id", "uuid", "session_id", "turn_index", "question", "answer",
            "answer_type", "citations", "retrieval_hits",
            "latency_retrieval_ms", "latency_rerank_ms", "latency_llm_ms", "latency_total_ms",
            "latency_ttfb_ms",
            "tokens_prompt", "tokens_completion", "cost_estimate",
            "llm_provider", "llm_model", "is_hit_cache", "is_task_split",
            "created_at", "tool_traces",
        ]

    def get_tool_traces(self, obj):
        """返回该问答记录的工具调用链
        依赖视图层 prefetch_related('agent_traces') 预取数据，避免 N+1 查询；
        若未 prefetch 则 fallback 到直接查询（仅单条详情场景会有性能损耗）。
        无工具调用时返回空列表（前端思考区自动隐藏）。
        """
        # Django prefetch_related 会将结果缓存到 obj._prefetched_objects_cache，
        # obj.agent_traces.all() 会自动命中缓存，无需额外处理
        traces = obj.agent_traces.all()
        return [
            {
                'tool_name': t.tool_name,
                'tool_args': t.tool_args,
                'tool_result': t.tool_result,
                'result_ok': t.result_ok,
                'latency_ms': t.latency_ms,
            }
            for t in traces
        ]


class QaFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = QaFeedback
        fields = ["id", "qa_record_id", "rating", "tags", "comment", "status", "created_at"]
        read_only_fields = ["status", "created_at"]
