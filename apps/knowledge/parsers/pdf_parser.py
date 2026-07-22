"""
PDF 深度解析器
- PyMuPDF (fitz) 提取文本 + 表格 + 图片
- 简单页眉页脚过滤（首末行高频重复模式）
- 保留章节路径（Heading 层级）
- 保留页码，便于溯源
"""
from loguru import logger
import re
from collections import Counter
from typing import List, Dict, Any

from .base import BaseParser



class PDFParser(BaseParser):
    name = 'pdf'

    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.error('PyMuPDF 未安装')
            return []

        blocks: List[Dict[str, Any]] = []
        try:
            doc = fitz.open(file_path)
        except Exception as e:
            logger.exception('open pdf fail')
            return []

        page_texts = []
        for page in doc:
            page_texts.append(page.get_text('text') or '')

        # 检测页眉页脚（各页首末非空行取最多的）
        header, footer = self._detect_header_footer(page_texts)

        section_path = ''
        for pnum, page in enumerate(doc, 1):
            text = page_texts[pnum - 1]
            lines = [l for l in text.splitlines() if l.strip()]
            # 过滤页眉页脚
            if header and lines and lines[0].strip() == header:
                lines = lines[1:]
            if footer and lines and lines[-1].strip() == footer:
                lines = lines[:-1]

            # 章节标题识别（"第X章"、"1. Xxx"、"1.1 Xxx"）
            current_section = section_path
            for l in lines:
                if self._is_heading(l):
                    current_section = l.strip()[:64]
                    section_path = current_section
                    break

            para = '\n'.join(lines).strip()
            if para:
                blocks.append({
                    'type': 'text',
                    'content': para,
                    'section_path': section_path,
                    'page_number': pnum,
                    'extra': {'source': 'pdf'},
                })

            # 图片抽取（预留 OCR 字段）
            images = page.get_images(full=False)
            for i, img_info in enumerate(images):
                blocks.append({
                    'type': 'image',
                    'content': f'[图片 P{pnum}#{i+1} xref={img_info[0]}]',
                    'section_path': section_path,
                    'page_number': pnum,
                    'extra': {
                        'xref': img_info[0],
                        'ocr_needed': True,
                        'ocr_text': '',
                        'ocr_confidence': 0.0,
                        'ocr_status': 'pending',
                    },
                })

        doc.close()
        return blocks

    def _detect_header_footer(self, page_texts: List[str]) -> tuple:
        if len(page_texts) < 3:
            return '', ''
        firsts, lasts = [], []
        for t in page_texts:
            lines = [l.strip() for l in t.splitlines() if l.strip()]
            if lines:
                firsts.append(lines[0])
                lasts.append(lines[-1])
        # 阈值：出现在 60% 以上的页
        threshold = max(2, int(len(page_texts) * 0.6))
        first_common = Counter(firsts).most_common(1)
        last_common = Counter(lasts).most_common(1)
        header = first_common[0][0] if first_common and first_common[0][1] >= threshold else ''
        footer = last_common[0][0] if last_common and last_common[0][1] >= threshold else ''
        return header, footer

    _HEADING_RE = re.compile(
        r'^(第[一二三四五六七八九十百千0-9]+[章节篇卷]|(\d+\.){1,3}\d*\s+\S+|[A-Z][A-Z ]{3,}\s*$)'
    )

    def _is_heading(self, line: str) -> bool:
        return bool(self._HEADING_RE.match(line.strip()))
