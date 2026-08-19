"""
apps.analytics.views_common 单元测试 —— 组织筛选工具函数

覆盖范围：
- _parse_org_scope：从 query_params 解析 dept_id/team_id，含合法值、空值、非法值
- _apply_org_filter_on_qa：QaRecord 起点的 QuerySet 组织筛选（team 优先级高于 dept）
- _apply_org_filter_on_doc：Document 起点的 QuerySet 组织筛选
- 前缀规范化：qa_prefix 非空且不以 __ 结尾时自动补 __

三个函数均为纯逻辑，不依赖 DB，使用 @pytest.mark.unit。
"""
import pytest
from unittest.mock import MagicMock


# ============================================================================
# _parse_org_scope —— 解析 request.query_params 中的 dept_id / team_id
# ============================================================================
class TestParseOrgScope:
    """从 query_params 解析 dept_id 和 team_id"""

    @staticmethod
    def _make_request(**kwargs):
        """构造 mock request，query_params 由传入的 kwargs 模拟"""
        request = MagicMock()
        request.query_params = kwargs
        return request

    @pytest.mark.unit
    def test_valid_dept_id(self):
        """dept_id 为合法整数字符串 → 返回 int"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='42', team_id='')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id == 42
        assert team_id is None

    @pytest.mark.unit
    def test_valid_team_id(self):
        """team_id 为合法整数字符串 → 返回 int"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='', team_id='7')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id is None
        assert team_id == 7

    @pytest.mark.unit
    def test_both_valid(self):
        """dept_id 和 team_id 同时提供 → 两者均正确解析（由调用方决定优先级）"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='10', team_id='3')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id == 10
        assert team_id == 3

    @pytest.mark.unit
    def test_both_empty_returns_none(self):
        """两者均为空字符串 → 返回 (None, None)，调用方跳过组织过滤"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='', team_id='')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id is None
        assert team_id is None

    @pytest.mark.unit
    def test_both_absent_returns_none(self):
        """query_params 中不存在 dept_id/team_id → 默认空串 → 返回 (None, None)"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request()
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id is None
        assert team_id is None

    @pytest.mark.unit
    def test_invalid_dept_id_becomes_none(self):
        """dept_id 非数字 → int() 抛 ValueError → 降级为 None"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='abc', team_id='')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id is None
        assert team_id is None

    @pytest.mark.unit
    def test_invalid_team_id_becomes_none(self):
        """team_id 非数字 → int() 抛 ValueError → 降级为 None"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='', team_id='xyz')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id is None
        assert team_id is None

    @pytest.mark.unit
    def test_whitespace_stripped(self):
        """值前后有空格 → strip 后为空串 → 降级为 None"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='  ', team_id='  ')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id is None
        assert team_id is None

    @pytest.mark.unit
    def test_dept_id_with_whitespace_still_valid(self):
        """dept_id 带前后空格但内容为数字 → strip 后正确解析"""
        from apps.analytics.views_common import _parse_org_scope
        req = self._make_request(dept_id='  5  ', team_id='')
        dept_id, team_id = _parse_org_scope(req)
        assert dept_id == 5


