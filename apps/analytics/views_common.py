"""
analytics views - 共享工具函数（组织筛选）
"""

# ============================================================================
# 组织筛选工具函数
# ============================================================================

def _parse_org_scope(request):
    """从 request.query_params 解析 dept_id/team_id,返回 (dept_id, team_id)。

    team_id 有值时 dept_id 自动忽略(团队天然属于某部门,过滤更精确);
    dept_id 有值时用 user__department_id=dept_id 过滤(包含部门所有团队成员);
    两者都为空返回 (None, None),调用方跳过组织过滤。
    """
    dept_id = request.query_params.get('dept_id', '').strip() or None
    team_id = request.query_params.get('team_id', '').strip() or None
    if dept_id:
        try:
            dept_id = int(dept_id)
        except (ValueError, TypeError):
            dept_id = None
    if team_id:
        try:
            team_id = int(team_id)
        except (ValueError, TypeError):
            team_id = None
    return dept_id, team_id


def _apply_org_filter_on_qa(qs, dept_id, team_id, qa_prefix=''):
    """对以 QaRecord 为 JOIN 起点的 QuerySet 应用组织筛选(按提问用户归属)。

    qa_prefix: 当 QS 是 JOIN 后的表时(如 MultiDimensionScore),传入 qa 关联前缀
    (如 'qa_record__'),最终生成 qa_record__user__department_id。空串表示 qs 就是 QaRecord。
    前缀必须以 '__' 结尾(或为空串),否则会拼出错误的 ORM lookup 路径。
    """
    # 统一规范化前缀:非空时确保以 '__' 结尾,再拼接 'user__'
    base = (qa_prefix + '__') if (qa_prefix and not qa_prefix.endswith('__')) else qa_prefix
    base += 'user__'
    # team 有值时直接按团队过滤,更精确,无需再按部门过滤
    if team_id:
        return qs.filter(**{f'{base}team_id': team_id})
    if dept_id:
        return qs.filter(**{f'{base}department_id': dept_id})
    return qs


def _apply_org_filter_on_doc(qs, dept_id, team_id, doc_prefix=''):
    """对以 Document 为 JOIN 起点的 QuerySet 应用组织筛选(按文档 dept_id/team_id 归属)。

    doc_prefix: JOIN 前缀(如 'document__'),空串表示 qs 本身就是 Document。
    """
    # team 有值时直接按团队过滤(团队归属文档或冗余 dept_id 已对齐的团队文档)
    if team_id:
        return qs.filter(**{f'{doc_prefix}team_id': team_id})
    if dept_id:
        # 部门级:直接归属部门(dept_id=X,team_id 空)或其下属团队的文档(dept_id 冗余对齐)
        return qs.filter(**{f'{doc_prefix}dept_id': dept_id})
    return qs
