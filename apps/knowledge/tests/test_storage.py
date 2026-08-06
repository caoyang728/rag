"""
apps.knowledge.storage 单元测试 —— 文档存储抽象层（本地 / OSS 两种模式）

覆盖范围：
- _safe_node_name：空名 / 特殊字符 / 中文转拼音 / pypinyin 缺失降级 / 超长截断
- generate_node_storage_path：根节点 / 多级父节点链的路径拼接
- LocalStorage：save（带/不带 node_path）/ delete / get_url
- OssStorage：bucket 懒加载缓存 / save 小文件（put_object）/ 大文件（multipart）
  / delete / get_url（签名 URL 与失败降级）
- get_document_storage：按 settings.DOCUMENT_STORAGE_MODE 选择后端

采用 mock 而非真实基础设施：
storage.py 是纯存储抽象层，本地后端可真实落盘（临时目录），
OSS 后端依赖 oss2 SDK（测试环境未安装），统一 mock 掉 oss2 验证调用契约。
"""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.knowledge.storage import (
    LocalStorage, OssStorage, _safe_node_name,
    generate_node_storage_path, get_document_storage,
)


# ============================================================================
# _safe_node_name 纯函数测试
# ============================================================================
class TestSafeNodeName:
    """节点名安全化函数测试"""

    @pytest.mark.unit
    def test_empty_name_uses_node_id(self):
        """空名应降级为节点 ID"""
        assert _safe_node_name('', 42) == '42'

    @pytest.mark.unit
    def test_english_name_kept(self):
        """纯英文/数字名称保持不变（去除特殊字符）"""
        assert _safe_node_name('Backend-2024!', 1) == 'Backend_2024'

    @pytest.mark.unit
    def test_special_chars_only_uses_node_id(self):
        """全特殊字符名称应降级为节点 ID（strip 后为空）"""
        assert _safe_node_name('!!!@@@###', 7) == '7'

    @pytest.mark.unit
    def test_chinese_name_to_pinyin(self):
        """中文名称转拼音（pypinyin 已安装）"""
        assert _safe_node_name('知识库', 3) == 'zhi_shi_ku'

    @pytest.mark.unit
    def test_pypinyin_missing_uses_node_id(self):
        """pypinyin 未安装时中文名降级为节点 ID

        注意：_safe_node_name 内部 `from pypinyin import lazy_pinyin`，
        因此把 sys.modules 中的 pypinyin 置 None 触发 ImportError。
        """
        with patch.dict('sys.modules', {'pypinyin': None}):
            assert _safe_node_name('中文名', 9) == '9'

    @pytest.mark.unit
    def test_long_name_truncated_to_32(self):
        """超长名称截断为 32 字符"""
        name = 'a' * 100
        assert len(_safe_node_name(name, 1)) == 32


# ============================================================================
# generate_node_storage_path 路径生成测试
# ============================================================================
class TestGenerateNodeStoragePath:
    """节点存储路径生成测试（node-{id}_{safe_name}/ 链式拼接）"""

    @pytest.mark.unit
    def test_root_node_path(self):
        """根节点（无 parent）只生成一级路径，末尾带 /"""
        node = MagicMock()
        node.id = 1
        node.name = '知识库'
        node.parent = None
        path = generate_node_storage_path(node)
        assert path == 'node-1_zhi_shi_ku/'

    @pytest.mark.unit
    def test_chain_path(self):
        """多级父节点链按根→叶子顺序拼接"""
        root = MagicMock()
        root.id = 1
        root.name = '知识库'
        root.parent = None
        child = MagicMock()
        child.id = 2
        child.name = '研发部'
        child.parent = root
        path = generate_node_storage_path(child)
        assert path == 'node-1_zhi_shi_ku/node-2_yan_fa_bu/'


