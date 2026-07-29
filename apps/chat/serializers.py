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
    class Meta:
        model = QaRecord
        fields = [
            "id", "uuid", "session_id", "turn_index", "question", "answer",
            "answer_type", "citations", "retrieval_hits",
            "latency_retrieval_ms", "latency_rerank_ms", "latency_llm_ms", "latency_total_ms",
            "latency_ttfb_ms",
            "tokens_prompt", "tokens_completion", "cost_estimate",
            "llm_provider", "llm_model", "is_hit_cache", "is_task_split",
            "created_at",
        ]


class QaFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = QaFeedback
        fields = ["id", "qa_record_id", "rating", "tags", "comment", "status", "created_at"]
        read_only_fields = ["status", "created_at"]
