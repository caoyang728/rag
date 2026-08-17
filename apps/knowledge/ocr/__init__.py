"""OCR 服务包（腾讯云通用印刷体识别）

对外暴露入口：
    is_ocr_enabled()           是否启用 OCR（SystemConfig OCR_ENABLED）
    is_pdf_ocr_enabled()       扫描件/低文本页 PDF 是否自动 OCR
    pdf_ocr_page_limit()       单份 PDF 最大 OCR 页数（成本控制）
    get_ocr_client()           腾讯云 OcrClient（未配置凭证返回 None）
    ocr_image_bytes(bytes)     识别图片字节，返回文本
    ocr_image_file(file_path)  识别图片文件，返回文本
"""
from .tencent_ocr import (
    get_ocr_client,
    is_ocr_enabled,
    is_pdf_ocr_enabled,
    ocr_image_bytes,
    ocr_image_file,
    pdf_ocr_page_limit,
)

__all__ = [
    'get_ocr_client',
    'is_ocr_enabled',
    'is_pdf_ocr_enabled',
    'ocr_image_bytes',
    'ocr_image_file',
    'pdf_ocr_page_limit',
]
