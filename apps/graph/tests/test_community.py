"""
apps.graph.community 单元/集成测试 —— 社区检测与摘要生成

覆盖范围：
- build_graph：从 DB 构图（关系为边 + 孤立节点兜底）
- detect_communities：Louvain 算法 + level 粒度 + 空图降级
- generate_community_summary：LLM 摘要 + JSON 解析 + 代码块兼容 + 降级兜底
- run_community_detection：完整流程（检测 + 摘要 + 异常隔离 + 重建成功后清理旧数据）

测试分层：
- build_graph / detect_communities / run_community_detection：mock GraphRelation/build_graph，
  隔离 DB 依赖，专注验证图构建逻辑与控制流
- generate_community_summary：DB 集成测试，需要真实 GraphCommunity/
  GraphEntity/GraphRelation 实例以验证 LLM 摘要落库
"""
import json
import pytest
from unittest.mock import patch, MagicMock

import networkx as nx

from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity


# ============================================================================
# build_graph：mock GraphRelation，验证图构建
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.community.GraphRelation')
def test_build_graph_with_relations(mock_gr):
    """有关系时应正确添加边（含 weight/relation_type 属性）"""
    from apps.graph.community import build_graph

    mock_rel = MagicMock()
    mock_rel.source_entity_id = 1
    mock_rel.target_entity_id = 2
    mock_rel.weight = 1.5
    mock_rel.relation_type = '负责'

    mock_gr.objects.select_related.return_value.all.return_value = [mock_rel]
    # values_list 被调用两次（source_entity_id / target_entity_id），用 side_effect 分别返回
    mock_gr.objects.values_list.side_effect = [[1], [2]]

    G = build_graph()

    assert G.number_of_nodes() == 2
    assert G.number_of_edges() == 1
    assert G.has_edge(1, 2)
    assert G[1][2]['weight'] == 1.5
    assert G[1][2]['relation_type'] == '负责'


@pytest.mark.unit
@patch('apps.graph.community.GraphRelation')
def test_build_graph_empty(mock_gr):
    """无关系时应返回空图（0 节点 0 边）"""
    from apps.graph.community import build_graph

    mock_gr.objects.select_related.return_value.all.return_value = []
    mock_gr.objects.values_list.side_effect = [[], []]

    G = build_graph()

    assert G.number_of_nodes() == 0
    assert G.number_of_edges() == 0


@pytest.mark.unit
@patch('apps.graph.community.GraphRelation')
def test_build_graph_multiple_edges(mock_gr):
    """多条关系应全部添加为边"""
    from apps.graph.community import build_graph

    mock_rel1 = MagicMock(source_entity_id=1, target_entity_id=2,
                          weight=1.0, relation_type='认识')
    mock_rel2 = MagicMock(source_entity_id=2, target_entity_id=3,
                          weight=2.0, relation_type='合作')
    mock_gr.objects.select_related.return_value.all.return_value = [mock_rel1, mock_rel2]
    mock_gr.objects.values_list.side_effect = [[1, 2], [2, 3]]

    G = build_graph()

    assert G.number_of_edges() == 2
    assert G.has_edge(1, 2)
    assert G.has_edge(2, 3)


# ============================================================================
# detect_communities：mock build_graph，验证 Louvain 结果结构
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.community.build_graph')
def test_detect_communities_empty_graph(mock_build):
    """空图应返回空列表"""
    from apps.graph.community import detect_communities

    mock_build.return_value = nx.Graph()
    result = detect_communities(0)

    assert result == []


@pytest.mark.unit
@patch('apps.graph.community.build_graph')
def test_detect_communities_returns_structure(mock_build):
    """有节点的图应返回社区列表，每项包含 community_id/entity_ids/level"""
    from apps.graph.community import detect_communities

    G = nx.Graph()
    G.add_edge(1, 2)
    G.add_edge(3, 4)
    mock_build.return_value = G

    result = detect_communities(level=0)

    assert isinstance(result, list)
    assert len(result) >= 1
    for comm in result:
        assert 'community_id' in comm
        assert 'entity_ids' in comm
        assert 'level' in comm
        assert comm['level'] == 0
        assert isinstance(comm['entity_ids'], list)


