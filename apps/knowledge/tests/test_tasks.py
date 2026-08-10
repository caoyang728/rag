"""
apps.knowledge.tasks 单元测试 —— 文档解析 / 清理 / 批量导入 Celery 任务

覆盖范围：
- _sanitize_content / _sanitize_dict：NUL 字节清理
- parse_document：完整解析流水线（mock parser/chunker/embedding/向量库/图谱同步）
  - 文档不存在 / 解析异常置 failed / embedding 失败置 failed（瞬时 embedding_failed 被覆盖）
  - 图片块生成 ImageResource / 代码块生成 CodeChunk
  - OSS 文件下载分支 / DOCUMENT_RETENTION_ENABLED=False 清理原文件分支
- cleanup_deleted_docs：源码 __ne 缺陷固化 + mock queryset 边界验证循环体
- _log_batch_import_failure：失败日志落盘
- batch_import_single_file：临时文件缺失 / 节点缺失 / 上传者缺失 / 哈希去重
  / 成功导入（可见性归一化 int/str / 部门团队归属推导）/ 异常兜底

直接调用任务函数而非 .delay()：
任务函数直接调用（Task.__call__ → run）不会触发 Celery 重试机制，
每个错误路径只执行一次，便于精确断言状态流转；
happy path 下断言 parse_document.delay 被调用则 mock 掉任务对象。
"""
import hashlib
import os
import tempfile
import uuid as uuid_lib
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.knowledge.models import (
    Document, DocumentChunk, CodeChunk, ImageResource,
    KnowledgeNode, VisibilityLevel,
)
from apps.knowledge.tasks import (
    _log_batch_import_failure, _notify_admin_on_embedding_failure, _sanitize_content, _sanitize_dict,
    batch_import_single_file, cleanup_deleted_docs, parse_document,
)
from apps.notification.models import EmailSendLog, EmailSubscription
from apps.users.models import Department, Team, User


# ============================================================================
# 文本清洗工具函数测试
# ============================================================================
class TestSanitize:
    """NUL 字节清理函数测试（纯函数，无 DB）"""

    @pytest.mark.unit
    def test_sanitize_content_none(self):
        """None 输入返回空串"""
        assert _sanitize_content(None) == ''

    @pytest.mark.unit
    def test_sanitize_content_without_nul(self):
        """无 NUL 字节的字符串原样返回"""
        assert _sanitize_content('abc') == 'abc'

    @pytest.mark.unit
    def test_sanitize_content_removes_nul(self):
        """含 NUL 字节的字符串剔除 NUL"""
        assert _sanitize_content('a\x00b\x00c') == 'abc'

    @pytest.mark.unit
    def test_sanitize_dict_nested(self):
        """嵌套 dict/list/str/int 递归清理"""
        data = {
            'a': 'x\x00y',
            'b': ['\x00', 'ok'],
            'c': 42,
            'd': {'e': 'f\x00g'},
        }
        result = _sanitize_dict(data)
        assert result == {'a': 'xy', 'b': ['', 'ok'], 'c': 42, 'd': {'e': 'fg'}}


