"""
PDF 解析器

核心改进点：
1. 多栏布局检测 - 分析文本块x坐标分布，自动识别左右栏，避免阅读顺序错乱
2. 智能页眉页脚过滤 - 统计跨页重复文本模式，支持多行页眉页脚识别
3. 跨页句子合并 - 检测页面末尾句子完整性，自动拼接断裂句子
4. 字体信息增强章节检测 - 通过字体大小/加粗自动识别标题层级
5. 合并单元格处理 - 识别跨行跨列单元格，确保表格数据完整性
6. 扫描件PDF检测 - 检测纯图片PDF，标记需要OCR处理
"""
from loguru import logger
import re
import sys
import base64
import statistics
from collections import Counter, defaultdict
from typing import List, Dict, Any, Tuple

from .base import BaseParser


def _get_fitz():
    """获取 PyMuPDF 模块（延迟导入），未安装时返回 None。

    读取顺序：模块属性 fitz → sys.modules → 实际 import。采用延迟导入而非模块级
    固定引用，既保证真实环境取到 fitz，也便于测试用 patch.object(模块, 'fitz') 或
    patch.dict('sys.modules', {'fitz': mock}) 两种方式打桩替换。
    """
    mod = globals().get('fitz')
    if mod is not None:
        return mod
    mod = sys.modules.get('fitz')
    if mod is not None:
        return mod
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    return fitz


