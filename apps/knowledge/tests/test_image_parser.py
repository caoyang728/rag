"""
apps.knowledge.parsers.image_parser 单元测试 —— 图片解析器

覆盖范围：
- OCR 识别出文本：text 块（OCR 结果）+ image 块（原图 base64 + ocr_text）
- OCR 关闭/失败（返回空串）：仅 image 块
- 图片文件损坏：返回空列表

纯 pytest + mock（无 DB / 网络）：patch ocr_image_bytes 注入识别结果，
真实文件用 Pillow 在 tmp_path 下生成。
"""
import io
from unittest.mock import patch

import pytest
from PIL import Image

from apps.knowledge.parsers.image_parser import ImageParser


def _make_png_bytes(size=(20, 20), color=(255, 0, 0)) -> bytes:
    """生成一张 PNG 图片字节"""
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'PNG')
    return buf.getvalue()


class TestImageParser:
    def test_parse_when_ocr_returns_text_then_text_and_image_blocks(self, tmp_path):
        """OCR 识别出文本 → 返回 text 块（识别结果）+ image 块（原图）"""
        p = tmp_path / 'a.png'
        p.write_bytes(_make_png_bytes())
        with patch('apps.knowledge.parsers.image_parser.ocr_image_bytes',
                   return_value='图片中的文字'):
            blocks = ImageParser().parse(str(p))

        assert len(blocks) == 2
        text_blk, img_blk = blocks[0], blocks[1]
        assert text_blk['type'] == 'text'
        assert text_blk['content'] == '图片中的文字'
        assert text_blk['page_number'] == 1
        assert img_blk['type'] == 'image'
        assert img_blk['content'] == '[图片]'
        assert img_blk['extra']['base64_data']
        assert img_blk['extra']['mime_type'] == 'image/png'
        assert img_blk['extra']['ocr_text'] == '图片中的文字'
        assert img_blk['extra']['size_bytes'] == len(_make_png_bytes())

    def test_parse_when_ocr_empty_then_only_image_block(self, tmp_path):
        """OCR 关闭/失败（返回空串）→ 仅返回 image 块，不产出空文本块"""
        p = tmp_path / 'a.png'
        p.write_bytes(_make_png_bytes())
        with patch('apps.knowledge.parsers.image_parser.ocr_image_bytes', return_value=''):
            blocks = ImageParser().parse(str(p))

        assert len(blocks) == 1
        assert blocks[0]['type'] == 'image'
        assert blocks[0]['extra']['ocr_text'] == ''

    def test_parse_when_file_invalid_then_returns_empty(self, tmp_path):
        """文件不是合法图片 → 返回空列表，不抛异常"""
        p = tmp_path / 'a.txt'
        p.write_text('not an image')
        assert ImageParser().parse(str(p)) == []

    def test_parse_when_file_missing_then_returns_empty(self, tmp_path):
        """文件不存在 → 返回空列表"""
        assert ImageParser().parse(str(tmp_path / 'missing.png')) == []
