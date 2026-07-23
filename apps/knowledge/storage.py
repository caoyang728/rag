"""
文档存储抽象层
支持本地文件系统和 OSS（阿里云/腾讯云/MinIO 兼容）两种存储模式
通过环境变量 DOCUMENT_STORAGE_MODE 切换
支持按节点路径存储文件
"""
from loguru import logger
import os
import re

from django.conf import settings


def generate_node_storage_path(node) -> str:
    """
    根据节点生成存储路径
    
    路径格式：node-{id}_{safe_name}/
    - 如果节点名为中文，转换为拼音或英文
    - 如果名称已存在，添加数字后缀
    
    :param node: KnowledgeNode 对象
    :return: 相对存储路径（不含文件名）
    """
    # 获取完整的节点路径链（从根节点到当前节点）
    path_parts = []
    current = node
    original_names = []
    
    # 向上遍历获取路径
    while current:
        # 记录原始节点名用于日志
        original_names.insert(0, current.name)
        # 生成安全的节点名
        safe_name = _safe_node_name(current.name, current.id)
        path_parts.insert(0, f"node-{current.id}_{safe_name}")
        current = current.parent
    
    result = '/'.join(path_parts) + '/'
    logger.info('[Storage] node path generated: %s -> %s', ' > '.join(original_names), result)
    return result


def _safe_node_name(name: str, node_id: int) -> str:
    """
    生成安全的节点文件名
    
    规则：
    1. 如果名称是中文，转换为拼音（如果可用）或使用节点ID
    2. 如果名称是英文，保持不变（但去除特殊字符）
    3. 如果名称为空或无法转换，使用节点ID
    4. 长度限制为32字符
    
    :param name: 原始节点名
    :param node_id: 节点ID（用于降级）
    :return: 安全的文件名
    """
    if not name:
        return str(node_id)
    
    # 去除特殊字符
    safe = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]', '_', name).strip('_')
    
    if not safe:
        return str(node_id)
    
    # 判断是否包含中文
    has_chinese = any('\u4e00' <= c <= '\u9fff' for c in safe)
    
    if not has_chinese:
        # 纯英文/数字，直接使用
        return safe[:32]
    
    # 尝试将中文转换为拼音
    try:
        from pypinyin import lazy_pinyin
        pinyin_parts = lazy_pinyin(safe)
        pinyin_name = '_'.join(pinyin_parts)
        # 去除多余的下划线
        pinyin_name = re.sub(r'_+', '_', pinyin_name).strip('_')
        return pinyin_name[:32] or str(node_id)
    except ImportError:
        # pypinyin 未安装，使用节点ID
        logger.warning('[Storage] pypinyin not installed, using node_id instead of Chinese name')
        return str(node_id)



class DocumentStorage:
    def save(self, filename, file_obj, node_path=None):
        raise NotImplementedError

    def delete(self, filepath):
        raise NotImplementedError

    def get_url(self, filepath):
        raise NotImplementedError


class LocalStorage(DocumentStorage):
    def save(self, filename, file_obj, node_path=None):
        base_dir = os.path.join(getattr(settings, 'MEDIA_ROOT', '/tmp'), 'documents')
        # 如果有节点路径，构建完整路径
        if node_path:
            save_dir = os.path.join(base_dir, node_path)
        else:
            save_dir = base_dir
            logger.warning('[Storage] LocalStorage.save called without node_path! file will be saved to root directory')
        logger.info('[Storage] LocalStorage.save: base_dir=%s, node_path=%s, save_dir=%s, filename=%s',
                    base_dir, node_path, save_dir, filename)
        os.makedirs(save_dir, exist_ok=True)
        fpath = os.path.join(save_dir, filename)
        with open(fpath, 'wb') as w:
            for chunk in file_obj.chunks():
                w.write(chunk)
        logger.info('[Storage] LocalStorage.save: file saved to %s', fpath)
        return fpath

    def delete(self, filepath):
        if os.path.exists(filepath):
            os.remove(filepath)

    def get_url(self, filepath):
        return filepath


class OssStorage(DocumentStorage):
    def __init__(self):
        self.endpoint = getattr(settings, 'OSS_ENDPOINT', '')
        self.access_key_id = getattr(settings, 'OSS_ACCESS_KEY_ID', '')
        self.access_key_secret = getattr(settings, 'OSS_ACCESS_KEY_SECRET', '')
        self.bucket_name = getattr(settings, 'OSS_BUCKET_NAME', '')
        self.region = getattr(settings, 'OSS_REGION', '')
        self._bucket = None

    def _get_bucket(self):
        if self._bucket:
            return self._bucket
        
        try:
            from oss2 import Auth, Bucket
            auth = Auth(self.access_key_id, self.access_key_secret)
            self._bucket = Bucket(auth, self.endpoint, self.bucket_name)
            return self._bucket
        except ImportError:
            logger.error('oss2 not installed')
            raise

    def save(self, filename, file_obj, node_path=None):
        bucket = self._get_bucket()
        # 如果有节点路径，构建完整路径
        if node_path:
            key = f'documents/{node_path}{filename}'
        else:
            key = f'documents/{filename}'
        
        content = b''
        for chunk in file_obj.chunks():
            content += chunk
        
        if len(content) > 10 * 1024 * 1024:
            upload_id = bucket.init_multipart_upload(key).upload_id
            parts = []
            part_number = 1
            chunk_size = 10 * 1024 * 1024
            
            for i in range(0, len(content), chunk_size):
                chunk = content[i:i + chunk_size]
                result = bucket.upload_part(key, upload_id, part_number, chunk)
                parts.append(oss2.models.PartInfo(part_number, result.etag))
                part_number += 1
            
            bucket.complete_multipart_upload(key, upload_id, parts)
        else:
            bucket.put_object(key, content)
        
        return f'oss://{self.bucket_name}/{key}'

    def delete(self, filepath):
        if not filepath.startswith('oss://'):
            return
        
        key = filepath.replace(f'oss://{self.bucket_name}/', '')
        try:
            bucket = self._get_bucket()
            bucket.delete_object(key)
        except Exception as e:
            logger.warning(f'oss delete failed: {e}')

    def get_url(self, filepath):
        if not filepath.startswith('oss://'):
            return filepath
        
        key = filepath.replace(f'oss://{self.bucket_name}/', '')
        try:
            bucket = self._get_bucket()
            return bucket.sign_url('GET', key, 3600)
        except Exception as e:
            logger.warning(f'oss sign_url failed: {e}')
            return filepath


def get_document_storage():
    mode = getattr(settings, 'DOCUMENT_STORAGE_MODE', 'local')
    if mode == 'oss':
        return OssStorage()
    return LocalStorage()
