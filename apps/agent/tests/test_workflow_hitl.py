"""
HITL 模块测试：工作流人工确认工单的创建与恢复

覆盖：
- create_approval_ticket：biz_type=AGENT、reason 前缀 [agent:{wf_id}:approval]、
  审批链为 WORKFLOW_OWNER 单节点、工单号 AG 前缀
- resume_workflow_from_ticket：通过 → 恢复工作流；驳回 → 降级恢复；
  非等待审批状态 / 非 agent 工单 → 安全跳过

需要 DB（TicketList 工单创建），审批链构建 / 工单号生成 mock，
工作流恢复逻辑 mock（避免与 engine 集成测试重复）。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.users.models import TicketList, TicketStatus, TicketBizType
from apps.agent.models import AgentWorkflow
from apps.agent.workflow import hitl

pytestmark = pytest.mark.integration


def _make_workflow(test_user, db):
    """构造一个 waiting_approval 的工作流实例（不创建审批工单）"""
    from apps.memory.models import Session
    session = Session.objects.create(user=test_user, title='HITL测试')
    wf = AgentWorkflow.objects.create(
        user=test_user, session=session, question='测试问题',
        status='waiting_approval', max_nodes=10, max_duration_sec=300,
    )
    return wf


class TestCreateApprovalTicket:
    """create_approval_ticket：创建 HITL 审批工单"""

    def test_create_approval_ticket_then_workflow_owner_chain(self, test_user, db):
        """工单应为 AGENT 类型，审批链为 WORKFLOW_OWNER 单节点，reason 带指定前缀"""
        from apps.users import ticket_service
        wf = _make_workflow(test_user, db)
        node = {'id': 'ap1', 'name': '人工确认', 'type': 'approval', 'reason': '需要确认'}

        with patch.object(ticket_service, '_gen_ticket_no', return_value='AG20260001') as mock_no, \
             patch.object(ticket_service, '_log_flow') as mock_log:
            ticket = hitl.create_approval_ticket(wf, node, test_user)

        mock_no.assert_called_once_with(TicketBizType.AGENT)
        mock_log.assert_called_once()

        ticket.refresh_from_db()
        assert ticket.biz_type == TicketBizType.AGENT
        assert ticket.applicant_id == test_user.id
        assert ticket.status == TicketStatus.PENDING
        # 审批链：单节点 WORKFLOW_OWNER（发起人自助确认）
        assert ticket.approval_chain[0]['approver_role'] == ticket_service.ApproverRole.WORKFLOW_OWNER
        # 详情子表：workflow_id / node_id / reason 前缀约定
        detail = ticket.agent_approval_detail
        assert detail.workflow_id == wf.id
        assert detail.node_id == 'ap1'
        assert detail.reason == f'[agent:{wf.id}:approval] 需要确认'


class TestResumeWorkflowFromTicket:
    """resume_workflow_from_ticket：工单终态 → 恢复工作流"""

    def test_resume_when_ticket_executed_then_resume_approved(self, test_user, db):
        """审批通过（EXECUTED）→ 以 approved=True 恢复工作流"""
        wf = _make_workflow(test_user, db)
        detail = MagicMock(workflow_id=wf.id, node_id='ap1')
        ticket = MagicMock(agent_approval_detail=detail, status=TicketStatus.EXECUTED)

        with patch('apps.agent.workflow.engine.resume_workflow') as mock_resume:
            hitl.resume_workflow_from_ticket(ticket)

        mock_resume.assert_called_once_with(wf, node_id='ap1', approved=True)

    def test_resume_when_ticket_rejected_then_resume_rejected(self, test_user, db):
        """审批驳回（REJECTED）→ 以 approved=False 恢复工作流（降级）"""
        wf = _make_workflow(test_user, db)
        detail = MagicMock(workflow_id=wf.id, node_id='ap1')
        ticket = MagicMock(agent_approval_detail=detail, status=TicketStatus.REJECTED)

        with patch('apps.agent.workflow.engine.resume_workflow') as mock_resume:
            hitl.resume_workflow_from_ticket(ticket)

        mock_resume.assert_called_once_with(wf, node_id='ap1', approved=False)

    def test_resume_when_workflow_not_waiting_then_skip(self, test_user, db):
        """工作流不在 waiting_approval（如已超时/已完成）→ 安全跳过，不触发恢复"""
        wf = _make_workflow(test_user, db)
        wf.status = 'running'
        wf.save(update_fields=['status'])
        detail = MagicMock(workflow_id=wf.id, node_id='ap1')
        ticket = MagicMock(agent_approval_detail=detail, status=TicketStatus.EXECUTED)

        with patch('apps.agent.workflow.engine.resume_workflow') as mock_resume:
            hitl.resume_workflow_from_ticket(ticket)

        mock_resume.assert_not_called()

    def test_resume_when_no_detail_then_skip(self, test_user, db):
        """工单无 agent_approval_detail（非 HITL 工单）→ 记日志并安全跳过"""
        ticket = MagicMock(agent_approval_detail=None, status=TicketStatus.EXECUTED)

        with patch('apps.agent.workflow.engine.resume_workflow') as mock_resume:
            hitl.resume_workflow_from_ticket(ticket)

        mock_resume.assert_not_called()

    def test_resume_when_workflow_missing_then_skip(self, test_user, db):
        """工单指向的工作流不存在（已删除/被清理）→ 记日志并安全跳过"""
        detail = MagicMock(workflow_id=999999, node_id='ap1')
        ticket = MagicMock(agent_approval_detail=detail, status=TicketStatus.EXECUTED)

        with patch('apps.agent.workflow.engine.resume_workflow') as mock_resume:
            hitl.resume_workflow_from_ticket(ticket)

        mock_resume.assert_not_called()

    def test_resume_when_engine_raises_then_logged(self, test_user, db):
        """恢复引擎抛异常 → 仅记日志，异常不向上抛（审批结果已落库，业务不可丢）"""
        wf = _make_workflow(test_user, db)
        detail = MagicMock(workflow_id=wf.id, node_id='ap1')
        ticket = MagicMock(agent_approval_detail=detail, status=TicketStatus.EXECUTED)

        with patch('apps.agent.workflow.engine.resume_workflow',
                   side_effect=RuntimeError('engine boom')) as mock_resume:
            # 不应抛异常，恢复失败由日志兜底
            hitl.resume_workflow_from_ticket(ticket)

        mock_resume.assert_called_once_with(wf, node_id='ap1', approved=True)