# ============================================================================
# parse_document 解析流水线测试
# ============================================================================
@pytest.mark.django_db
class TestParseDocument:
    """parse_document 任务测试（mock 全部外部依赖，验证状态机流转）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/部门/团队/节点链

        yield 后统一停止 _patch_pipeline 启动的 mock，避免泄漏到其他测试
        （测试结束后统一恢复）。
        """
        self.user = User.objects.create_user(
            username='owner', password='pass123', email='owner@test.com')
        self.dept = Department.objects.create(name='研发部', code='rd')
        self.team = Team.objects.create(
            name='后端组', code='rd-backend', department=self.dept)
        # 节点链：root -> dept -> team -> category
        self.root = self._make_node('知识库', 1, None, 'root')
        self.dept_node = self._make_node('研发部', 2, self.root, 'folder', ref_id=self.dept.id)
        self.team_node = self._make_node('后端组', 3, self.dept_node, 'folder', ref_id=self.team.id)
        self.category = self._make_node('业务分类', 4, self.team_node, 'folder')
        yield
        for p in getattr(self, '_pipeline_patches', []):
            p.stop()
        self._pipeline_patches = []

    def _make_node(self, name, level, parent, node_type, ref_id=None):
        node = KnowledgeNode.objects.create(
            name=name, node_type=node_type, node_level=level,
            root_type='company_doc', parent=parent, ref_id=ref_id,
            depth=(parent.depth + 1) if parent else 0,
            created_by=self.user,
        )
        padded = f'{node.id:04d}'
        node.path = f'{parent.path}{padded}/' if parent else f'/{padded}/'
        node.save(update_fields=['path'])
        return node

    def _make_doc(self, **kw):
        defaults = dict(
            node=self.category, title='测试文档', file_name='t.txt',
            file_type='txt', file_size=10, file_hash=uuid_lib.uuid4().hex,
            file_path='/tmp/fake_doc.txt', mime_type='text/plain',
            owner=self.user, dept_id=self.dept.id, team_id=self.team.id,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            root_type='company_doc', status='pending',
        )
        defaults.update(kw)
        return Document.objects.create(**defaults)

    def _patch_pipeline(self, blocks, pieces, embeddings):
        """统一 mock 解析流水线外部依赖，返回各 mock 对象

        patch 对象存入 self._pipeline_patches，由 _env fixture 在测试结束后
        统一 stop，避免泄漏到其他测试。
        """
        m_parser = MagicMock()
        m_parser.parse.return_value = blocks
        m_desensitize = MagicMock(return_value=('', 0))
        m_chunk = MagicMock(return_value=pieces)
        m_delete = MagicMock()
        m_upsert = MagicMock()
        m_embed = MagicMock()
        m_embed.embed.return_value = embeddings
        p1 = patch('apps.knowledge.tasks.get_parser', return_value=m_parser)
        p2 = patch('apps.knowledge.tasks.desensitize', m_desensitize)
        p3 = patch('apps.knowledge.tasks.chunk_blocks', m_chunk)
        p4 = patch('apps.knowledge.tasks.delete_by_document', m_delete)
        p5 = patch('apps.knowledge.tasks.upsert_vector', m_upsert)
        p6 = patch('apps.knowledge.tasks.get_embedding_client', return_value=m_embed)
        p7 = patch('apps.graph.sync.on_document_done')
        p8 = patch('apps.wiki.sync.on_document_done_for_wiki')
        for p in (p1, p2, p3, p4, p5, p6, p7, p8):
            p.start()
        self._pipeline_patches = [p1, p2, p3, p4, p5, p6, p7, p8]
        return m_parser, m_desensitize, m_chunk, m_delete, m_upsert, m_embed

    def test_doc_not_found(self):
        """文档不存在时返回错误 dict，不抛异常"""
        result = parse_document(999999)
        assert result == {'ok': False, 'error': 'doc not found'}

    def test_parse_happy_path(self):
        """完整解析：2 个块 → 2 个切片 + 向量写入 + 状态 done"""
        blocks = [
            {'type': 'text', 'content': '第一段', 'section_path': 'S1', 'page_number': 1},
            {'type': 'text', 'content': '第二段', 'section_path': 'S2', 'page_number': 2},
        ]
        pieces = [
            {'content': '第一段', 'type': 'text', 'section_path': 'S1', 'page_number': 1, 'extra': {}},
            {'content': '第二段', 'type': 'text', 'section_path': 'S2', 'page_number': 2, 'extra': {}},
        ]
        m_parser, m_desensitize, m_chunk, m_delete, m_upsert, m_embed = \
            self._patch_pipeline(blocks, pieces, [[0.1, 0.2], [0.3, 0.4]])
        doc = self._make_doc()
        result = parse_document(doc.id)
        assert result['ok'] is True
        assert result['chunks'] == 2
        doc.refresh_from_db()
        assert doc.status == 'done'
        assert doc.chunk_count == 2
        assert doc.error_message == ''
        # 切片落库 + 向量写入
        assert DocumentChunk.objects.filter(document=doc).count() == 2
        assert m_upsert.call_count == 2
        # 旧切片/向量清理应被调用
        m_delete.assert_called_once_with(doc.id)
        # 解析器应被调用一次
        m_parser.parse.assert_called_once()

    def test_parse_image_chunk_creates_image_resource(self):
        """图片块（含 base64_data）应生成 ImageResource，且从 extra 中剥离 base64"""
        blocks = [{'type': 'image', 'content': '', 'section_path': '', 'page_number': 1}]
        pieces = [{
            'content': 'img', 'type': 'image', 'section_path': '', 'page_number': 1,
            'extra': {'base64_data': 'QUJD', 'mime_type': 'image/png', 'width': 10, 'height': 20},
        }]
        self._patch_pipeline(blocks, pieces, [[0.1]])
        doc = self._make_doc()
        parse_document(doc.id)
        img = ImageResource.objects.filter(document=doc).first()
        assert img is not None
        assert img.storage_mode == 'base64'
        assert img.base64_data == 'QUJD'
        # extra 中不应再保留 base64_data（避免重复入库）
        chunk = DocumentChunk.objects.get(document=doc, chunk_index=0)
        assert 'base64_data' not in chunk.extra

    def test_parse_code_chunk_creates_code_chunk(self):
        """代码块（含 symbol_type）应生成 CodeChunk 元数据"""
        blocks = [{'type': 'code', 'content': 'def foo(): pass', 'section_path': '', 'page_number': 1}]
        pieces = [{
            'content': 'def foo(): pass', 'type': 'code', 'section_path': '', 'page_number': 1,
            'extra': {'symbol_type': 'function', 'language': 'python', 'symbol_name': 'foo',
                      'signature': 'def foo()', 'params': [], 'docstring': '', 'start_line': 1, 'end_line': 2},
        }]
        self._patch_pipeline(blocks, pieces, [[0.1]])
        doc = self._make_doc()
        parse_document(doc.id)
        assert CodeChunk.objects.filter(document=doc).count() == 1
        cc = CodeChunk.objects.get(document=doc)
        assert cc.symbol_name == 'foo'

    def test_parse_parser_exception_sets_failed(self):
        """解析器抛异常 → 文档状态置 failed 并重新抛出"""
        m_parser = MagicMock()
        m_parser.parse.side_effect = RuntimeError('parse boom')
        with patch('apps.knowledge.tasks.get_parser', return_value=m_parser):
            doc = self._make_doc()
            with pytest.raises(RuntimeError):
                parse_document(doc.id)
        doc.refresh_from_db()
        assert doc.status == 'failed'
        assert 'parse boom' in doc.error_message

    def test_parse_embedding_failure_marks_failed(self):
        """embedding 失败 → 通知管理员 + 状态最终置 failed 并重新抛出

        注意（源码行为）：内层先把状态置为 embedding_failed，随后 raise 被
        外层 except 捕获，最终状态被覆盖为 failed —— embedding_failed 只是瞬时值。
        """
        m_parser = MagicMock()
        m_parser.parse.return_value = [
            {'type': 'text', 'content': 'x', 'section_path': '', 'page_number': 1}]
        pieces = [{'content': 'x', 'type': 'text', 'section_path': '', 'page_number': 1, 'extra': {}}]
        with patch('apps.knowledge.tasks.get_parser', return_value=m_parser), \
                patch('apps.knowledge.tasks.chunk_blocks', return_value=pieces), \
                patch('apps.knowledge.tasks.desensitize', return_value=('', 0)):
            m_embed = MagicMock()
            m_embed.embed.side_effect = RuntimeError('embed down')
            with patch('apps.knowledge.tasks.get_embedding_client', return_value=m_embed), \
                    patch('apps.knowledge.tasks._notify_admin_on_embedding_failure') as m_notify:
                doc = self._make_doc()
                with pytest.raises(RuntimeError):
                    parse_document(doc.id)
                m_notify.assert_called_once()
        doc.refresh_from_db()
        assert doc.status == 'failed'
        assert 'embed down' in doc.error_message

    def test_parse_oss_download_branch(self):
        """OSS 文件：下载到临时文件后走正常解析流水线"""
        blocks = [{'type': 'text', 'content': 'x', 'section_path': '', 'page_number': 1}]
        pieces = [{'content': 'x', 'type': 'text', 'section_path': '', 'page_number': 1, 'extra': {}}]
        self._patch_pipeline(blocks, pieces, [[0.1]])
        m_storage = MagicMock()
        m_storage.get_url.return_value = 'http://oss.example.com/t.pdf'
        # urllib.request.urlopen 作为 context manager 使用
        m_resp = MagicMock()
        m_resp.read.return_value = b'pdf-content'
        with patch('apps.knowledge.tasks.get_document_storage', return_value=m_storage), \
                patch('urllib.request.urlopen') as m_urlopen:
            m_urlopen.return_value.__enter__.return_value = m_resp
            doc = self._make_doc(file_path='oss://bucket/documents/t.pdf', file_name='t.pdf', file_type='pdf')
            result = parse_document(doc.id)
        assert result['ok'] is True
        m_urlopen.assert_called_once_with('http://oss.example.com/t.pdf')
        doc.refresh_from_db()
        assert doc.status == 'done'

    @override_settings(DOCUMENT_RETENTION_ENABLED=False)
    def test_parse_retention_cleanup_original_file(self):
        """关闭文档保留后，解析完成应删除原始文件并清空 file_path"""
        blocks = [{'type': 'text', 'content': 'x', 'section_path': '', 'page_number': 1}]
        pieces = [{'content': 'x', 'type': 'text', 'section_path': '', 'page_number': 1, 'extra': {}}]
        self._patch_pipeline(blocks, pieces, [[0.1]])
        m_storage = MagicMock()
        with patch('apps.knowledge.tasks.get_document_storage', return_value=m_storage):
            doc = self._make_doc(file_path='/tmp/retention_file.txt')
            parse_document(doc.id)
        m_storage.delete.assert_called_once_with('/tmp/retention_file.txt')
        doc.refresh_from_db()
        assert doc.file_path == ''


