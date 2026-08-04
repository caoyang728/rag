"""
Agent app 数据模型
- AgentTrace: 记录 Agent 模式下每次工具调用的详细信息（工具名/参数/结果/耗时），
  为第二阶段端到端 Tracing 体系铺路，也供管理端展示 Agent 的"思考过程"。
"""
from django.conf import settings
from django.db import models


class AgentTrace(models.Model):
    """Agent 工具调用链记录

    每次 Agent 模式问答（auto/agent）中，每一轮工具调用都会创建一条记录。
    一条 QaRecord 可能关联多条 AgentTrace（多轮工具调用）。

    用途：
    1. 端到端 Tracing：定位 Agent 决策链路，分析工具调用是否合理
    2. 性能分析：统计各工具的平均耗时、成功率
    3. 故障排查：工具失败时查看参数和结果，定位根因
    4. 前端展示：管理端可展示 Agent 的"思考过程"
    """

    # 关联问答记录（QaRecord），on_delete=CASCADE 随问答记录一起删除
    qa_record = models.ForeignKey(
        'chat.QaRecord', on_delete=models.CASCADE, related_name='agent_traces',
        db_index=True, verbose_name='问答记录',
    )
    # 用户（冗余字段，便于按用户维度查询，不随 QaRecord 关联查询）
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, db_index=True, verbose_name='用户',
    )
    # 会话（冗余字段，便于按会话维度查询）
    session = models.ForeignKey(
        'memory.Session', on_delete=models.SET_NULL, null=True,
        blank=True, db_index=True, verbose_name='会话',
    )

    # 工具调用轮次（从 1 开始，同一轮可能调用多个工具）
    tool_round = models.IntegerField(default=1, verbose_name='调用轮次')
    # 工具名称（knowledge_search / web_search / calculator / text2sql）
    tool_name = models.CharField(max_length=50, db_index=True, verbose_name='工具名称')
    # 工具参数（LLM 生成的 arguments，JSON 格式）
    tool_args = models.JSONField(default=dict, blank=True, verbose_name='工具参数')
    # 工具执行结果（截断存储，避免过长；完整结果可从日志查看）
    tool_result = models.TextField(blank=True, default='', verbose_name='工具结果')
    # 工具调用 ID（OpenAI 协议的 tool_call_id，便于关联 LLM 的 tool_calls）
    call_id = models.CharField(max_length=100, blank=True, default='', verbose_name='调用ID')
    # 执行是否成功
    result_ok = models.BooleanField(default=True, verbose_name='是否成功')
    # 工具执行耗时（毫秒）
    latency_ms = models.IntegerField(default=0, verbose_name='耗时(ms)')
    # 创建时间
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='创建时间')

    class Meta:
        db_table = 'agent_trace'
        ordering = ['qa_record_id', 'tool_round', 'id']
        verbose_name = 'Agent调用链'
        verbose_name_plural = verbose_name
        # 索引：按问答记录 + 轮次查询（展示某次问答的完整工具调用链）
        indexes = [
            models.Index(fields=['qa_record_id', 'tool_round'], name='idx_agent_qa_round'),
        ]

    def __str__(self):
        return f'[{self.tool_name}] round={self.tool_round} ok={self.result_ok}'

    @classmethod
    def batch_create_from_traces(cls, qa_record, user, session, tool_traces: list):
        """批量创建工具调用链记录

        在 Agent 模式问答完成后调用，将 tool_traces 列表持久化。
        tool_traces 来自 react.py 的 agent_ask/agent_ask_stream 返回值。

        Args:
            qa_record: QaRecord 实例
            user: 用户实例
            session: Session 实例
            tool_traces: [{'round', 'call_id', 'tool_name', 'tool_args',
                          'result', 'ok', 'meta', 'latency_ms'}, ...]
        """
        if not tool_traces:
            return
        objs = []
        for t in tool_traces:
            result = t.get('result', '')
            # 截断过长的工具结果，避免存储膨胀
            if len(result) > 5000:
                result = result[:5000] + '\n...（结果已截断，完整内容见 celery 日志）'
            objs.append(cls(
                qa_record=qa_record,
                user=user,
                session=session,
                tool_round=t.get('round', 1),
                tool_name=t.get('tool_name', ''),
                tool_args=t.get('tool_args', {}),
                tool_result=result,
                call_id=t.get('call_id', ''),
                result_ok=t.get('ok', True),
                latency_ms=t.get('latency_ms', 0),
            ))
        try:
            cls.objects.bulk_create(objs, ignore_conflicts=False)
        except Exception:
            # 持久化失败不影响主流程，仅记录日志
            import logging
            logging.getLogger(__name__).exception(
                f'[AgentTrace] batch_create failed for qa_record={qa_record.id}')
