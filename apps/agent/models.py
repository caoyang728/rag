"""
Agent app 数据模型
- AgentTrace: 记录 Agent 模式下每次工具调用的详细信息（工具名/参数/结果/耗时），
  为第二阶段端到端 Tracing 体系铺路，也供管理端展示 Agent 的"思考过程"。
- AgentWorkflow / WorkflowNodeRun: 多 Agent 工作流执行实例与节点级执行轨迹，
  支持"编排器 + 执行 Agent"模式与 Human-in-the-Loop 人工确认节点。
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
    # 工具名称（knowledge_search / web_search / text2sql）
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


class AgentWorkflow(models.Model):
    """多 Agent 工作流执行实例

    编排器（LLM）把复杂问题拆成节点 DAG（research 子 Agent / tool 工具 / approval
    人工确认），由执行引擎按拓扑序运行；审批节点触发统一审批工单（HITL），
    审批通过前工作流停留在 waiting_approval，通过后继续、拒绝后降级。

    状态机：
      planning → running → succeeded / failed / degraded
                          ↘ waiting_approval →(审批通过)→ running → succeeded
      degraded：部分节点失败或被人工拒绝，最终基于已有结果降级回答
    """
    STATUS_CHOICES = [
        ('planning', '规划中'),
        ('running', '执行中'),
        ('waiting_approval', '等待人工确认'),
        ('succeeded', '成功'),
        ('failed', '失败'),
        ('degraded', '降级完成'),
    ]

    # 发起人与会话（冗余字段，便于按用户/会话维度查询）
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, db_index=True, verbose_name='用户',
    )
    session = models.ForeignKey(
        'memory.Session', on_delete=models.SET_NULL, null=True,
        blank=True, db_index=True, verbose_name='会话',
    )
    # 最终答案落库后的 QaRecord（等待审批阶段为空）
    qa_record = models.ForeignKey(
        'chat.QaRecord', on_delete=models.SET_NULL, null=True,
        blank=True, db_index=True, verbose_name='问答记录',
    )

    question = models.TextField(verbose_name='原始问题')
    # 编排器产出的节点 DAG 定义（[{id,name,type,depends_on,...}]，含引擎追加的审批/finalize 节点）
    definition = models.JSONField(default=list, blank=True, verbose_name='节点DAG定义')
    status = models.CharField(max_length=24, choices=STATUS_CHOICES,
                              default='planning', db_index=True, verbose_name='状态')
    # 执行上限（防失控）：最大节点数 + 总时长秒
    max_nodes = models.IntegerField(default=10, verbose_name='最大节点数')
    max_duration_sec = models.IntegerField(default=300, verbose_name='最大时长(秒)')

    # 最终结果快照（answer/citations/stats/qa_id），供工作流详情页直接展示
    result = models.JSONField(default=dict, blank=True, verbose_name='执行结果')
    error = models.TextField(blank=True, default='', verbose_name='错误信息')

    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'agent_workflow'
        ordering = ['-created_at']
        verbose_name = 'Agent工作流'
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=['user', '-created_at'], name='idx_wf_user_time'),
            models.Index(fields=['status', '-created_at'], name='idx_wf_status_time'),
            models.Index(fields=['qa_record'], name='idx_wf_qa'),
        ]

    def __str__(self):
        return f'Workflow<{self.id}>{self.status}'


class WorkflowNodeRun(models.Model):
    """工作流节点执行记录 —— 一次工作流执行的完整轨迹

    与 AgentWorkflow 一对多，记录每个节点的状态/输入/输出/耗时/错误，
    供"轨迹可查"（工作流详情 API 与前端节点可视化）与失败复盘。

    节点状态机：
      pending → running → succeeded / failed
                          ↘ blocked(等待人工确认) → approved / rejected
      skipped：依赖节点失败/被拒后不再执行
    """
    STATUS_CHOICES = [
        ('pending', '待执行'),
        ('running', '执行中'),
        ('succeeded', '成功'),
        ('failed', '失败'),
        ('blocked', '等待人工确认'),
        ('approved', '已批准'),
        ('rejected', '已拒绝'),
        ('skipped', '已跳过'),
    ]

    workflow = models.ForeignKey(
        AgentWorkflow, on_delete=models.CASCADE, related_name='node_runs',
        db_index=True, verbose_name='工作流',
    )
    node_id = models.CharField(max_length=64, verbose_name='节点ID')
    node_name = models.CharField(max_length=128, blank=True, default='', verbose_name='节点名称')
    # 节点类型：research(子Agent) / tool(工具) / approval(人工确认) / finalize(汇总)
    step_type = models.CharField(max_length=32, db_index=True, verbose_name='节点类型')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default='pending', db_index=True, verbose_name='状态')
    # 重试次数（从 1 开始，失败自动重试后递增）
    attempt = models.IntegerField(default=1, verbose_name='重试次数')
    # 节点输入（research=子问题 / tool=工具参数 / approval=确认理由）
    input = models.JSONField(default=dict, blank=True, verbose_name='输入')
    # 节点输出快照（{output, ok, meta:{citations/chunks/...}}）
    output = models.JSONField(default=dict, blank=True, verbose_name='输出')
    error = models.TextField(blank=True, default='', verbose_name='错误信息')
    # 审批节点关联的工单 ID（blocked 状态时有值）
    ticket_id = models.BigIntegerField(null=True, blank=True, verbose_name='审批工单ID')
    latency_ms = models.IntegerField(default=0, verbose_name='耗时(ms)')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'agent_workflow_node'
        ordering = ['id']
        verbose_name = '工作流节点'
        verbose_name_plural = verbose_name
        # 同一工作流内节点 ID 唯一（定义层已保证，DB 层兜底防重复）
        unique_together = [('workflow', 'node_id')]
        indexes = [
            models.Index(fields=['workflow', 'status'], name='idx_wfnode_wf_status'),
        ]

    def __str__(self):
        return f'Node<{self.node_id}>{self.step_type}:{self.status}'