# ============================================================================
# cleanup_deleted_docs 清理任务测试
# ============================================================================
@pytest.mark.django_db
class TestCleanupDeletedDocs:
    """已删除文档物理文件清理任务测试

    源码缺陷：cleanup_deleted_docs 使用 `file_path__ne=''` 非法查询语法，
    queryset 求值时必抛 FieldError，任务实际不可运行。
    因此测试分两层：① 固化 FieldError 当前行为；② mock queryset 边界验证循环体逻辑。
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/部门/团队/节点"""
        self.user = User.objects.create_user(
            username='owner2', password='pass123', email='o2@test.com')
        self.dept = Department.objects.create(name='市场部', code='mkt')
        self.team = Team.objects.create(name='市场一组', code='mkt-1', department=self.dept)
        self.node = KnowledgeNode.objects.create(
            name='分类', node_type='folder', node_level=4, root_type='company_doc',
            depth=3, path='/1/2/3/', created_by=self.user)

    def _make_doc(self, **kw):
        defaults = dict(
            node=self.node, title='文档', file_name='f.txt', file_type='txt',
            file_size=10, file_hash=uuid_lib.uuid4().hex, file_path='/tmp/f.txt',
            mime_type='text/plain', owner=self.user, dept_id=self.dept.id,
            team_id=self.team.id, visibility_level=VisibilityLevel.TEAM_ONLY,
            root_type='company_doc', status='done',
        )
        defaults.update(kw)
        return Document.objects.create(**defaults)

    def test_cleanup_source_bug_raises_field_error(self):
        """源码缺陷：file_path__ne 是非法 lookup，真实调用必然抛 FieldError

        固化当前行为；修复源码后此测试应改为断言正常清理逻辑。
        """
        from django.core.exceptions import FieldError
        old = timezone.now() - timedelta(days=200)
        self._make_doc(is_deleted=True, delete_time=old, file_path='/tmp/a.txt', file_name='a.txt')
        with pytest.raises(FieldError):
            cleanup_deleted_docs(180)

    def test_cleanup_deleted_docs_mocked_queryset(self):
        """超过保留期的已删除文档被清理（mock queryset 边界，绕过 __ne 缺陷）

        mock Document.objects.filter 返回假文档列表，只验证循环体逻辑：
        删除物理文件 → 清空 file_path → 计数与返回结构。
        """
        doc1 = MagicMock()
        doc1.id = 1
        doc1.file_path = '/tmp/a.txt'
        doc1.file_name = 'a.txt'
        doc2 = MagicMock()
        doc2.id = 2
        doc2.file_path = '/tmp/b.txt'
        doc2.file_name = 'b.txt'
        with patch.object(Document.objects, 'filter', return_value=[doc1, doc2]) as m_filter, \
                patch('apps.knowledge.tasks.get_document_storage') as m_storage:
            storage = m_storage.return_value
            result = cleanup_deleted_docs(180)
        assert result['ok'] is True
        assert result['cleaned'] == 2
        assert result['failed'] == 0
        assert storage.delete.call_count == 2
        # 清理后 file_path 置空并保存
        assert doc1.file_path == ''
        assert doc2.file_path == ''
        assert doc1.save.called
        assert doc2.save.called

    def test_cleanup_no_matches(self):
        """无匹配文档时 cleaned=0（mock 空 queryset）"""
        with patch.object(Document.objects, 'filter', return_value=[]) as m_filter, \
                patch('apps.knowledge.tasks.get_document_storage') as m_storage:
            result = cleanup_deleted_docs(180)
        assert result == {'ok': True, 'cleaned': 0, 'failed': 0, 'failed_paths': []}
        m_storage.return_value.delete.assert_not_called()

    def test_cleanup_delete_failure_counted(self):
        """存储删除抛异常 → 计入 failed 并记录失败路径"""
        doc1 = MagicMock()
        doc1.id = 7
        doc1.file_path = '/tmp/err.txt'
        doc1.file_name = 'err.txt'
        with patch.object(Document.objects, 'filter', return_value=[doc1]) as m_filter, \
                patch('apps.knowledge.tasks.get_document_storage') as m_storage:
            m_storage.return_value.delete.side_effect = Exception('disk error')
            result = cleanup_deleted_docs(180)
        assert result['ok'] is True
        assert result['failed'] == 1
        assert len(result['failed_paths']) == 1
        assert result['failed_paths'][0] == '7:/tmp/err.txt'
        # 失败时 file_path 保留
        assert doc1.file_path == '/tmp/err.txt'


