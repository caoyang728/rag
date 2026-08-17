"""
图片解析器

- 支持 jpg/jpeg/png/bmp/webp 等图片文件的解析。
- OCR 启用且凭证就绪时：调用腾讯云 OCR 提取文字，产出 text 块（参与向量化/检索），
  同时保留 image 块（原图 base64 存储进 ImageResource，extra.ocr_text 供图片检索）。
- OCR 未启用/失败时：仅返回 image 块，行为与 PDF 内嵌图片一致，不阻断解析流程。
"""
import base64

from loguru import logger
from typing import List, Dict, Any

from .base import BaseParser
from ..ocr import ocr_image_bytes


class ImageParser(BaseParser):
    name = 'image'

    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        """解析图片文件：读取原图信息 → OCR 文本块（可选）→ 图片资源块"""
        image_bytes, mime_type, width, height = self._read_image_info(file_path)
        if image_bytes is None:
            return []

        blocks: List[Dict[str, Any]] = []

        # 先做 OCR：识别出的文本作为独立 text 块参与向量化，图片本身不重复携带文本
        ocr_text = ocr_image_bytes(image_bytes)
        if ocr_text:
            blocks.append({
                'type': 'text',
                'content': ocr_text,
                'section_path': '',
                'page_number': 1,
                'extra': {'source': 'image_ocr', 'ocr_text': ocr_text},
            })

        # 图片资源块：原图 base64 入库，供预览与 ImageResource 落库
        blocks.append({
            'type': 'image',
            'content': '[图片]',
            'section_path': '',
            'page_number': 1,
            'extra': {
                'base64_data': base64.b64encode(image_bytes).decode('utf-8'),
                'mime_type': mime_type,
                'width': width,
                'height': height,
                'size_bytes': len(image_bytes),
                'source': 'image_file',
                'ocr_text': ocr_text,
            },
        })
        return blocks

    def _read_image_info(self, file_path: str):
        """读取图片基础信息（尺寸/MIME）与原始字节；读取失败返回 (None, '', 0, 0)"""
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                fmt = img.format or ''
                mime_type = Image.MIME.get(fmt, 'image/png')
                width, height = img.size
            with open(file_path, 'rb') as f:
                image_bytes = f.read()
            return image_bytes, mime_type, width, height
        except Exception as e:
            logger.warning(f'[ImageParser] 图片读取失败 {file_path}: {e}')
            return None, '', 0, 0