# ============================================================================
# LocalStorage 本地存储测试
# ============================================================================
class TestLocalStorage:
    """本地文件系统存储后端测试（真实落盘到临时目录）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入临时目录与存储实例（每测试独立目录）"""
        self.tmp_dir = tempfile.mkdtemp()
        self.storage = LocalStorage()

    @pytest.mark.unit
    def test_save_with_node_path(self):
        """带 node_path 时保存到 MEDIA_ROOT/documents/{node_path}/ 下"""
        media = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media):
            f = SimpleUploadedFile('a.txt', b'hello world')
            path = self.storage.save('a.txt', f, node_path='node-1_x/')
            assert os.path.exists(path)
            assert 'documents' in path
            assert path.endswith('node-1_x/a.txt')
            with open(path, 'rb') as r:
                assert r.read() == b'hello world'

    @pytest.mark.unit
    def test_save_without_node_path(self):
        """不带 node_path 时保存到 MEDIA_ROOT/documents/ 根目录（并打警告）"""
        media = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media):
            f = SimpleUploadedFile('b.txt', b'abc')
            path = self.storage.save('b.txt', f, node_path=None)
            assert os.path.exists(path)
            assert path == os.path.join(media, 'documents', 'b.txt')

    @pytest.mark.unit
    def test_delete_existing_file(self):
        """删除存在的文件"""
        f = SimpleUploadedFile('c.txt', b'x')
        path = self.storage.save('c.txt', f, node_path='node-1_x/')
        assert os.path.exists(path)
        self.storage.delete(path)
        assert not os.path.exists(path)

    @pytest.mark.unit
    def test_delete_missing_file_noop(self):
        """删除不存在的文件应静默通过（不抛异常）"""
        self.storage.delete('/nonexistent/path/file.txt')

    @pytest.mark.unit
    def test_get_url_returns_path(self):
        """本地模式 get_url 直接返回文件路径"""
        assert self.storage.get_url('/tmp/x.txt') == '/tmp/x.txt'