@pytest.mark.unit
@patch('apps.graph.community.build_graph')
def test_detect_communities_level_passed(mock_build):
    """level 参数应透传到结果中"""
    from apps.graph.community import detect_communities

    G = nx.Graph()
    G.add_edge(1, 2)
    mock_build.return_value = G

    result = detect_communities(level=2)

    for comm in result:
        assert comm['level'] == 2


@pytest.mark.unit
@patch('apps.graph.community.build_graph')
def test_detect_communities_entity_ids_sorted(mock_build):
    """entity_ids 应为已排序列表（sorted(list(community))）"""
    from apps.graph.community import detect_communities

    G = nx.Graph()
    G.add_edge(5, 1)
    G.add_edge(3, 2)
    mock_build.return_value = G

    result = detect_communities(0)
    for comm in result:
        # 验证 entity_ids 是升序的
        ids = comm['entity_ids']
        assert ids == sorted(ids)


# ============================================================================
# generate_community_summary：DB 集成测试
# ============================================================================
@pytest.mark.django_db
class TestGenerateCommunitySummary:
    """generate_community_summary DB 集成测试

    需要真实 GraphCommunity/GraphEntity/GraphRelation 实例，
    以验证 LLM 返回的摘要正确落库（summary/keywords/metadata）。
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：构造最小社区（2 实体 + 1 关系）"""
        self.entity1 = GraphEntity.objects.create(
            name='张三', type='PERSON', description='研发部员工')
        self.entity2 = GraphEntity.objects.create(
            name='HR部门', type='ORG', description='人力资源部门')
        self.relation = GraphRelation.objects.create(
            source_entity=self.entity1,
            target_entity=self.entity2,
            relation_type='隶属于',
            weight=1.0,
        )
        self.community = GraphCommunity.objects.create(
            community_id=0, level=0,
            entity_ids=[self.entity1.id, self.entity2.id],
        )

    def _make_llm(self, content):
        """构造 mock LLM，chat 返回指定 content"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {'content': content}
        return mock_llm

    def test_summary_success_plain_json(self):
        """LLM 返回纯 JSON 时应正确解析并落库"""
        from apps.graph.community import generate_community_summary

        llm_response = json.dumps({
            'topic': '人事架构',
            'summary': '张三隶属于HR部门',
            'keywords': ['张三', 'HR'],
        })
        llm = self._make_llm(llm_response)

        result = generate_community_summary(self.community, llm)

        self.community.refresh_from_db()
        assert self.community.summary == '张三隶属于HR部门'
        assert '张三' in self.community.keywords
        assert 'HR' in self.community.keywords
        assert self.community.metadata.get('topic') == '人事架构'
        assert result == '张三隶属于HR部门'

    def test_summary_with_code_block(self):
        """LLM 返回 ```json 代码块时应正确提取 JSON"""
        from apps.graph.community import generate_community_summary

        content = '```json\n' + json.dumps({
            'topic': '代码块测试',
            'summary': '代码块摘要内容',
            'keywords': ['测试'],
        }) + '\n```'
        llm = self._make_llm(content)

        generate_community_summary(self.community, llm)

        self.community.refresh_from_db()
        assert self.community.summary == '代码块摘要内容'
        assert self.community.metadata.get('topic') == '代码块测试'

    def test_summary_with_plain_code_block(self):
        """LLM 返回 ``` 代码块（无 json 标记）也应正确提取"""
        from apps.graph.community import generate_community_summary

        content = '```\n' + json.dumps({
            'topic': '纯代码块',
            'summary': '纯代码块摘要',
            'keywords': [],
        }) + '\n```'
        llm = self._make_llm(content)

        generate_community_summary(self.community, llm)

        self.community.refresh_from_db()
        assert self.community.summary == '纯代码块摘要'

    def test_summary_malformed_json_degrades(self):
        """LLM 返回非 JSON 时应降级为原始文本摘要（前 500 字）"""
        from apps.graph.community import generate_community_summary

        llm = self._make_llm('这不是合法的JSON内容')
        result = generate_community_summary(self.community, llm)

        self.community.refresh_from_db()
        # 降级：summary = content[:500]
        assert self.community.summary == '这不是合法的JSON内容'
        assert result == '这不是合法的JSON内容'

    def test_summary_empty_content(self):
        """LLM 返回空 content 时应降级为空字符串摘要"""
        from apps.graph.community import generate_community_summary

        llm = self._make_llm('')
        result = generate_community_summary(self.community, llm)

        self.community.refresh_from_db()
        assert self.community.summary == ''

    def test_summary_partial_json_uses_defaults(self):
        """JSON 缺少部分字段时应使用默认值（summary 默认 content[:300]）"""
        from apps.graph.community import generate_community_summary

        # 缺少 summary 和 keywords
        content = json.dumps({'topic': '只有主题'})
        llm = self._make_llm(content)

        result = generate_community_summary(self.community, llm)

        self.community.refresh_from_db()
        # summary 默认 content[:300]
        assert self.community.summary == content
        # keywords 默认 []
        assert self.community.keywords == []
        assert self.community.metadata.get('topic') == '只有主题'

    def test_summary_llm_called_with_prompt(self):
        """应使用社区实体/关系信息构造 prompt 调用 LLM"""
        from apps.graph.community import generate_community_summary

        llm = self._make_llm('{"summary": "ok"}')
        generate_community_summary(self.community, llm)

        llm.chat.assert_called_once()
        call_args = llm.chat.call_args
        # 第一个位置参数是 messages 列表
        messages = call_args[0][0]
        assert len(messages) == 1
        assert messages[0]['role'] == 'user'
        # prompt 应包含实体名和关系类型
        prompt = messages[0]['content']
        assert '张三' in prompt
        assert 'HR部门' in prompt
        assert '隶属于' in prompt

    def test_summary_temperature_and_max_tokens(self):
        """LLM 调用应使用 temperature=0.3, max_tokens=1024"""
        from apps.graph.community import generate_community_summary

        llm = self._make_llm('{"summary": "ok"}')
        generate_community_summary(self.community, llm)

        call_kwargs = llm.chat.call_args[1]
        assert call_kwargs['temperature'] == 0.3
        assert call_kwargs['max_tokens'] == 1024