# ============================================================================
# _log_batch_import_failure 失败日志测试
# ============================================================================
class TestLogBatchImportFailure:
    """批量导入失败日志落盘测试"""

    @pytest.mark.unit
    def test_log_file_written(self):
        """失败日志写入 BASE_DIR/logs/batch_import_failed.log"""
        base = tempfile.mkdtemp()
        with override_settings(BASE_DIR=base):
            _log_batch_import_failure('坏文件.xlsx', 'node_id=3', '解析失败')
        log_path = os.path.join(base, 'logs', 'batch_import_failed.log')
        assert os.path.exists(log_path)
        with open(log_path, encoding='utf-8') as f:
            content = f.read()
        assert '坏文件.xlsx' in content
        assert '解析失败' in content


# ============================================================================
# batch_import_single_file 批量导入任务测试
# ============================================================================
@pytest.mark.django_db
class TestBatchImportSingleFile:
    """批量导入单文件任务测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/部门/团队/节点链"""
        self.user = User.objects.create_user(
            username='importer', password='pass123', email='imp@test.com')
        self.dept = Department.objects.create(name='技术部', code='tech')
        self.team = Team.objects.create(name='技术一组', code='tech-1', department=self.dept)
        self.user.department = self.dept
        self.user.team = self.team
        self.user.save()
        self.root = self._make_node('知识库', 1, None, 'root')
        self.dept_node = self._make_node('技术部', 2, self.root, 'folder', ref_id=self.dept.id)
        self.team_node = self._make_node('技术一组', 3, self.dept_node, 'folder', ref_id=self.team.id)
        self.category = self._make_node('业务分类', 4, self.team_node, 'folder')

    def _make_node(self, name, level, parent, node_type, ref_id=None):
        node = KnowledgeNode.objects.create(
            name=name, node_type=node_type, node_level=level,
            root_type='company_doc', parent=parent, ref_id=ref_id,
            depth=(parent.depth + 1) if parent else 0,
            created_by=self.user,
        )
        padded = f'{node.id:04d}'
        node.path = f'{parent.path}{padded}/' if parent else f'/{padded}/'
        node.save(update_fields=['path'])
        return node

    def _write_temp(self, content=b'import content', suffix='.txt'):
        """写临时文件并返回路径"""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(content)
        tmp.close()
        return tmp.name

    def test_temp_file_missing(self):
        """临时文件不存在 → 返回错误"""
        result = batch_import_single_file(
            '/nonexistent/tmp.bin', self.category.id, self.user.id,
            'TEAM_ONLY', None, 'a.txt')
        assert result['ok'] is False
        assert '临时文件不存在' in result['error']

    def test_node_not_found(self):
        """目标节点不存在 → 返回错误"""
        tmp = self._write_temp()
        result = batch_import_single_file(
            tmp, 999999, self.user.id, 'TEAM_ONLY', None, 'a.txt')
        assert result['ok'] is False
        assert '节点不存在' in result['error']
        os.remove(tmp)

    def test_owner_not_found(self):
        """上传者不存在 → 返回错误"""
        tmp = self._write_temp()
        result = batch_import_single_file(
            tmp, self.category.id, 999999, 'TEAM_ONLY', None, 'a.txt')
        assert result['ok'] is False
        assert '上传者不存在' in result['error']
        os.remove(tmp)

    @patch('apps.knowledge.tasks.get_document_storage')
    def test_duplicate_hash_rejected(self, m_storage):
        """文件哈希已存在 → 拒绝重复导入并删除临时文件"""
        content = b'duplicate-content'
        Document.objects.create(
            node=self.category, title='dup.txt', file_name='dup.txt', file_type='txt',
            file_size=len(content), file_hash=hashlib.sha256(content).hexdigest(),
            file_path='/tmp/dup.txt', mime_type='text/plain', owner=self.user,
            dept_id=self.dept.id, team_id=self.team.id,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            root_type='company_doc', status='done')
        tmp = self._write_temp(content)
        result = batch_import_single_file(
            tmp, self.category.id, self.user.id, 'TEAM_ONLY', self.team.id, 'dup.txt')
        assert result['ok'] is False
        assert '重复导入' in result['error']
        assert not os.path.exists(tmp)  # 重复文件临时文件被删除
        # 不应新建文档
        assert Document.objects.filter(node=self.category).count() == 1

    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.tasks.get_document_storage')
    def test_batch_import_success(self, m_storage, m_parse):
        """成功导入：保存文件 + 创建文档 + 触发解析任务 + 清理临时文件"""
        m_storage.return_value.save.return_value = 'documents/xx.txt'
        tmp = self._write_temp(b'hello batch import')
        result = batch_import_single_file(
            tmp, self.category.id, self.user.id, 'TEAM_ONLY', self.team.id, '导入文档.txt')
        assert result['ok'] is True
        doc = Document.objects.get(id=result['doc_id'])
        assert doc.title == '导入文档.txt'
        assert doc.file_name == '导入文档.txt'
        assert doc.file_type == 'txt'
        assert doc.visibility_level == 'TEAM_ONLY'
        assert doc.status == 'pending'
        assert doc.version == 1
        assert doc.version_tag == 'v1'
        # 部门/团队归属推导：level2 祖先 ref_id=dept.id；owner_team_id 直接使用
        assert doc.dept_id == self.dept.id
        assert doc.team_id == self.team.id
        # 触发异步解析
        m_parse.delay.assert_called_once_with(doc.id)
        # 临时文件已删除
        assert not os.path.exists(tmp)

    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.tasks.get_document_storage')
    def test_batch_import_visibility_int_and_str(self, m_storage, m_parse):
        """可见性归一化：int(4)=PUBLIC / str('public')=PUBLIC / 非法值回退 TEAM_ONLY"""
        m_storage.return_value.save.return_value = 'documents/y.txt'
        # int 4 → PUBLIC
        tmp1 = self._write_temp(b'vis-int')
        r1 = batch_import_single_file(tmp1, self.category.id, self.user.id, 4, None, 'vis-int.txt')
        assert Document.objects.get(id=r1['doc_id']).visibility_level == 'PUBLIC'
        # str 'public' → PUBLIC
        tmp2 = self._write_temp(b'vis-str')
        r2 = batch_import_single_file(tmp2, self.category.id, self.user.id, 'public', None, 'vis-str.txt')
        assert Document.objects.get(id=r2['doc_id']).visibility_level == 'PUBLIC'
        # 非法值 → TEAM_ONLY
        tmp3 = self._write_temp(b'vis-bad')
        r3 = batch_import_single_file(tmp3, self.category.id, self.user.id, '???', None, 'vis-bad.txt')
        assert Document.objects.get(id=r3['doc_id']).visibility_level == 'TEAM_ONLY'

    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.tasks.get_document_storage')
    def test_batch_import_team_derived_from_ancestor(self, m_storage, m_parse):
        """未传 owner_team_id 时，团队从 Level3 祖先节点 ref_id 推导"""
        m_storage.return_value.save.return_value = 'documents/z.txt'
        tmp = self._write_temp(b'team-derive')
        result = batch_import_single_file(
            tmp, self.category.id, self.user.id, 'TEAM_ONLY', None, 'derive.txt')
        doc = Document.objects.get(id=result['doc_id'])
        assert doc.team_id == self.team.id
        assert doc.dept_id == self.dept.id

    @patch('apps.knowledge.tasks.get_document_storage')
    def test_batch_import_exception_keeps_temp_file(self, m_storage):
        """存储保存抛异常 → 返回错误并保留临时文件（便于人工处理）"""
        m_storage.return_value.save.side_effect = RuntimeError('disk full')
        tmp = self._write_temp(b'boom')
        result = batch_import_single_file(
            tmp, self.category.id, self.user.id, 'TEAM_ONLY', None, 'boom.txt')
        assert result['ok'] is False
        assert 'disk full' in result['error']
        assert os.path.exists(tmp)  # 失败保留临时文件
        os.remove(tmp)