# ============================================================================
# _apply_org_filter_on_qa —— QaRecord 起点的组织筛选
# ============================================================================
class TestApplyOrgFilterOnQA:
    """对 QaRecord（或 JOIN 后的表）应用组织筛选"""

    @pytest.mark.unit
    def test_team_id_filter(self):
        """team_id 有值时按 user__team_id 过滤（团队粒度最精确）"""
        from apps.analytics.views_common import _apply_org_filter_on_qa
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_qa(qs, dept_id=1, team_id=2)
        qs.filter.assert_called_once_with(user__team_id=2)
        assert result == 'filtered_qs'

    @pytest.mark.unit
    def test_dept_id_filter(self):
        """dept_id 有值且 team_id 为空时按 user__department_id 过滤"""
        from apps.analytics.views_common import _apply_org_filter_on_qa
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_qa(qs, dept_id=3, team_id=None)
        qs.filter.assert_called_once_with(user__department_id=3)
        assert result == 'filtered_qs'

    @pytest.mark.unit
    def test_no_filter_when_both_none(self):
        """dept_id 和 team_id 均为 None → 不调用 filter，直接返回原 qs"""
        from apps.analytics.views_common import _apply_org_filter_on_qa
        qs = MagicMock()
        result = _apply_org_filter_on_qa(qs, dept_id=None, team_id=None)
        qs.filter.assert_not_called()
        assert result is qs

    @pytest.mark.unit
    def test_team_takes_priority_over_dept(self):
        """team_id 和 dept_id 同时有值 → 只按 team_id 过滤（团队天然属于某部门，更精确）"""
        from apps.analytics.views_common import _apply_org_filter_on_qa
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_qa(qs, dept_id=10, team_id=20)
        qs.filter.assert_called_once_with(user__team_id=20)

    @pytest.mark.unit
    def test_with_qa_prefix(self):
        """qa_prefix='qa_record'（无 __ 结尾）→ 自动补 __ 后拼接 user__"""
        from apps.analytics.views_common import _apply_org_filter_on_qa
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_qa(qs, dept_id=None, team_id=5, qa_prefix='qa_record')
        qs.filter.assert_called_once_with(qa_record__user__team_id=5)

    @pytest.mark.unit
    def test_with_qa_prefix_already_has_dunders(self):
        """qa_prefix='qa_record__'（已有 __ 结尾）→ 不重复补 __"""
        from apps.analytics.views_common import _apply_org_filter_on_qa
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_qa(qs, dept_id=8, team_id=None, qa_prefix='qa_record__')
        qs.filter.assert_called_once_with(qa_record__user__department_id=8)

    @pytest.mark.unit
    def test_empty_prefix_no_change(self):
        """qa_prefix='' 时 base 直接为 user__，QaRecord 本身起点"""
        from apps.analytics.views_common import _apply_org_filter_on_qa
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_qa(qs, dept_id=None, team_id=99, qa_prefix='')
        qs.filter.assert_called_once_with(user__team_id=99)


# ============================================================================
# _apply_org_filter_on_doc —— Document 起点的组织筛选
# ============================================================================
class TestApplyOrgFilterOnDoc:
    """对 Document（或 JOIN 后的表）应用组织筛选"""

    @pytest.mark.unit
    def test_team_id_filter(self):
        """team_id 有值时按 {prefix}team_id 过滤"""
        from apps.analytics.views_common import _apply_org_filter_on_doc
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_doc(qs, dept_id=1, team_id=2)
        qs.filter.assert_called_once_with(team_id=2)
        assert result == 'filtered_qs'

    @pytest.mark.unit
    def test_dept_id_filter(self):
        """dept_id 有值且 team_id 为空时按 {prefix}dept_id 过滤"""
        from apps.analytics.views_common import _apply_org_filter_on_doc
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_doc(qs, dept_id=3, team_id=None)
        qs.filter.assert_called_once_with(dept_id=3)
        assert result == 'filtered_qs'

    @pytest.mark.unit
    def test_no_filter_when_both_none(self):
        """dept_id 和 team_id 均为 None → 不调用 filter，直接返回原 qs"""
        from apps.analytics.views_common import _apply_org_filter_on_doc
        qs = MagicMock()
        result = _apply_org_filter_on_doc(qs, dept_id=None, team_id=None)
        qs.filter.assert_not_called()
        assert result is qs

    @pytest.mark.unit
    def test_team_takes_priority_over_dept(self):
        """team_id 和 dept_id 同时有值 → 只按 team_id 过滤"""
        from apps.analytics.views_common import _apply_org_filter_on_doc
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_doc(qs, dept_id=10, team_id=20)
        qs.filter.assert_called_once_with(team_id=20)

    @pytest.mark.unit
    def test_with_doc_prefix(self):
        """doc_prefix='document' → 过滤条件为 document__team_id"""
        from apps.analytics.views_common import _apply_org_filter_on_doc
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_doc(qs, dept_id=None, team_id=5, doc_prefix='document__')
        qs.filter.assert_called_once_with(document__team_id=5)

    @pytest.mark.unit
    def test_with_doc_prefix_for_dept(self):
        """doc_prefix='related_doc__' + dept_id → 过滤条件为 related_doc__dept_id"""
        from apps.analytics.views_common import _apply_org_filter_on_doc
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_doc(qs, dept_id=8, team_id=None, doc_prefix='related_doc__')
        qs.filter.assert_called_once_with(related_doc__dept_id=8)

    @pytest.mark.unit
    def test_empty_prefix_no_change(self):
        """doc_prefix='' 时直接用 team_id/dept_id 作为字段名（Document 本身起点）"""
        from apps.analytics.views_common import _apply_org_filter_on_doc
        qs = MagicMock()
        qs.filter.return_value = 'filtered_qs'
        result = _apply_org_filter_on_doc(qs, dept_id=None, team_id=99, doc_prefix='')
        qs.filter.assert_called_once_with(team_id=99)