# ============================================================================
# run_community_detection：mock detect_communities + generate_community_summary
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.community.generate_community_summary')
@patch('apps.graph.community.detect_communities')
@patch('apps.graph.community.GraphCommunity')
def test_run_community_detection_creates_communities(mock_gc, mock_detect, mock_summary):
    """完整流程：清空旧社区 -> 检测 -> 创建 -> 摘要"""
    from apps.graph.community import run_community_detection

    mock_detect.return_value = [
        {'community_id': 0, 'entity_ids': [1, 2], 'level': 0},
        {'community_id': 1, 'entity_ids': [3], 'level': 0},
    ]
    mock_gc.objects.create.return_value = MagicMock(community_id=0, level=0)

    llm = MagicMock()
    count = run_community_detection(llm, levels=[0])

    assert count == 2
    # 新社区创建成功后，用 pk 排除法清理旧社区（失败时旧数据保留）
    mock_gc.objects.exclude.assert_called_once()
    mock_gc.objects.exclude.return_value.delete.assert_called_once()
    # 为每个社区创建记录
    assert mock_gc.objects.create.call_count == 2
    # 为每个社区生成摘要
    assert mock_summary.call_count == 2


@pytest.mark.unit
@patch('apps.graph.community.generate_community_summary')
@patch('apps.graph.community.detect_communities')
@patch('apps.graph.community.GraphCommunity')
def test_run_community_detection_default_levels(mock_gc, mock_detect, mock_summary):
    """levels=None 时应默认检测 [0, 1, 2] 三个层级"""
    from apps.graph.community import run_community_detection

    mock_detect.return_value = []
    llm = MagicMock()
    run_community_detection(llm)

    # detect_communities 应被调用 3 次（level 0, 1, 2）
    assert mock_detect.call_count == 3
    called_levels = [call.args[0] for call in mock_detect.call_args_list]
    assert called_levels == [0, 1, 2]