# ============================================================================
# _notify_admin_on_embedding_failure —— embedding 失败管理员邮件通知
# ============================================================================
@pytest.mark.django_db
class TestNotifyAdminOnEmbeddingFailure:
    """embedding 失败邮件通知测试（订阅查询 + send_mail + EmailSendLog 落库）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """构造管理员/上传者与待通知文档"""
        self.admin = User.objects.create_user(
            username='admin', password='pass123', email='admin@test.com')
        self.owner = User.objects.create_user(
            username='owner', password='pass123', email='owner@test.com')
        self.dept = Department.objects.create(name='研发部', code='rd')
        self.team = Team.objects.create(
            name='后端组', code='rd-backend', department=self.dept)
        self.root = KnowledgeNode.objects.create(
            name='知识库', node_type='root', node_level=1,
            root_type='company_doc', parent=None, depth=0, created_by=self.owner)
        padded = f'{self.root.id:04d}'
        self.root.path = f'/{padded}/'
        self.root.save(update_fields=['path'])
        self.doc = Document.objects.create(
            node=self.root, title='坏文档.txt', file_name='坏文档.txt',
            file_type='txt', file_size=10, file_hash=uuid_lib.uuid4().hex,
            file_path='/tmp/bad.txt', mime_type='text/plain', owner=self.owner,
            dept_id=self.dept.id, team_id=self.team.id,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            root_type='company_doc', status='embedding_failed')

    def test_no_subscribers_then_no_mail_and_no_log(self):
        """无 system_notice 订阅 → 不发送邮件、不落 EmailSendLog"""
        with patch('django.core.mail.send_mail') as m_send:
            _notify_admin_on_embedding_failure(self.doc, 'boom')
        m_send.assert_not_called()
        assert EmailSendLog.objects.count() == 0

    def test_disabled_subscriber_then_skipped(self):
        """订阅但停用 → 视为无订阅者，不发送邮件"""
        EmailSubscription.objects.create(
            user=self.admin, category='system_notice', is_enabled=False)
        with patch('django.core.mail.send_mail') as m_send:
            _notify_admin_on_embedding_failure(self.doc, 'boom')
        m_send.assert_not_called()
        assert EmailSendLog.objects.count() == 0

    def test_with_subscribers_then_mail_sent_and_logged(self):
        """有启用订阅者 → 发送邮件并落 EmailSendLog(success)"""
        EmailSubscription.objects.create(
            user=self.admin, category='system_notice', is_enabled=True)
        with patch('django.core.mail.send_mail') as m_send:
            _notify_admin_on_embedding_failure(self.doc, 'boom')
        m_send.assert_called_once()
        log = EmailSendLog.objects.get()
        assert log.to_email == 'admin@test.com'
        assert log.status == 'success'
        assert 'Embedding 失败' in log.subject

    def test_mail_failure_then_log_failed(self):
        """send_mail 抛异常 → EmailSendLog 记录 failed，不阻断"""
        EmailSubscription.objects.create(
            user=self.admin, category='system_notice', is_enabled=True)
        with patch('django.core.mail.send_mail',
                   side_effect=RuntimeError('smtp down')):
            _notify_admin_on_embedding_failure(self.doc, 'boom')
        log = EmailSendLog.objects.get()
        assert log.status == 'failed'
        assert 'smtp down' in log.error_message