class PDFParser(BaseParser):
    name = 'pdf'

    # 页眉页脚检测阈值：出现在超过60%的页面上视为页眉/页脚
    HEADER_FOOTER_THRESHOLD_RATIO = 0.6
    # 多栏布局检测阈值：文本块x坐标标准差超过页面宽度的10%视为多栏
    MULTI_COLUMN_STD_RATIO = 0.1
    # 跨页句子合并阈值：页面末尾小于此长度且不以句号结尾，视为句子断裂
    CROSS_PAGE_MERGE_THRESHOLD = 100
    # 标题字体大小阈值：比平均正文字体大30%以上视为标题
    HEADING_FONT_SIZE_RATIO = 1.3
    # 扫描件PDF检测阈值：页面文本占比低于此值视为图片型PDF
    SCANNED_PDF_TEXT_RATIO = 0.01
    # 单页文本长度低于此值时视为无文本页，触发 OCR（混合型 PDF 中逐页兜底）
    OCR_PAGE_MIN_TEXT_LEN = 20
    # PDF 页渲染缩放系数（2.0 ≈ 144 DPI，兼顾识别精度与请求体积）
    OCR_PAGE_ZOOM = 2.0

    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        """
        PDF解析主入口
        处理流程：
        1. 打开PDF文档
        2. 预扫描所有页面，检测页眉页脚、多栏布局、扫描件类型
        3. 逐页提取文本、表格、图片
        4. 跨页处理：合并断裂句子、合并跨页表格
        5. 返回结构化blocks列表
        """
        fitz = _get_fitz()
        if fitz is None:
            logger.error('[PDFParser] PyMuPDF未安装，无法解析PDF')
            return []

        blocks: List[Dict[str, Any]] = []
        try:
            doc = fitz.open(file_path)
        except Exception as e:
            logger.exception(f'[PDFParser] 打开PDF失败: {file_path}')
            return []

        # 预扫描阶段：获取页面文本、检测页眉页脚、多栏布局等
        page_texts, page_word_info = self._pre_scan(doc)
        header_lines, footer_lines = self._detect_header_footer(page_texts)
        is_multi_column = self._detect_multi_column(page_word_info)
        is_scanned_pdf = self._detect_scanned_pdf(page_texts, doc)

        if is_scanned_pdf:
            logger.warning('[PDFParser] 检测到扫描件PDF，文本提取可能不完整')

        # OCR 能力探测：扫描件/低文本页需要渲染成图片走腾讯云 OCR。
        # 逐页判断而不是仅依赖整份 PDF 检测，可覆盖"文字页+图片页混合"的文档。
        pdf_ocr_on = self._pdf_ocr_ready()
        pdf_ocr_page_limit = 0
        if pdf_ocr_on:
            from ..ocr import pdf_ocr_page_limit as _get_page_limit
            pdf_ocr_page_limit = _get_page_limit()
        ocr_page_count = 0

        # 初始化状态
        section_path = ''
        tables_buffer = []
        # 跨页句子合并缓冲区：存储上一页末尾的不完整句子
        prev_page_trailing_text = ''

        # 逐页解析
        for pnum, page in enumerate(doc, 1):
            # 获取当前页面预处理数据
            text = page_texts[pnum - 1] if pnum - 1 < len(page_texts) else ''
            word_info = page_word_info[pnum - 1] if pnum - 1 < len(page_word_info) else []

            # 过滤页眉页脚
            text = self._filter_header_footer(text, header_lines, footer_lines)
            lines = [l for l in text.splitlines() if l.strip()]

            # 多栏布局处理：按列重新排列文本
            if is_multi_column:
                lines = self._reorder_multi_column(lines, word_info)

            # 跨页句子合并：如果上一页末尾有不完整句子，拼接到当前页开头
            if prev_page_trailing_text and lines:
                lines[0] = prev_page_trailing_text + ' ' + lines[0]
                prev_page_trailing_text = ''

            # 章节标题识别（基于字体信息 + 正则匹配）
            current_section = section_path
            for l in lines:
                if self._is_heading(l, word_info):
                    current_section = l.strip()[:64]
                    section_path = current_section
                    break

            # 提取表格（保留完整结构，处理合并单元格）
            tables = self._extract_tables(page, pnum, section_path)
            for t in tables:
                tables_buffer.append(t)

            # 提取文本块（排除表格区域）
            text_blocks = self._extract_text_blocks(page, text, header_lines, footer_lines,
                                                   pnum, section_path, word_info)
            blocks.extend(text_blocks)

            # 无文本页 OCR 兜底：页面本身没有可提取文字时，渲染成图片走腾讯云 OCR。
            # 受 OCR_PDF_PAGE_LIMIT 限制，避免大文件扫描件产生不可控的成本。
            if pdf_ocr_on and ocr_page_count < pdf_ocr_page_limit:
                page_text_len = len((page_texts[pnum - 1] or '') if pnum - 1 < len(page_texts) else '')
                if page_text_len < self.OCR_PAGE_MIN_TEXT_LEN:
                    ocr_text = self._ocr_page(page, pnum)
                    if ocr_text:
                        blocks.append({
                            'type': 'text',
                            'content': ocr_text,
                            'section_path': section_path,
                            'page_number': pnum,
                            'extra': {'source': 'pdf_ocr', 'ocr_text': ocr_text},
                        })
                        ocr_page_count += 1

            # 提取图片（包含base64数据）
            images = self._extract_images(page, doc, pnum, section_path)
            blocks.extend(images)

            # 检测当前页末尾是否有不完整句子，准备跨页合并
            prev_page_trailing_text = self._detect_trailing_incomplete(text)

        # 跨页表格合并
        if tables_buffer:
            merged_tables = self._merge_cross_page_tables(tables_buffer)
            blocks.extend(merged_tables)

        doc.close()
        return blocks

    def _pre_scan(self, doc) -> Tuple[List[str], List[List[Dict]]]:
        """
        预扫描所有页面
        获取：页面纯文本、单词位置信息
        用途：页眉页脚检测、多栏布局检测、扫描件检测
        """
        page_texts = []
        page_word_info = []

        for page in doc:
            # 获取纯文本
            text = page.get_text('text') or ''
            page_texts.append(text)

            # 获取单词级位置信息（用于多栏检测、字体分析）
            word_info = []
            try:
                words = page.get_text('words')
                for w in words:
                    word_info.append({
                        'text': w[4],
                        'x0': w[0],
                        'y0': w[1],
                        'font_size': w[5],
                        'font_name': w[6],
                        'bold': 'Bold' in w[6] or 'bold' in w[6],
                    })
            except Exception:
                pass
            page_word_info.append(word_info)

        return page_texts, page_word_info

    def _detect_multi_column(self, page_word_info: List[List[Dict]]) -> bool:
        """
        多栏布局检测
        原理：分析所有页面单词的x坐标分布，如果标准差超过页面宽度的10%，视为多栏布局
        解决问题：多栏PDF中，PyMuPDF默认按y坐标顺序提取，导致左右栏文本交叉
        """
        if not page_word_info or not page_word_info[0]:
            return False

        # 获取页面宽度
        sample_words = page_word_info[0]
        if not sample_words:
            return False

        page_width = max(w['x0'] for w in sample_words) * 1.2  # 估算页面宽度

        # 收集所有页面单词的x坐标
        all_x_coords = []
        for word_info in page_word_info:
            for w in word_info:
                all_x_coords.append(w['x0'])

        if len(all_x_coords) < 10:
            return False

        # 计算x坐标标准差
        x_std = statistics.stdev(all_x_coords)
        threshold = page_width * self.MULTI_COLUMN_STD_RATIO

        is_multi = x_std > threshold
        if is_multi:
            logger.info('[PDFParser] 检测到多栏布局')

        return is_multi

    def _reorder_multi_column(self, lines: List[str], word_info: List[Dict]) -> List[str]:
        """
        多栏布局文本重排
        原理：按x坐标将单词分组到不同列，先处理左列再处理右列
        """
        if not word_info:
            return lines

        # 获取页面宽度，确定分栏边界
        x_coords = [w['x0'] for w in word_info]
        if not x_coords:
            return lines

        page_width = max(x_coords) * 1.2
        column_boundary = page_width / 2

        # 按列分组单词
        left_column_words = []
        right_column_words = []
        for w in word_info:
            if w['x0'] < column_boundary:
                left_column_words.append(w)
            else:
                right_column_words.append(w)

        # 按y坐标排序（保持阅读顺序）
        left_column_words.sort(key=lambda w: w['y0'])
        right_column_words.sort(key=lambda w: w['y0'])

        # 拼接文本
        left_text = ' '.join(w['text'] for w in left_column_words)
        right_text = ' '.join(w['text'] for w in right_column_words)

        # 重新分割为行
        combined_text = left_text + '\n' + right_text
        return [l for l in combined_text.split('\n') if l.strip()]

    def _detect_header_footer(self, page_texts: List[str]) -> Tuple[List[str], List[str]]:
        """
        智能页眉页脚检测
        改进点：
        1. 支持多行页眉页脚
        2. 基于频率统计，超过60%页面出现的文本行视为页眉/页脚
        3. 过滤常见页码模式（如 "1/10"、"Page 1"）
        """
        if len(page_texts) < 3:
            return [], []

        # 统计每页前3行和后3行
        top_lines_counter = Counter()
        bottom_lines_counter = Counter()
        page_count = len(page_texts)

        for t in page_texts:
            lines = [l.strip() for l in t.splitlines() if l.strip()]
            if not lines:
                continue

            # 统计前3行（页眉候选）
            for i in range(min(3, len(lines))):
                line = lines[i]
                if self._is_likely_header_footer(line):
                    top_lines_counter[line] += 1

            # 统计后3行（页脚候选）
            for i in range(min(3, len(lines))):
                line = lines[-(i + 1)]
                if self._is_likely_header_footer(line):
                    bottom_lines_counter[line] += 1

        # 阈值：出现在60%以上的页面
        threshold = max(2, int(page_count * self.HEADER_FOOTER_THRESHOLD_RATIO))

        # 筛选符合条件的页眉页脚
        header_lines = [line for line, count in top_lines_counter.items() if count >= threshold]
        footer_lines = [line for line, count in bottom_lines_counter.items() if count >= threshold]

        logger.info(f'[PDFParser] 检测到页眉 {len(header_lines)} 行，页脚 {len(footer_lines)} 行')
        return header_lines, footer_lines

    def _is_likely_header_footer(self, line: str) -> bool:
        """
        判断一行文本是否可能是页眉/页脚
        规则：
        1. 长度适中（2-60字符）
        2. 包含页码模式（如数字、"Page"）
        3. 不包含段落级内容（如长句子、多个标点）
        """
        line = line.strip()
        if len(line) < 2 or len(line) > 60:
            return False

        # 包含页码模式
        if re.search(r'^\d+/?\d*$|^Page\s*\d+|^\d+\s*[–-]\s*\d+$', line, re.IGNORECASE):
            return True

        # 包含文档标题（短文本，不含句号）
        if not re.search(r'[。？！.!?]', line):
            return True

        return False

    def _filter_header_footer(self, text: str, header_lines: List[str], footer_lines: List[str]) -> str:
        """
        过滤页眉页脚
        改进点：支持多行页眉页脚过滤
        """
        lines = [l for l in text.splitlines()]
        filtered_lines = []

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped in header_lines:
                continue
            if stripped in footer_lines:
                continue
            filtered_lines.append(line)

        return '\n'.join(filtered_lines)

    def _detect_trailing_incomplete(self, text: str) -> str:
        """
        检测页面末尾的不完整句子
        原理：页面末尾短文本且不以句号/问号/感叹号结尾，视为句子断裂
        用途：跨页句子合并，避免embedding时句子被截断
        """
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return ''

        last_line = lines[-1]
        # 如果最后一行较短且不以终止标点结尾
        if len(last_line) < self.CROSS_PAGE_MERGE_THRESHOLD and not re.search(r'[。？！.!?]$', last_line):
            return last_line

        return ''

    def _detect_scanned_pdf(self, page_texts: List[str], doc) -> bool:
        """
        检测扫描件PDF（纯图片型PDF）
        原理：计算页面文本占比，如果远低于正常水平则视为扫描件
        """
        if not page_texts:
            return False

        total_text_length = sum(len(t) for t in page_texts)
        num_pages = len(page_texts)

        if num_pages == 0:
            return False

        avg_text_per_page = total_text_length / num_pages
        # 估算页面字符容量（A4页面约5000字符）
        estimated_capacity = 5000

        text_ratio = avg_text_per_page / estimated_capacity

        return text_ratio < self.SCANNED_PDF_TEXT_RATIO

    def _pdf_ocr_ready(self) -> bool:
        """PDF OCR 是否就绪（总开关 + 扫描件自动 OCR 开关，含凭证检查）

        单独探测而非直接在 _ocr_page 内降级，是为了避免对每个低文本页都重复
        走一遍开关/凭证读取；全部关闭时整份文档零成本跳过。
        """
        try:
            from ..ocr import is_pdf_ocr_enabled, get_ocr_client
        except ImportError:
            return False
        if not is_pdf_ocr_enabled():
            return False
        # 凭证未配置时 OCR 不可用，避免逐页空转
        return get_ocr_client() is not None

    def _ocr_page(self, page, pnum: int) -> str:
        """将 PDF 页渲染为图片并调用腾讯云 OCR，返回识别文本；失败/不可用返回空串"""
        fitz = _get_fitz()
        if fitz is None:
            return ''
        try:
            from ..ocr import ocr_image_bytes
            mat = fitz.Matrix(self.OCR_PAGE_ZOOM, self.OCR_PAGE_ZOOM)
            pix = page.get_pixmap(matrix=mat)
            # 透明/CMYK 页面统一转 RGB，保证 PNG 渲染正确
            if pix.n >= 5:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            image_bytes = pix.tobytes('png')
            pix = None  # 及时释放 Pixmap 内存
            return ocr_image_bytes(image_bytes)
        except Exception as e:
            logger.warning(f'[PDFParser] 页面{pnum} OCR 失败: {e}')
            return ''

    def _is_heading(self, line: str, word_info: List[Dict]) -> bool:
        """
        章节标题识别（字体特征 + 正则匹配）
        改进点：
        1. 利用字体大小和加粗特征判断标题
        2. 结合正则匹配提高准确性
        """
        # 先尝试正则匹配
        if self._match_heading_pattern(line):
            return True

        # 再尝试字体特征检测
        if word_info and self._detect_heading_by_font(line, word_info):
            return True

        return False

    def _match_heading_pattern(self, line: str) -> bool:
        """正则匹配标题模式"""
        heading_re = re.compile(
            r'^(第[一二三四五六七八九十百千0-9]+[章节篇卷]|(\d+\.){1,3}\d*\s+\S+|[A-Z][A-Z ]{3,}\s*$)'
        )
        return bool(heading_re.match(line.strip()))

    def _detect_heading_by_font(self, line: str, word_info: List[Dict]) -> bool:
        """
        通过字体特征检测标题
        规则：标题通常比正文大30%以上，且可能加粗
        """
        if not word_info:
            return False

        # 获取当前行的字体信息
        line_words = [w for w in word_info if w['text'] in line]
        if not line_words:
            return False

        # 计算当前行平均字体大小
        line_font_sizes = [w['font_size'] for w in line_words]
        line_avg_size = sum(line_font_sizes) / len(line_font_sizes)
        line_is_bold = any(w['bold'] for w in line_words)

        # 计算页面平均字体大小（正文大小）
        all_font_sizes = [w['font_size'] for w in word_info]
        if len(all_font_sizes) < 10:
            return False

        page_avg_size = sum(all_font_sizes) / len(all_font_sizes)

        # 判断：字体明显更大，或加粗且较大
        if line_avg_size >= page_avg_size * self.HEADING_FONT_SIZE_RATIO:
            return True
        if line_is_bold and line_avg_size >= page_avg_size:
            return True

        return False

    def _extract_tables(self, page, pnum: int, section_path: str) -> List[Dict[str, Any]]:
        """
        提取页面中的表格
        改进点：
        1. 处理合并单元格（跨行跨列）
        2. 保留表格完整结构信息
        3. 添加详细日志便于调试
        """
        tables = []
        try:
            tabs = page.find_tables()
            logger.debug(f'[PDFParser] Page {pnum}: 发现 {len(tabs.tables)} 个表格')

            if tabs.tables:
                for i, tab in enumerate(tabs.tables):
                    table_text = tab.to_markdown()
                    has_header = tab.header is not None

                    # 提取合并单元格信息
                    merge_info = self._extract_merge_info(tab)

                    logger.debug(f'[PDFParser]   Table {i}: {tab.row_count}行 x {tab.col_count}列, '
                                f'has_header={has_header}, merges={len(merge_info)}')

                    tables.append({
                        'type': 'table',
                        'content': table_text,
                        'section_path': section_path,
                        'page_number': pnum,
                        'extra': {
                            'table_index': i,
                            'rows': tab.row_count,
                            'cols': tab.col_count,
                            'has_header': has_header,
                            # PyMuPDF 1.28 的 Table 已移除 is_spanning 属性，
                            # 跨页表格表现为 bbox 超出当前页面上下边界，这里本地计算
                            'is_cross_page': tab.bbox[1] < page.rect.y0 or tab.bbox[3] > page.rect.y1,
                            'merge_info': merge_info,
                        },
                    })
        except Exception as e:
            logger.warning(f'[PDFParser] 页面{pnum}表格提取失败: {e}')

        return tables

    def _extract_merge_info(self, table) -> List[Dict]:
        """
        提取表格合并单元格信息
        返回：合并单元格列表，每个元素包含位置和跨度
        """
        merge_info = []
        try:
            # PyMuPDF 1.28 的 Table 不再支持 table[row][col] 直接取值，
            # 改为基于 extract() 的二维内容网格判断：被合并的单元格在网格中为 None
            for row_idx, row in enumerate(table.extract()):
                for col_idx, cell in enumerate(row):
                    if cell is None:
                        merge_info.append({'row': row_idx, 'col': col_idx, 'span': 'merged'})
        except Exception:
            pass

        return merge_info

    def _extract_text_blocks(self, page, text: str, header_lines: List[str], footer_lines: List[str],
                           pnum: int, section_path: str, word_info: List[Dict]) -> List[Dict[str, Any]]:
        """
        提取页面中的文本块（排除表格区域）
        改进点：
        1. 精确排除表格区域的文本
        2. 保留段落结构
        3. 支持多栏布局后的文本块提取
        """
        blocks = []
        text = self._filter_header_footer(text, header_lines, footer_lines)
        lines = [l for l in text.splitlines() if l.strip()]

        # 获取表格区域边界
        table_rects = []
        try:
            tabs = page.find_tables()
            for tab in tabs.tables:
                table_rects.append(tab.bbox)
        except Exception:
            table_rects = []

        # 按段落分组文本
        text_blocks = []
        current_block = []

        for l in lines:
            # 检查该行是否在表格区域内
            is_in_table = False
            if table_rects and word_info:
                line_words = [w for w in word_info if w['text'] in l]
                for w in line_words:
                    for rect in table_rects:
                        if page.is_inside((w['x0'], w['y0'], w['x0'] + 1, w['y0'] + 1), rect):
                            is_in_table = True
                            break
                    if is_in_table:
                        break

            if is_in_table:
                # 遇到表格区域，结束当前文本块
                if current_block:
                    text_blocks.append('\n'.join(current_block))
                    current_block = []
            else:
                current_block.append(l)

        # 处理最后一个文本块
        if current_block:
            text_blocks.append('\n'.join(current_block))

        # 转换为结构化block
        for i, block_text in enumerate(text_blocks):
            block_text = block_text.strip()
            if block_text:
                blocks.append({
                    'type': 'text',
                    'content': block_text,
                    'section_path': section_path,
                    'page_number': pnum,
                    'extra': {'source': 'pdf', 'text_block_index': i},
                })

        return blocks

    def _extract_images(self, page, doc, pnum: int, section_path: str) -> List[Dict[str, Any]]:
        """
        提取页面中的图片（包含base64数据）
        改进点：
        1. 支持透明背景图片（n >= 5时转换为RGB）
        2. 记录图片尺寸和大小信息
        3. 异常处理，确保单个图片失败不影响其他图片
        """
        images = []
        fitz = _get_fitz()
        if fitz is None:
            return images
        image_list = page.get_images(full=True)
        logger.debug(f'[PDFParser] Page {pnum}: 发现 {len(image_list)} 张图片')

        for i, img_info in enumerate(image_list):
            xref = img_info[0]
            try:
                pix = fitz.Pixmap(doc, xref)

                # 处理透明背景图片（CMYK + Alpha通道）
                if pix.n >= 5:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                # 转换为PNG格式并编码为base64
                img_bytes = pix.tobytes('png')
                img_b64 = base64.b64encode(img_bytes).decode('utf-8')

                logger.debug(f'[PDFParser]   Image {i}: xref={xref}, {pix.width}x{pix.height}, '
                            f'{len(img_bytes)} bytes')

                images.append({
                    'type': 'image',
                    'content': f'[图片 P{pnum}#{i+1}]',
                    'section_path': section_path,
                    'page_number': pnum,
                    'extra': {
                        'xref': xref,
                        'base64_data': img_b64,
                        'width': pix.width,
                        'height': pix.height,
                        'size_bytes': len(img_bytes),
                        'mime_type': 'image/png',
                    },
                })

                # 释放资源
                pix = None

            except Exception as e:
                logger.warning(f'[PDFParser] 页面{pnum}图片{i}提取失败: {e}')
                # 记录失败信息，不中断其他图片提取
                images.append({
                    'type': 'image',
                    'content': f'[图片 P{pnum}#{i+1} xref={xref}]',
                    'section_path': section_path,
                    'page_number': pnum,
                    'extra': {
                        'xref': xref,
                        'base64_data': '',
                        'width': 0,
                        'height': 0,
                        'size_bytes': 0,
                        'mime_type': 'image/png',
                        'error': str(e),
                    },
                })

        return images

    def _merge_cross_page_tables(self, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        合并跨页表格
        改进点：
        1. 基于章节、列数、页眉重复等多维度判断
        2. 自动处理重复页眉的去除
        3. 支持连续多页表格合并
        """
        if len(tables) < 2:
            return tables

        merged = []
        i = 0
        while i < len(tables):
            current = tables[i]

            # 检查是否需要与下一页表格合并
            if i + 1 < len(tables):
                next_table = tables[i + 1]
                if self._is_same_table(current, next_table):
                    # 合并两个表格，然后继续检查是否需要与后续表格合并
                    merged_table = self._merge_two_tables(current, next_table)
                    j = i + 2
                    while j < len(tables):
                        if self._is_same_table(merged_table, tables[j]):
                            merged_table = self._merge_two_tables(merged_table, tables[j])
                            j += 1
                        else:
                            break
                    merged.append(merged_table)
                    i = j
                    continue

            merged.append(current)
            i += 1

        return merged

    def _is_same_table(self, t1: Dict[str, Any], t2: Dict[str, Any]) -> bool:
        """
        判断两个表格是否是同一个跨页表格
        判断条件：
        1. 属于同一章节
        2. 页码连续
        3. 列数相同
        4. 第二页表格没有页眉（或页眉与第一页相同，表示延续）
        """
        # 章节必须相同
        if t1['section_path'] != t2['section_path']:
            return False

        # 页码必须连续
        if t2['page_number'] != t1['page_number'] + 1:
            return False

        # 列数必须相同
        t1_extra = t1.get('extra', {})
        t2_extra = t2.get('extra', {})
        if t1_extra.get('cols') != t2_extra.get('cols'):
            return False

        # 检查页眉重复情况
        t1_lines = t1['content'].strip().split('\n')
        t2_lines = t2['content'].strip().split('\n')
        if len(t1_lines) < 2 or len(t2_lines) < 2:
            return False

        t1_header = t1_lines[0]
        t2_first = t2_lines[0]

        # 如果第一页有页眉，第二页没有页眉 → 可能是同一表格的延续
        if t1_extra.get('has_header') and not t2_extra.get('has_header'):
            # 如果第二页第一行等于第一页页眉，说明是重复页眉，跳过
            if t2_first == t1_header:
                return True
            # 否则直接判断为同一表格
            return True

        # 如果第二页有页眉但与第一页相同，说明是重复页眉，是同一表格
        if t2_first == t1_header:
            return True

        return False

    def _merge_two_tables(self, t1: Dict[str, Any], t2: Dict[str, Any]) -> Dict[str, Any]:
        """
        合并两个跨页表格
        处理逻辑：
        1. 如果第二页开头是重复页眉，去除重复页眉
        2. 合并内容行
        3. 更新行数统计和页面信息
        """
        t1_lines = t1['content'].strip().split('\n')
        t2_lines = t2['content'].strip().split('\n')

        t1_extra = t1.get('extra', {})
        t2_extra = t2.get('extra', {})

        # 去除重复页眉
        if not t2_extra.get('has_header') and t2_lines and t1_lines:
            if t2_lines[0] == t1_lines[0]:
                t2_lines = t2_lines[1:]

        # 合并内容
        merged_content = '\n'.join(t1_lines + t2_lines)

        # 更新元信息
        merged_extra = {
            'table_index': t1_extra.get('table_index'),
            'rows': t1_extra.get('rows', 0) + t2_extra.get('rows', 0),
            'cols': t1_extra.get('cols', 0),
            'has_header': t1_extra.get('has_header', False),
            'is_cross_page': True,
            'pages': self._merge_pages(t1_extra, t2_extra, t1['page_number'], t2['page_number']),
        }

        return {
            'type': 'table',
            'content': merged_content,
            'section_path': t1['section_path'],
            'page_number': t1['page_number'],
            'extra': merged_extra,
        }

    def _merge_pages(self, t1_extra: Dict, t2_extra: Dict, p1: int, p2: int) -> List[int]:
        """合并页面信息，处理连续多页表格的情况"""
        pages = []
        if t1_extra.get('pages'):
            pages.extend(t1_extra['pages'])
        else:
            pages.append(p1)
        if p2 not in pages:
            pages.append(p2)
        return pages