#!/usr/bin/env python
"""
清理所有文档相关数据
包括：Document, DocumentChunk, DocumentVector, CodeChunk, DocOperationLog
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')
django.setup()

from apps.knowledge.models import Document, DocumentChunk, CodeChunk, DocOperationLog
from apps.retrieval.models import DocumentVector


def clean_all_docs():
    print("=== 清理文档相关数据 ===")
    
    # 清理向量表
    vec_count = DocumentVector.objects.count()
    DocumentVector.objects.all().delete()
    print(f"  删除 DocumentVector: {vec_count} 条")
    
    # 清理代码块表
    code_count = CodeChunk.objects.count()
    CodeChunk.objects.all().delete()
    print(f"  删除 CodeChunk: {code_count} 条")
    
    # 清理切片表
    chunk_count = DocumentChunk.objects.count()
    DocumentChunk.objects.all().delete()
    print(f"  删除 DocumentChunk: {chunk_count} 条")
    
    # 清理操作日志
    log_count = DocOperationLog.objects.count()
    DocOperationLog.objects.all().delete()
    print(f"  删除 DocOperationLog: {log_count} 条")
    
    # 清理文档表（级联删除会清理关联数据，但这里先清理关联表）
    doc_count = Document.objects.count()
    Document.objects.all().delete()
    print(f"  删除 Document: {doc_count} 条")
    
    print("=== 清理完成 ===")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--confirm':
        clean_all_docs()
    else:
        print("请使用 --confirm 参数确认清理")
        print("示例: python scripts/clean_docs.py --confirm")
