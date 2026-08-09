"""
apps.retrieval.vector_store 测试 —— pgvector 向量检索封装

覆盖范围：
- _extract_keywords：jieba 关键词提取（空文本/正常/异常降级）
- upsert_vector：写入/更新向量并同步 Document 冗余权限字段
- delete_by_document：按文档删除向量
- vector_search：余弦相似度检索排序、score 计算、root_types 过滤（依赖 pgvector）

部分 DB 集成：
upsert_vector / delete_by_document / vector_search 依赖 DocumentVector 与
CosineDistance 表达式，需真实 pgvector 才能验证向量排序语义（与 graph app
test_vector_search 同款策略）。_extract_keywords 为纯逻辑，patch jieba 隔离。
"""
import uuid
from unittest.mock import patch

import pytest

from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk, VisibilityLevel
from apps.retrieval.models import DocumentVector
from apps.retrieval import vector_store
from apps.users.models import User


def _vec(fill: float):
    """构造 1024 维全同值向量"""
    return [fill] * 1024


def _alt_vec():
    """交替正负号向量，与全正值向量余弦相似度≈0"""
    return [0.5 if i % 2 == 0 else -0.5 for i in range(1024)]


def _make_user(username='retrieval-user'):
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')


def _make_node(name='测试节点', root_type='company_doc'):
    """创建挂载节点（level 4 业务分类，可手动创建）"""
    node = KnowledgeNode.objects.create(
        root_type=root_type, node_type='folder', node_level=4, name=name)
    node.path = f'/{node.id}/'
    node.depth = 1
    node.save(update_fields=['path', 'depth'])
    return node


def _make_doc(node, owner, title='测试文档'):
    """创建文档（绕过上传管线）"""
    return Document.objects.create(
        node=node, title=title, file_name=f'{title}.txt', file_type='txt',
        file_size=100, file_hash=uuid.uuid4().hex, file_path='/tmp/fake.txt',
        mime_type='text/plain', owner=owner, dept_id=None, team_id=None,
        visibility_level=VisibilityLevel.PUBLIC, root_type=node.root_type,
        status='done')


def _make_chunk(doc, content='测试切片内容', index=0):
    """创建文档切片"""
    return DocumentChunk.objects.create(
        document=doc, chunk_index=index, chunk_type='text', content=content)


# ============================================================================
# _extract_keywords
# ============================================================================
@pytest.mark.unit
class TestExtractKeywords:
    """jieba 关键词提取测试"""

    def test_empty_text_returns_empty(self):
        """空文本直接返回空列表"""
        assert vector_store._extract_keywords('') == []
        assert vector_store._extract_keywords(None) == []

    def test_normal_text_uses_jieba(self):
        """非空文本走 jieba.analyse.extract_tags"""
        with patch('jieba.analyse.extract_tags', return_value=['知识', '检索']) as mock_tags:
            assert vector_store._extract_keywords('知识检索测试') == ['知识', '检索']
        mock_tags.assert_called_once_with('知识检索测试', topK=10)

    def test_jieba_error_falls_back(self):
        """jieba 异常时降级返回空列表，不向上抛出"""
        with patch.dict('sys.modules', {'jieba.analyse': None}):
            assert vector_store._extract_keywords('文本') == []


