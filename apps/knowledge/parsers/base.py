"""
解析器基类
所有解析器接收 (file_path, options)，返回 [{'type':str,'content':str,'section_path':str,'page_number':int,'extra':dict}]
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any


class BaseParser(ABC):
    """所有 Parser 都必须实现 parse"""

    name: str = 'base'

    @abstractmethod
    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        raise NotImplementedError


def get_parser(file_type: str) -> 'BaseParser':
    from .pdf_parser import PDFParser
    from .docx_parser import DocxParser
    from .markdown_parser import MarkdownParser
    from .code_parser import CodeParser
    from .config_parser import ConfigParser

    file_type = (file_type or '').lower()
    mapping = {
        'pdf': PDFParser(),
        'docx': DocxParser(),
        'doc': DocxParser(),
        'markdown': MarkdownParser(),
        'md': MarkdownParser(),
        'txt': MarkdownParser(),
        'code': CodeParser(),
        'py': CodeParser(),
        'config': ConfigParser(),
    }
    return mapping.get(file_type, MarkdownParser())