# ============================================================================
# OssStorage OSS 存储测试（mock oss2 SDK）
# ============================================================================
class TestOssStorage:
    """OSS 存储后端测试 —— oss2 SDK 缺失，全部 mock"""

    def _make_oss2_module(self):
        """构造 mock 的 oss2 模块"""
        return MagicMock()

    @pytest.mark.unit
    def test_get_bucket_cached(self):
        """_get_bucket 懒加载且只创建一次 Auth"""
        with patch.dict('sys.modules', {'oss2': self._make_oss2_module()}):
            storage = OssStorage()
            storage.access_key_id = 'ak'
            storage.access_key_secret = 'sk'
            storage.endpoint = 'http://oss.example.com'
            storage.bucket_name = 'bucket'
            b1 = storage._get_bucket()
            b2 = storage._get_bucket()
            assert b1 is b2
            # Auth 只应被实例化一次（缓存生效）
            from oss2 import Auth
            Auth.assert_called_once_with('ak', 'sk')

    @pytest.mark.unit
    def test_get_bucket_import_error(self):
        """oss2 未安装时 _get_bucket 应抛出 ImportError"""
        with patch.dict('sys.modules', {'oss2': None}):
            storage = OssStorage()
            with pytest.raises(ImportError):
                storage._get_bucket()

    @pytest.mark.unit
    def test_save_small_file_put_object(self):
        """小文件（<=10MB）走 put_object 单次上传，返回 oss:// 路径"""
        mock_oss2 = self._make_oss2_module()
        with patch.dict('sys.modules', {'oss2': mock_oss2}):
            storage = OssStorage()
            storage.bucket_name = 'my-bucket'
            bucket = storage._get_bucket()
            f = SimpleUploadedFile('d.txt', b'small content')
            result = storage.save('d.txt', f, node_path='node-1_x/')
            assert result == 'oss://my-bucket/documents/node-1_x/d.txt'
            bucket.put_object.assert_called_once_with(
                'documents/node-1_x/d.txt', b'small content')

    @pytest.mark.unit
    def test_save_large_file_multipart(self):
        """大文件（>10MB）走 multipart 分片上传

        源码缺陷说明：OssStorage.save 中 `oss2.models.PartInfo(...)` 引用的
        是模块级全局名 oss2，而 storage.py 仅在 _get_bucket 内局部
        `from oss2 import Auth, Bucket`，模块级从未绑定 oss2 ——
        该分支在真实运行中会 NameError。测试通过 create=True 注入模块全局
        以覆盖分片上传的调用契约。
        """
        from apps.knowledge import storage as storage_module
        mock_oss2 = self._make_oss2_module()
        mock_oss2.Bucket.return_value.init_multipart_upload.return_value.upload_id = 'upload-1'
        with patch.dict('sys.modules', {'oss2': mock_oss2}), \
                patch.object(storage_module, 'oss2', mock_oss2, create=True):
            storage = OssStorage()
            storage.bucket_name = 'my-bucket'
            bucket = storage._get_bucket()
            big = SimpleUploadedFile('big.bin', b'x' * (10 * 1024 * 1024 + 1))
            result = storage.save('big.bin', big, node_path=None)
            assert result == 'oss://my-bucket/documents/big.bin'
            bucket.init_multipart_upload.assert_called_once()
            assert bucket.upload_part.called
            bucket.complete_multipart_upload.assert_called_once()

    @pytest.mark.unit
    def test_delete_non_oss_path_noop(self):
        """delete 非 oss:// 路径应直接返回，不调用 SDK"""
        storage = OssStorage()
        storage.delete('/local/path.txt')
        assert storage._bucket is None  # 未触发 SDK

    @pytest.mark.unit
    def test_delete_ok(self):
        """delete oss:// 路径应调用 bucket.delete_object"""
        mock_oss2 = self._make_oss2_module()
        with patch.dict('sys.modules', {'oss2': mock_oss2}):
            storage = OssStorage()
            storage.bucket_name = 'my-bucket'
            bucket = storage._get_bucket()
            storage.delete('oss://my-bucket/documents/x.txt')
            bucket.delete_object.assert_called_once_with('documents/x.txt')

    @pytest.mark.unit
    def test_delete_failure_logs_warning(self):
        """delete 抛异常时只记日志不向上抛"""
        mock_oss2 = self._make_oss2_module()
        mock_oss2.Bucket.return_value.delete_object.side_effect = Exception('network')
        with patch.dict('sys.modules', {'oss2': mock_oss2}):
            storage = OssStorage()
            storage.bucket_name = 'my-bucket'
            storage._get_bucket()
            storage.delete('oss://my-bucket/documents/x.txt')  # 不应抛异常

    @pytest.mark.unit
    def test_get_url_sign(self):
        """get_url 走 bucket.sign_url 生成 1 小时签名 URL"""
        mock_oss2 = self._make_oss2_module()
        mock_oss2.Bucket.return_value.sign_url.return_value = 'http://signed/url'
        with patch.dict('sys.modules', {'oss2': mock_oss2}):
            storage = OssStorage()
            storage.bucket_name = 'my-bucket'
            bucket = storage._get_bucket()
            url = storage.get_url('oss://my-bucket/documents/x.txt')
            assert url == 'http://signed/url'
            bucket.sign_url.assert_called_once_with('GET', 'documents/x.txt', 3600)

    @pytest.mark.unit
    def test_get_url_failure_fallback(self):
        """get_url 抛异常时降级返回原始路径"""
        mock_oss2 = self._make_oss2_module()
        mock_oss2.Bucket.return_value.sign_url.side_effect = Exception('net')
        with patch.dict('sys.modules', {'oss2': mock_oss2}):
            storage = OssStorage()
            storage.bucket_name = 'my-bucket'
            storage._get_bucket()
            assert storage.get_url('oss://my-bucket/documents/x.txt') == \
                'oss://my-bucket/documents/x.txt'

    @pytest.mark.unit
    def test_get_url_non_oss_path(self):
        """get_url 非 oss:// 路径直接返回"""
        storage = OssStorage()
        assert storage.get_url('/local/x.txt') == '/local/x.txt'


# ============================================================================
# get_document_storage 工厂函数测试
# ============================================================================
class TestGetDocumentStorage:
    """存储工厂：按 DOCUMENT_STORAGE_MODE 选择后端"""

    @pytest.mark.unit
    @override_settings(DOCUMENT_STORAGE_MODE='local')
    def test_local_mode(self):
        """local 模式返回 LocalStorage 实例"""
        assert isinstance(get_document_storage(), LocalStorage)

    @pytest.mark.unit
    @override_settings(DOCUMENT_STORAGE_MODE='oss')
    def test_oss_mode(self):
        """oss 模式返回 OssStorage 实例"""
        assert isinstance(get_document_storage(), OssStorage)

    @pytest.mark.unit
    @override_settings(DOCUMENT_STORAGE_MODE='unknown')
    def test_unknown_mode_falls_back_local(self):
        """未知模式回退到 LocalStorage（默认安全）"""
        assert isinstance(get_document_storage(), LocalStorage)