@pytest.mark.unit
@patch('apps.graph.community.generate_community_summary')
@patch('apps.graph.community.detect_communities')
@patch('apps.graph.community.GraphCommunity')
def test_run_community_detection_summary_error_isolated(mock_gc, mock_detect, mock_summary):
    """摘要生成失败不应阻断整体流程（异常隔离）"""
    from apps.graph.community import run_community_detection

    mock_detect.return_value = [
        {'community_id': 0, 'entity_ids': [1], 'level': 0},
        {'community_id': 1, 'entity_ids': [2], 'level': 0},
    ]
    # 第一个摘要抛异常，第二个正常
    mock_summary.side_effect = [Exception('LLM error'), None]
    mock_gc.objects.create.return_value = MagicMock(community_id=0, level=0)

    llm = MagicMock()
    count = run_community_detection(llm, levels=[0])

    # 两个社区都创建了（摘要失败不回滚社区创建）
    assert count == 2
    assert mock_summary.call_count == 2


@pytest.mark.unit
@patch('apps.graph.community.generate_community_summary')
@patch('apps.graph.community.detect_communities')
@patch('apps.graph.community.GraphCommunity')
def test_run_community_detection_empty_communities(mock_gc, mock_detect, mock_summary):
    """检测结果为空时应返回 0，不创建社区记录"""
    from apps.graph.community import run_community_detection

    mock_detect.return_value = []
    llm = MagicMock()
    count = run_community_detection(llm, levels=[0])

    assert count == 0
    mock_gc.objects.create.assert_not_called()
    mock_summary.assert_not_called()
    # 即使无新社区，也应清空旧社区（与旧行为一致）
    mock_gc.objects.exclude.assert_called_once()


@pytest.mark.unit
@patch('apps.graph.community.generate_community_summary')
@patch('apps.graph.community.detect_communities')
@patch('apps.graph.community.GraphCommunity')
def test_run_community_detection_creates_with_correct_fields(mock_gc, mock_detect, mock_summary):
    """社区记录应使用 detect_communities 返回的 community_id/level/entity_ids"""
    from apps.graph.community import run_community_detection

    comm_data = {'community_id': 5, 'entity_ids': [10, 20], 'level': 1}
    mock_detect.return_value = [comm_data]
    mock_gc.objects.create.return_value = MagicMock(community_id=5, level=1)

    llm = MagicMock()
    run_community_detection(llm, levels=[1])

    mock_gc.objects.create.assert_called_once_with(
        community_id=5, level=1, entity_ids=[10, 20],
    )


# ============================================================================
# run_community_detection 防丢失：旧社区重建成功后清理，中途失败时保留
# ============================================================================
@pytest.mark.integration
@pytest.mark.django_db
@patch('apps.graph.community.generate_community_summary')
def test_run_community_detection_keeps_old_data_when_detect_fails(mock_summary):
    """检测中途异常时旧社区应保留，避免社区列表被清空"""
    from apps.graph.community import run_community_detection

    old = GraphCommunity.objects.create(community_id=9, level=0, entity_ids=[1])
    with patch('apps.graph.community.detect_communities',
               side_effect=RuntimeError('boom')):
        with pytest.raises(RuntimeError):
            run_community_detection(MagicMock(), levels=[0])

    # 旧数据未被清空
    assert GraphCommunity.objects.filter(pk=old.pk).exists()
    assert GraphCommunity.objects.count() == 1


@pytest.mark.integration
@pytest.mark.django_db
@patch('apps.graph.community.generate_community_summary')
def test_run_community_detection_replaces_old_data_after_success(mock_summary):
    """重建成功后应删除旧社区，仅保留新社区"""
    from apps.graph.community import run_community_detection

    old = GraphCommunity.objects.create(community_id=99, level=0, entity_ids=[1])
    with patch('apps.graph.community.detect_communities', return_value=[
        {'community_id': 0, 'entity_ids': [1], 'level': 0},
    ]):
        count = run_community_detection(MagicMock(), levels=[0])

    assert count == 1
    assert GraphCommunity.objects.filter(pk=old.pk).exists() is False
    # 仅剩新社区
    assert GraphCommunity.objects.count() == 1
    assert GraphCommunity.objects.first().community_id == 0