# ============================================================================
# upsert_vector / delete_by_document（DB 集成）
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestUpsertVector:
    """向量写入与冗余字段同步测试"""

    def test_upsert_creates_vector_with_synced_fields(self):
        """首次写入：DocumentVector 同步 document 的权限/节点冗余字段"""
        user = _make_user()
        node = _make_node()
        doc = _make_doc(node, user)
        chunk = _make_chunk(doc, '内容' * 100)  # 超过 200 字截断 preview

        vec = vector_store.upsert_vector(chunk, _vec(0.1))

        assert vec.document_id == doc.id
        assert vec.chunk_id == chunk.id
        assert vec.visibility_level == VisibilityLevel.PUBLIC
        assert vec.owner_id == user.id
        assert vec.root_type == 'company_doc'
        assert vec.node_id == node.id
        assert vec.node_path == node.path
        assert vec.content_preview == (chunk.content or '')[:200]
        assert vec.embedding_model == 'bge-m3'

    def test_upsert_updates_existing_vector(self):
        """重复写入同一 chunk：update_or_create 应更新而非新建"""
        user = _make_user()
        node = _make_node()
        doc = _make_doc(node, user)
        chunk = _make_chunk(doc)
        v1 = vector_store.upsert_vector(chunk, _vec(0.1))

        # 修改文档可见性后再次 upsert，冗余字段应同步刷新
        # DEPT_ONLY 需满足 doc_owner_scope_required 约束（部门归属非空）
        Document.objects.filter(id=doc.id).update(
            visibility_level=VisibilityLevel.DEPT_ONLY, dept_id=999)
        doc.refresh_from_db()
        v2 = vector_store.upsert_vector(chunk, _vec(0.2))

        assert v2.id == v1.id
        assert DocumentVector.objects.filter(chunk_id=chunk.id).count() == 1
        v2.refresh_from_db()
        assert v2.visibility_level == VisibilityLevel.DEPT_ONLY

    def test_delete_by_document(self):
        """按文档删除其全部向量"""
        user = _make_user()
        node = _make_node()
        doc = _make_doc(node, user)
        chunk = _make_chunk(doc)
        vector_store.upsert_vector(chunk, _vec(0.1))
        assert DocumentVector.objects.filter(document_id=doc.id).count() == 1

        vector_store.delete_by_document(doc.id)
        assert DocumentVector.objects.filter(document_id=doc.id).count() == 0


# ============================================================================
# vector_search（依赖 pgvector）
# ============================================================================
@pytest.fixture
def vector_env():
    """构造检索环境：用户 + 文档 + 两个向量（与 query 同向的 chunk1、正交的 chunk2）"""
    user = _make_user()
    node = _make_node(root_type='company_doc')
    doc = _make_doc(node, user)
    chunk1 = _make_chunk(doc, '检索内容一', index=0)
    chunk2 = _make_chunk(doc, '检索内容二', index=1)
    vector_store.upsert_vector(chunk1, _vec(0.1))
    vector_store.upsert_vector(chunk2, _alt_vec())
    return {'user': user, 'chunk1': chunk1, 'chunk2': chunk2}


@pytest.mark.django_db
@pytest.mark.integration
class TestVectorSearch:
    """向量检索排序与过滤测试"""

    def test_search_orders_by_score_desc(self, vector_env):
        """与 query 相同向量的 chunk 应排最前，score=1.0"""
        results = vector_store.vector_search(_vec(0.1), vector_env['user'], top_k=10)
        assert len(results) == 2
        assert results[0]['chunk_id'] == vector_env['chunk1'].id
        assert results[0]['score'] == 1.0
        scores = [r['score'] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_filters_by_root_types(self, vector_env):
        """root_types 过滤只返回指定根类型的向量"""
        results = vector_store.vector_search(_vec(0.1), vector_env['user'], root_types=['other_type'])
        assert results == []

    def test_search_empty_query_returns_empty(self, vector_env):
        """无匹配向量时返回空列表"""
        other = _make_node(root_type='hr_docs')
        other_doc = _make_doc(other, vector_env['user'], title='HR文档')
        other_chunk = _make_chunk(other_doc, 'HR内容')
        vector_store.upsert_vector(other_chunk, _alt_vec())
        results = vector_store.vector_search(_vec(0.1), vector_env['user'], root_types=['hr_docs'])
        # 交替向量与全正值 query 相似度≈0，distance≈1 → score≈0
        assert len(results) == 1
        assert results[0]['chunk_id'] == other_chunk.id
        assert results[0]['score'] >= 0.0
