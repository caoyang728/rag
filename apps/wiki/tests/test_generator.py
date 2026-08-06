"""
apps.wiki.generator 单元测试 —— LLM Wiki 页面生成器

覆盖范围：
- generate_wiki_page：基于知识节点文档生成 Wiki 页面（source_info 组装 / llm.chat / update_or_create / embedding 同步）
- generate_community_wiki：基于图谱社区生成 Wiki 页面（entities/relations 组装 / llm.chat / update_or_create）
- _sync_wiki_embedding：embedding 同步（非零向量写入、零向量跳过、空向量跳过）
- 错误处理：节点不存在抛 DoesNotExist

用纯 mock（不依赖 DB）：
generate_wiki_page 内部 import KnowledgeNode/Document，generate_community_wiki 内部
import GraphCommunity，且依赖 LLM 与 embedding 外部服务。本测试聚焦分支逻辑
（source_info 组装、摘要截断、embedding 兜底），故统一 mock 模型查询链、
get_embedding_client、llm 实例，避免真实 DB / LLM / 向量服务耦合。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.wiki.generator import (
    generate_wiki_page,
    generate_community_wiki,
    _sync_wiki_embedding,
    MAX_DOC_TITLES,
    MAX_DOC_FOR_CHUNKS,
    MAX_CHUNKS_PER_DOC,
    MAX_CHUNK_PREVIEW_LEN,
)


# ----------------------------------------------------------------------------
# 辅助构造：文档 / 切片 mock，支持切片与迭代（filter 直接返回真实列表）
# ----------------------------------------------------------------------------
def _make_chunk(content='切片内容'):
    """构造文档切片 mock"""
    chunk = MagicMock()
    chunk.content = content
    return chunk


def _make_doc(title='文档标题', chunks=None):
    """构造文档 mock，chunks.all() 返回真实列表以支持切片迭代"""
    doc = MagicMock()
    doc.title = title
    doc.chunks.all.return_value = chunks if chunks is not None else []
    return doc


def _make_node(node_id=1, name='测试节点', path='/root/test', root_type='business'):
    """构造知识节点 mock"""
    node = MagicMock()
    node.id = node_id
    node.name = name
    node.path = path
    node.root_type = root_type
    return node


def _make_llm(content='## 生成的 Wiki 正文内容'):
    """构造 LLM 实例 mock，chat 返回固定 content"""
    llm = MagicMock()
    llm.chat.return_value = {'content': content}
    return llm


# ============================================================================
# generate_wiki_page —— 正常生成流程
# ============================================================================
class TestGenerateWikiPage:
    """generate_wiki_page 生成流程测试"""

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_generate_wiki_page_success(self, mock_kn, mock_doc_model,
                                        mock_get_embed, mock_wiki_page):
        """正常生成：节点 + 文档 -> llm.chat -> update_or_create -> 同步 embedding"""
        node = _make_node()
        mock_kn.objects.get.return_value = node
        mock_doc_model.objects.filter.return_value = [_make_doc('文档A')]

        page = MagicMock()
        page.id = 101
        page.title = '测试节点'
        mock_wiki_page.objects.update_or_create.return_value = (page, True)

        mock_get_embed.return_value.embed_one.return_value = [0.1, 0.2]

        result = generate_wiki_page(node_id=1, llm=_make_llm('生成内容'))

        # 应以 node 查询
        mock_kn.objects.get.assert_called_once_with(id=1)
        # 应创建/更新 WikiPage，挂载 node
        args, kwargs = mock_wiki_page.objects.update_or_create.call_args
        assert kwargs['node'] == node
        defaults = kwargs['defaults']
        assert defaults['title'] == '测试节点'
        assert defaults['content'] == '生成内容'
        assert defaults['status'] == 'published'
        assert defaults['tags'] == ['business']
        # 返回 update_or_create 的 page
        assert result is page

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_generate_wiki_page_llm_chat_params(self, mock_kn, mock_doc_model,
                                                mock_get_embed, mock_wiki_page):
        """llm.chat 应以 temperature=0.3、max_tokens=4096 调用"""
        mock_kn.objects.get.return_value = _make_node()
        mock_doc_model.objects.filter.return_value = []
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]  # 零向量跳过 embedding 写入

        llm = _make_llm()
        generate_wiki_page(node_id=1, llm=llm)

        llm.chat.assert_called_once()
        args, kwargs = llm.chat.call_args
        assert kwargs['temperature'] == 0.3
        assert kwargs['max_tokens'] == 4096
        # 第一参数为消息列表，单条 user 消息
        messages = args[0]
        assert messages[0]['role'] == 'user'
        assert 'prompt' in messages[0]['content'] or '内容' in messages[0]['content']

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_source_info_includes_node_and_docs(self, mock_kn, mock_doc_model,
                                                mock_get_embed, mock_wiki_page):
        """source_info 应包含节点名称、路径及关联文档标题"""
        node = _make_node(name='后端规范', path='/研发部/后端组/规范')
        mock_kn.objects.get.return_value = node
        mock_doc_model.objects.filter.return_value = [
            _make_doc('编码规范'), _make_doc('代码评审指南')]
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        llm = _make_llm()
        generate_wiki_page(node_id=1, llm=llm)

        # llm.chat 第一参数为消息列表，首条消息的 content 即 prompt
        prompt = llm.chat.call_args.args[0][0]['content']
        # prompt 中应含节点名与文档标题
        assert '后端规范' in prompt
        assert '编码规范' in prompt
        assert '代码评审指南' in prompt

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_source_info_includes_chunk_contents(self, mock_kn, mock_doc_model,
                                                 mock_get_embed, mock_wiki_page):
        """source_info 应包含文档切片内容（前 MAX_CHUNK_PREVIEW_LEN 字符）"""
        mock_kn.objects.get.return_value = _make_node()
        long_chunk = '切片正文' * 100  # 远超 300 字符
        mock_doc_model.objects.filter.return_value = [
            _make_doc('文档A', chunks=[_make_chunk(long_chunk)])]
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        llm = _make_llm()
        generate_wiki_page(node_id=1, llm=llm)

        prompt = llm.chat.call_args.args[0][0]['content']
        # 切片内容应被截断到 MAX_CHUNK_PREVIEW_LEN
        assert '切片正文' in prompt

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_summary_truncated_when_content_long(self, mock_kn, mock_doc_model,
                                                 mock_get_embed, mock_wiki_page):
        """content 长度 > 300 时 summary 应截断为 content[:300]"""
        mock_kn.objects.get.return_value = _make_node()
        mock_doc_model.objects.filter.return_value = []
        long_content = 'X' * 500
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        generate_wiki_page(node_id=1, llm=_make_llm(long_content))

        defaults = mock_wiki_page.objects.update_or_create.call_args.kwargs['defaults']
        assert defaults['summary'] == long_content[:300]

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_summary_full_when_content_short(self, mock_kn, mock_doc_model,
                                             mock_get_embed, mock_wiki_page):
        """content 长度 <= 300 时 summary 应等于 content 全文"""
        mock_kn.objects.get.return_value = _make_node()
        mock_doc_model.objects.filter.return_value = []
        short_content = '短内容'
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        generate_wiki_page(node_id=1, llm=_make_llm(short_content))

        defaults = mock_wiki_page.objects.update_or_create.call_args.kwargs['defaults']
        assert defaults['summary'] == short_content

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_no_docs_still_generates(self, mock_kn, mock_doc_model,
                                     mock_get_embed, mock_wiki_page):
        """节点下无已完成文档时仍应生成页面（source_info 仅含节点信息）"""
        mock_kn.objects.get.return_value = _make_node()
        mock_doc_model.objects.filter.return_value = []
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        generate_wiki_page(node_id=1, llm=_make_llm())

        # 仍应调用 update_or_create
        mock_wiki_page.objects.update_or_create.assert_called_once()


# ============================================================================
# generate_wiki_page —— 错误处理
# ============================================================================
class TestGenerateWikiPageErrors:
    """generate_wiki_page 错误处理测试"""

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_node_not_found_raises(self, mock_kn, mock_doc_model,
                                   mock_get_embed, mock_wiki_page):
        """节点不存在时 KnowledgeNode.objects.get 抛 DoesNotExist，应向上抛出"""
        from django.core.exceptions import ObjectDoesNotExist
        mock_kn.objects.get.side_effect = ObjectDoesNotExist()

        with pytest.raises(ObjectDoesNotExist):
            generate_wiki_page(node_id=999, llm=_make_llm())

        # 节点不存在时不应继续创建 WikiPage
        mock_wiki_page.objects.update_or_create.assert_not_called()

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.knowledge.models.Document')
    @patch('apps.knowledge.models.KnowledgeNode')
    def test_llm_response_empty_content(self, mock_kn, mock_doc_model,
                                        mock_get_embed, mock_wiki_page):
        """llm.chat 返回不含 content 时 content 取空串，仍写入 WikiPage"""
        mock_kn.objects.get.return_value = _make_node()
        mock_doc_model.objects.filter.return_value = []
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        llm = MagicMock()
        llm.chat.return_value = {}  # 无 content 字段

        generate_wiki_page(node_id=1, llm=llm)

        defaults = mock_wiki_page.objects.update_or_create.call_args.kwargs['defaults']
        assert defaults['content'] == ''


# ============================================================================
# generate_community_wiki —— 社区 Wiki 生成
# ============================================================================
class TestGenerateCommunityWiki:
    """generate_community_wiki 生成流程测试"""

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.wiki.generator.GraphRelation')
    @patch('apps.wiki.generator.GraphEntity')
    @patch('apps.graph.models.GraphCommunity')
    def test_generate_community_wiki_success(self, mock_community_model,
                                              mock_entity_model, mock_rel_model,
                                              mock_get_embed, mock_wiki_page):
        """正常生成社区 Wiki：社区 + 实体 + 关系 -> llm.chat -> update_or_create"""
        community = MagicMock()
        community.community_id = 5
        community.level = 1
        community.entity_ids = [1, 2]
        community.metadata = {'topic': '知识图谱领域'}
        community.summary = '社区摘要'
        community.keywords = ['图谱', '实体']
        mock_community_model.objects.get.return_value = community

        # 实体列表
        e1 = MagicMock()
        e1.name = '实体A'
        e1.get_type_display.return_value = '人物'
        e1.description = '实体A的描述说明'
        mock_entity_model.objects.filter.return_value = [e1]

        # 关系列表（select_related 后切片）
        r1 = MagicMock()
        r1.source_entity.name = '实体A'
        r1.relation_type = '负责'
        r1.target_entity.name = '实体B'
        mock_rel_model.objects.filter.return_value.select_related.return_value = [r1]

        page = MagicMock()
        page.id = 201
        mock_wiki_page.objects.update_or_create.return_value = (page, True)
        mock_get_embed.return_value.embed_one.return_value = [0.1]

        result = generate_community_wiki(community_id=5, level=1, llm=_make_llm('社区内容'))

        # 查询社区按 community_id + level
        mock_community_model.objects.get.assert_called_once_with(community_id=5, level=1)
        # update_or_create 挂载 community
        kwargs = mock_wiki_page.objects.update_or_create.call_args.kwargs
        assert kwargs['community'] is community
        assert kwargs['defaults']['title'] == '知识图谱领域'
        assert kwargs['defaults']['content'] == '社区内容'
        assert kwargs['defaults']['status'] == 'published'
        assert kwargs['defaults']['tags'] == ['图谱', '实体']
        assert result is page

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.wiki.generator.GraphRelation')
    @patch('apps.wiki.generator.GraphEntity')
    @patch('apps.graph.models.GraphCommunity')
    def test_no_entities_shows_none_text(self, mock_community_model,
                                         mock_entity_model, mock_rel_model,
                                         mock_get_embed, mock_wiki_page):
        """无实体时 entities_text 应为 '无'"""
        community = MagicMock()
        community.entity_ids = []
        community.metadata = {'topic': '空领域'}
        community.summary = ''
        community.keywords = []
        mock_community_model.objects.get.return_value = community
        mock_entity_model.objects.filter.return_value = []
        mock_rel_model.objects.filter.return_value.select_related.return_value = []
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        llm = _make_llm()
        generate_community_wiki(community_id=1, level=1, llm=llm)

        prompt = llm.chat.call_args.args[0][0]['content']
        # 无实体时 entities_text 为 "无"（模板为 "主要实体：{entities_text}"）
        assert '主要实体：无' in prompt
        # 无关系时 relations_text 为 "无"
        assert '实体关系：无' in prompt

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.wiki.generator.GraphRelation')
    @patch('apps.wiki.generator.GraphEntity')
    @patch('apps.graph.models.GraphCommunity')
    def test_community_not_found_raises(self, mock_community_model,
                                        mock_entity_model, mock_rel_model,
                                        mock_get_embed, mock_wiki_page):
        """社区不存在时应抛 ObjectDoesNotExist"""
        from django.core.exceptions import ObjectDoesNotExist
        mock_community_model.objects.get.side_effect = ObjectDoesNotExist()

        with pytest.raises(ObjectDoesNotExist):
            generate_community_wiki(community_id=999, level=1, llm=_make_llm())

        mock_wiki_page.objects.update_or_create.assert_not_called()

    @pytest.mark.unit
    @patch('apps.wiki.generator.WikiPage')
    @patch('apps.wiki.generator.get_embedding_client')
    @patch('apps.wiki.generator.GraphRelation')
    @patch('apps.wiki.generator.GraphEntity')
    @patch('apps.graph.models.GraphCommunity')
    def test_title_fallback_when_no_topic(self, mock_community_model,
                                          mock_entity_model, mock_rel_model,
                                          mock_get_embed, mock_wiki_page):
        """metadata 无 topic 时 title 回退为 '知识领域-{community_id}'"""
        community = MagicMock()
        community.community_id = 7
        community.entity_ids = []
        community.metadata = {}  # 无 topic
        community.summary = ''
        community.keywords = []
        mock_community_model.objects.get.return_value = community
        mock_entity_model.objects.filter.return_value = []
        mock_rel_model.objects.filter.return_value.select_related.return_value = []
        mock_wiki_page.objects.update_or_create.return_value = (MagicMock(), True)
        mock_get_embed.return_value.embed_one.return_value = [0.0]

        generate_community_wiki(community_id=7, level=1, llm=_make_llm())

        defaults = mock_wiki_page.objects.update_or_create.call_args.kwargs['defaults']
        assert defaults['title'] == '知识领域-7'


# ============================================================================
# _sync_wiki_embedding —— embedding 同步
# ============================================================================
class TestSyncWikiEmbedding:
    """_sync_wiki_embedding 向量同步测试"""

    @pytest.mark.unit
    @patch('apps.wiki.generator.get_embedding_client')
    def test_sync_embedding_success(self, mock_get_embed):
        """非零向量时应写入 page.embedding 并 save(update_fields=['embedding'])"""
        page = MagicMock()
        page.title = '标题'
        page.summary = '摘要'
        mock_get_embed.return_value.embed_one.return_value = [0.1, 0.2, 0.3]

        _sync_wiki_embedding(page)

        # embed_one 以 "标题\n摘要" 为入参
        mock_get_embed.return_value.embed_one.assert_called_once_with('标题\n摘要')
        # 向量写入并保存
        assert page.embedding == [0.1, 0.2, 0.3]
        page.save.assert_called_once_with(update_fields=['embedding'])

    @pytest.mark.unit
    @patch('apps.wiki.generator.get_embedding_client')
    def test_sync_embedding_zero_vector_skipped(self, mock_get_embed):
        """全零向量时不写入 embedding、不 save（避免空向量污染检索）"""
        page = MagicMock()
        page.title = '标题'
        page.summary = '摘要'
        mock_get_embed.return_value.embed_one.return_value = [0.0, 0.0, 0.0]

        _sync_wiki_embedding(page)

        # 不应 save
        page.save.assert_not_called()
        # embedding 未被赋值
        assert not page.embedding.called

    @pytest.mark.unit
    @patch('apps.wiki.generator.get_embedding_client')
    def test_sync_embedding_empty_vec_skipped(self, mock_get_embed):
        """embed_one 返回空列表时（vec 为假值）不写入、不 save"""
        page = MagicMock()
        page.title = '标题'
        page.summary = '摘要'
        mock_get_embed.return_value.embed_one.return_value = []

        _sync_wiki_embedding(page)

        page.save.assert_not_called()
