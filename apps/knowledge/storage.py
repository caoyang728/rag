"""
文档存储抽象层
支持本地文件系统和 OSS（阿里云/腾讯云/MinIO 兼容）两种存储模式
通过环境变量 DOCUMENT_STORAGE_MODE 切换
"""
from loguru import logger
import os

from django.conf import settings



class DocumentStorage:
    def save(self, filename, file_obj):
        raise NotImplementedError

    def delete(self, filepath):
        raise NotImplementedError

    def get_url(self, filepath):
        raise NotImplementedError


class LocalStorage(DocumentStorage):
    def save(self, filename, file_obj):
        save_dir = os.path.join(getattr(settings, 'MEDIA_ROOT', '/tmp'), 'documents')
        os.makedirs(save_dir, exist_ok=True)
        fpath = os.path.join(save_dir, filename)
        with open(fpath, 'wb') as w:
            for chunk in file_obj.chunks():
                w.write(chunk)
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

    def save(self, filename, file_obj):
        bucket = self._get_bucket()
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
