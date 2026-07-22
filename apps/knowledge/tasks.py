"""
文档解析 & 向量化 Celery 任务
完整异步链
task: parse_document -> desensitize -> chunk -> embed -> save
状态机：pending -> parsing -> desensitizing -> chunking -> embedding -> done/failed
"""
from loguru import logger
import os
import tempfile
import time
from typing import List

from celery import shared_task

from apps.knowledge.models import Document, DocumentChunk, CodeChunk
from apps.knowledge.parsers.base import get_parser
from apps.knowledge.chunker import chunk_blocks
from apps.knowledge.desensitizer import desensitize
from apps.knowledge.storage import get_document_storage
from apps.retrieval.vector_store import upsert_vector
from apps.llm.embedding import get_embedding_client



@shared_task(name='knowledge.parse_document', queue='parse',
             autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def parse_document(document_id: int):
    """完整解析 pipeline"""
    t0 = time.time()
    try:
        doc = Document.objects.get(id=document_id)
    except Document.DoesNotExist:
        return {'ok': False, 'error': 'doc not found'}

    temp_file = None
    try:
        print(f"========== 开始解析文档 [{doc.id}] {doc.file_name} ==========")
        
        local_path = doc.file_path
        if doc.file_path.startswith('oss://'):
            storage = get_document_storage()
            url = storage.get_url(doc.file_path)
            import urllib.request
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(doc.file_name)[1])
            with urllib.request.urlopen(url) as response:
                temp_file.write(response.read())
            temp_file.close()
            local_path = temp_file.name
            print(f"  OSS文件已下载到临时文件: {local_path}")
        
        # 1. parsing
        print(f"[步骤1/5] 开始解析文档...")
        doc.status = 'parsing'
        doc.save(update_fields=['status'])
        parser = get_parser(doc.file_type)
        print(f"  使用解析器: {parser.__class__.__name__}")
        blocks = parser.parse(local_path)
        print(f"  解析完成，共 {len(blocks)} 个 blocks")
        logger.info('[Parse] doc=%d parsed %d blocks', doc.id, len(blocks))

        # 2. desensitizing
        print(f"[步骤2/5] 开始脱敏处理...")
        doc.status = 'desensitizing'
        doc.save(update_fields=['status'])
        desensitized_count = 0
        for blk in blocks:
            new_content, hits = desensitize(blk['content'])
            if hits:
                blk['content'] = new_content
                blk.setdefault('extra', {})['desensitized_hits'] = hits
                desensitized_count += len(hits)
        print(f"  脱敏完成，共处理 {desensitized_count} 个敏感信息")

        # 3. chunking
        print(f"[步骤3/5] 开始切片处理...")
        doc.status = 'chunking'
        doc.save(update_fields=['status'])
        pieces = chunk_blocks(blocks)
        print(f"  切片完成，共 {len(pieces)} 个 chunks")
        logger.info('[Parse] doc=%d chunked into %d pieces', doc.id, len(pieces))

        # 清旧 chunks
        print(f"  清理旧 chunks...")
        old_count = DocumentChunk.objects.filter(document=doc).count()
        DocumentChunk.objects.filter(document=doc).delete()
        print(f"  已清理 {old_count} 个旧 chunks")

        # 创建新 chunks
        print(f"  创建新 chunks...")
        chunk_objs = []
        code_meta = []
        for i, p in enumerate(pieces):
            ch = DocumentChunk.objects.create(
                document=doc,
                chunk_index=i,
                chunk_type=p.get('type', 'text'),
                content=p['content'][:8000],
                content_length=len(p['content']),
                section_path=p.get('section_path', '')[:512],
                page_number=p.get('page_number'),
                extra=p.get('extra') or {},
            )
            chunk_objs.append((ch, p))
            extra = p.get('extra') or {}
            if extra.get('symbol_type'):
                code_meta.append(CodeChunk(
                    document=doc, chunk=ch,
                    language=extra.get('language', 'python'),
                    symbol_type=extra['symbol_type'],
                    symbol_name=extra.get('symbol_name', '')[:128],
                    signature=extra.get('signature', '')[:500],
                    params=extra.get('params') or [],
                    docstring=extra.get('docstring', ''),
                    start_line=extra.get('start_line', 0),
                    end_line=extra.get('end_line', 0),
                    parent_symbol=extra.get('parent_symbol', '')[:128],
                ))
        if code_meta:
            CodeChunk.objects.bulk_create(code_meta)
            print(f"  同时创建了 {len(code_meta)} 个代码块元数据")
        print(f"  成功创建 {len(chunk_objs)} 个 chunks")

        # 4. embedding & vector upsert
        print(f"[步骤4/5] 开始向量化...")
        doc.status = 'embedding'
        doc.save(update_fields=['status'])
        texts = [p['content'] for _ch, p in chunk_objs]
        client = get_embedding_client()
        print(f"  使用 embedding 客户端: {client.__class__.__name__}")
        print(f"  正在生成 {len(texts)} 个向量...")
        embeddings = client.embed(texts)
        print(f"  向量生成完成，维度: {len(embeddings[0]) if embeddings else 0}")
        assert len(embeddings) == len(chunk_objs)

        print(f"  正在写入向量数据库...")
        for idx, ((ch, p), emb) in enumerate(zip(chunk_objs, embeddings)):
            upsert_vector(ch, emb)
            if (idx + 1) % 10 == 0 or idx == len(chunk_objs) - 1:
                print(f"    已写入 {idx + 1}/{len(chunk_objs)}")
        print(f"  向量写入完成")

        # 5. done
        print(f"[步骤5/5] 完成处理...")
        doc.status = 'done'
        doc.chunk_count = len(chunk_objs)
        doc.error_message = ''
        doc.save(update_fields=['status', 'chunk_count', 'error_message', 'updated_at'])
        
        if not getattr(settings, 'DOCUMENT_RETENTION_ENABLED', True):
            print(f"  清理原始文件...")
            storage = get_document_storage()
            storage.delete(doc.file_path)
            doc.file_path = ''
            doc.save(update_fields=['file_path'])

        elapsed = int((time.time() - t0) * 1000)
        print(f"========== 文档 [{doc.id}] 解析完成 ==========")
        print(f"  文件: {doc.file_name}")
        print(f"  生成 chunks: {len(chunk_objs)}")
        print(f"  耗时: {elapsed}ms")
        print(f"==============================================")

        return {'ok': True, 'doc_id': doc.id, 'chunks': len(chunk_objs),
                'elapsed_ms': elapsed}

    except Exception as e:
        print(f"========== 文档 [{doc.id}] 解析失败 ==========")
        print(f"  错误: {str(e)}")
        print(f"==============================================")
        logger.exception('parse_document fail doc=%d', document_id)
        doc.status = 'failed'
        doc.error_message = str(e)[:1000]
        doc.save(update_fields=['status', 'error_message'])
        raise
    finally:
        if temp_file and os.path.exists(temp_file.name):
            os.remove(temp_file.name)
