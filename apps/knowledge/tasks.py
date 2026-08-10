"""
文档解析 & 向量化 Celery 任务
完整异步链
task: parse_document -> desensitize -> chunk -> embed -> save
状态机：pending -> parsing -> desensitizing -> chunking -> embedding -> done/failed/embedding_failed
"""
from loguru import logger
import os
import tempfile
import time
import uuid
import hashlib

def _sanitize_content(content: str) -> str:
    """清理内容中的非法字符，特别是 PostgreSQL 不允许的 NUL 字节"""
    if content is None:
        return ''
    if '\x00' in content:
        before_len = len(content)
        content = content.replace('\x00', '')
        after_len = len(content)
        logger.warning(f'[Sanitize] Removed {before_len - after_len} NUL bytes from content')
    return content

def _sanitize_dict(d):
    """递归清理字典中的 NUL 字节"""
    if isinstance(d, dict):
        return {k: _sanitize_dict(v) for k, v in d.items()}
    elif isinstance(d, list):
        return [_sanitize_dict(item) for item in d]
    elif isinstance(d, str):
        return _sanitize_content(d)
    else:
        return d

from celery import shared_task
from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.knowledge.models import Document, DocumentChunk, CodeChunk, KnowledgeNode, ImageResource
from apps.knowledge.parsers.base import get_parser
from apps.knowledge.chunker import chunk_blocks
from apps.knowledge.desensitizer import desensitize
from apps.knowledge.storage import get_document_storage, generate_node_storage_path
from apps.retrieval.vector_store import upsert_vector, delete_by_document
from apps.llm.embedding import get_embedding_client
from apps.users.models import User


def _notify_admin_on_embedding_failure(doc: Document, error_msg: str):
    """
    预留邮件通知接口 - 当embedding失败时通知管理员

    实现逻辑：
    - 查询 EmailSubscription 中订阅 system_notice 且已启用的用户邮箱
    - 逐个发送告警邮件（失败仅记录日志，不阻断主业务）
    - 每次发送记录 EmailSendLog 落库（落库失败同样不阻断），便于审计追溯
    - 无任何订阅者时退化为仅记录日志

    :param doc: 失败的文档对象
    :param error_msg: 错误信息
    """
    from apps.notification.models import EmailSubscription, EmailSendLog

    logger.error(f'[Embedding Notify] embedding失败，文档ID={doc.id}, 文件名={doc.file_name}, 错误={error_msg}')

    # 收集订阅 system_notice 的启用用户邮箱（去重，用户可能多订阅记录）
    emails = set(
        EmailSubscription.objects.filter(
            category='system_notice', is_enabled=True, user__is_deleted=False
        ).exclude(user__email='').values_list('user__email', flat=True)
    )

    # 无订阅者：与旧行为一致，仅日志告警
    if not emails:
        logger.warning(f'[Embedding Notify] 无订阅 system_notice 的用户，跳过邮件通知，文档ID={doc.id}')
        return

    subject = f'【知库 Agent】文档 Embedding 失败 - {doc.file_name}'
    body = (
        '文档 Embedding 失败，请及时排查。\n\n'
        f'文档ID: {doc.id}\n'
        f'文件名: {doc.file_name}\n'
        f'错误信息: {error_msg}\n'
        f'上传时间: {doc.created_at}\n'
        f'上传者: {doc.owner.username if doc.owner else "未知"}'
    )

    from django.conf import settings
    from django.core.mail import send_mail
    for email in emails:
        status = 'success'
        err_msg = ''
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.EMAIL_FROM,
                recipient_list=[email],
                fail_silently=True,
            )
        except Exception as e:
            status = 'failed'
            err_msg = str(e)[:500]
            logger.error(f'[Embedding Notify] 发送邮件失败 {email}: {e}')
        try:
            EmailSendLog.objects.create(
                to_email=email,
                subject=subject[:256],
                body=body,
                category='system_notice',
                status=status,
                error_message=err_msg,
                sent_at=timezone.now() if status == 'success' else None,
            )
        except Exception as e:
            # 邮件日志落库失败不阻断主业务（审计可丢、业务不可丢）
            logger.error(f'[Embedding Notify] EmailSendLog 落库失败: {e}')

    logger.error(f'[Embedding Notify] embedding失败，文档ID={doc.id}，已通知 {len(emails)} 个订阅用户')



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
        logger.info(f'[Parse] doc={doc.id} parsed {len(blocks)} blocks')

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
                desensitized_count += hits
        print(f"  脱敏完成，共处理 {desensitized_count} 个敏感信息")

        # 3. chunking
        print(f"[步骤3/5] 开始切片处理...")
        doc.status = 'chunking'
        doc.save(update_fields=['status'])
        pieces = chunk_blocks(blocks)
        print(f"  切片完成，共 {len(pieces)} 个 chunks")
        logger.info(f'[Parse] doc={doc.id} chunked into {len(pieces)} pieces')

        # 清旧 chunks / 向量 / 代码块元数据 / 图片资源
        print(f"  清理旧 chunks 和向量数据...")
        old_chunk_count = DocumentChunk.objects.filter(document=doc).count()
        # 先删向量（依赖 chunk_id）
        delete_by_document(doc.id)
        # 再删代码块元数据
        CodeChunk.objects.filter(document=doc).delete()
        # 删图片资源
        ImageResource.objects.filter(document=doc).delete()
        # 最后删 chunks
        DocumentChunk.objects.filter(document=doc).delete()
        print(f"  已清理 {old_chunk_count} 个旧 chunks 及对应向量数据")

        # 创建新 chunks
        print(f"  创建新 chunks...")
        chunk_objs = []
        code_meta = []
        image_count = 0
        for i, p in enumerate(pieces):
            sanitized_content = _sanitize_content(p['content'])
            sanitized_extra = _sanitize_dict(p.get('extra') or {})
            chunk_type = p.get('type', 'text')
            extra = sanitized_extra
            
            image_id = None
            if chunk_type == 'image' and extra.get('base64_data'):
                img = ImageResource.objects.create(
                    document=doc,
                    storage_mode='base64',
                    base64_data=extra['base64_data'],
                    mime_type=extra.get('mime_type', 'image/png'),
                    width=extra.get('width', 0),
                    height=extra.get('height', 0),
                    size_bytes=extra.get('size_bytes', 0),
                )
                image_id = img.id
                extra.pop('base64_data', None)
                image_count += 1
            
            ch = DocumentChunk.objects.create(
                document=doc,
                chunk_index=i,
                chunk_type=chunk_type,
                content=sanitized_content[:8000],
                content_length=len(sanitized_content),
                section_path=_sanitize_content(p.get('section_path', ''))[:512],
                page_number=p.get('page_number'),
                image_id=image_id,
                extra=extra,
            )
            chunk_objs.append((ch, p))
            if extra.get('symbol_type'):
                code_meta.append(CodeChunk(
                    document=doc, chunk=ch,
                    language=extra.get('language', 'python'),
                    symbol_type=extra['symbol_type'],
                    symbol_name=_sanitize_content(extra.get('symbol_name', ''))[:128],
                    signature=_sanitize_content(extra.get('signature', ''))[:500],
                    params=extra.get('params') or [],
                    docstring=_sanitize_content(extra.get('docstring', '')),
                    start_line=extra.get('start_line', 0),
                    end_line=extra.get('end_line', 0),
                    parent_symbol=_sanitize_content(extra.get('parent_symbol', ''))[:128],
                ))
        if code_meta:
            CodeChunk.objects.bulk_create(code_meta)
            print(f"  同时创建了 {len(code_meta)} 个代码块元数据")
        if image_count > 0:
            print(f"  同时创建了 {image_count} 个图片资源")
        print(f"  成功创建 {len(chunk_objs)} 个 chunks")

        # 4. embedding & vector upsert
        print(f"[步骤4/5] 开始向量化...")
        doc.status = 'embedding'
        doc.save(update_fields=['status'])
        texts = [p['content'] for _ch, p in chunk_objs]
        client = get_embedding_client()
        print(f"  使用 embedding 客户端: {client.__class__.__name__}")
        print(f"  正在生成 {len(texts)} 个向量...")
        
        try:
            embeddings = client.embed(texts)
        except Exception as e:
            # embedding失败，暂停向量化，标记文档状态
            print(f"  embedding失败: {str(e)}")
            logger.error(f'[Parse] doc={doc.id} embedding failed: {str(e)}')
            
            # 预留邮件通知接口
            _notify_admin_on_embedding_failure(doc, str(e))
            
            # 标记文档状态为embedding_failed（暂停状态，可手动重试）
            doc.status = 'embedding_failed'
            doc.error_message = str(e)[:1000]
            doc.save(update_fields=['status', 'error_message'])
            
            # 抛出异常，触发Celery重试机制
            raise
        
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

        # 文档解析完成，触发图谱抽取 + Wiki 联动（异步，不阻塞解析流程）
        from apps.graph.sync import on_document_done
        from apps.wiki.sync import on_document_done_for_wiki
        on_document_done(doc.id)
        on_document_done_for_wiki(doc.id)
        
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
        logger.exception(f'parse_document fail doc={document_id}')
        doc.status = 'failed'
        doc.error_message = str(e)[:1000]
        doc.save(update_fields=['status', 'error_message'])
        raise
    finally:
        if temp_file and os.path.exists(temp_file.name):
            os.remove(temp_file.name)


@shared_task(name='knowledge.cleanup_deleted_docs', queue='cleanup')
def cleanup_deleted_docs(retention_days: int = 180):
    """
    清理已删除超过指定天数的文档物理文件（自动清理）
    
    默认180天：超过180天的已删除文档将被自动物理删除
    
    :param retention_days: 保留天数，超过此天数的已删除文档将被物理删除
    """
    cutoff_time = timezone.now() - timezone.timedelta(days=retention_days)
    
    deleted_docs = Document.objects.filter(
        is_deleted=True,
        delete_time__isnull=False,
        delete_time__lt=cutoff_time,
        file_path__isnull=False,
        file_path__ne=''
    )
    
    cleaned_count = 0
    failed_count = 0
    failed_paths = []
    
    storage = get_document_storage()
    
    for doc in deleted_docs:
        try:
            storage.delete(doc.file_path)
            doc.file_path = ''
            doc.save(update_fields=['file_path'])
            cleaned_count += 1
            logger.info(f'[Cleanup] Deleted file for doc={doc.id}, file={doc.file_name}')
        except Exception as e:
            failed_count += 1
            failed_paths.append(f'{doc.id}:{doc.file_path}')
            logger.exception(f'[Cleanup] Failed to delete file for doc={doc.id}')
    
    logger.info(f'[Cleanup] Completed: cleaned={cleaned_count}, failed={failed_count}')
    return {
        'ok': True,
        'cleaned': cleaned_count,
        'failed': failed_count,
        'failed_paths': failed_paths[:20]
    }


def _log_batch_import_failure(filename, node_name, error_msg):
    """记录批量导入失败日志"""
    log_dir = os.path.join(settings.BASE_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, 'batch_import_failed.log')
    
    from django.utils import timezone
    timestamp = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"[{timestamp}] FILE: {filename} | NODE: {node_name} | ERROR: {error_msg}\n")


@shared_task(name='knowledge.batch_import_single_file', queue='parse')
def batch_import_single_file(temp_file_path, node_id, owner_id, visibility, owner_team_id, filename):
    """批量导入单个文件的 Celery 任务

    参数：
        temp_file_path: 临时文件路径（脚本上传时创建）
        node_id: 目标节点ID
        owner_id: 上传者ID
        visibility: 可见范围（兼容旧版 int 1-4 或 str team/dept/public，内部归一化为 visibility_level）
        owner_team_id: 团队ID
        filename: 原始文件名
    """
    try:
        # 1. 验证文件
        if not os.path.exists(temp_file_path):
            error_msg = f"临时文件不存在: {temp_file_path}"
            logger.error(f'[BatchImport] {error_msg}')
            _log_batch_import_failure(filename, f"node_id={node_id}", error_msg)
            return {'ok': False, 'error': error_msg}
        
        # 2. 获取节点和上传者
        try:
            node = KnowledgeNode.objects.get(id=node_id, is_deleted=False)
        except KnowledgeNode.DoesNotExist:
            error_msg = f"节点不存在: {node_id}"
            logger.error(f'[BatchImport] {error_msg}')
            _log_batch_import_failure(filename, f"node_id={node_id}", error_msg)
            return {'ok': False, 'error': error_msg}
        
        try:
            owner = User.objects.get(id=owner_id, is_deleted=False)
        except User.DoesNotExist:
            error_msg = f"上传者不存在: {owner_id}"
            logger.error(f'[BatchImport] {error_msg}')
            _log_batch_import_failure(filename, node.name, error_msg)
            return {'ok': False, 'error': error_msg}
        
        # 3. 计算文件哈希（去重）
        file_hash = hashlib.sha256()
        with open(temp_file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                file_hash.update(chunk)
        file_hash = file_hash.hexdigest()
        
        # 检查是否已存在
        if Document.objects.filter(file_hash=file_hash, is_deleted=False).exists():
            error_msg = "文件已存在（重复导入）"
            logger.warning(f'[BatchImport] {error_msg}: {filename}')
            _log_batch_import_failure(filename, node.name, error_msg)
            # 删除临时文件
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            return {'ok': False, 'error': error_msg}
        
        # 4. 获取文件大小和类型
        file_size = os.path.getsize(temp_file_path)
        
        ext_map = {
            '.txt': 'txt', '.md': 'md', '.markdown': 'md',
            '.docx': 'docx', '.doc': 'docx', '.pdf': 'pdf',
            '.wps': 'docx',  # WPS 文字
            '.json': 'json', '.xml': 'xml', '.csv': 'csv',
            '.xlsx': 'xlsx', '.xls': 'xlsx', '.et': 'xlsx',  # WPS 表格
            '.ppt': 'ppt', '.pptx': 'pptx', '.dps': 'pptx',  # WPS 演示
        }
        ext = os.path.splitext(filename)[1].lower()
        file_type = ext_map.get(ext, 'other')
        
        mime_map = {
            '.txt': 'text/plain', '.md': 'text/markdown',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.doc': 'application/msword',
            '.wps': 'application/msword',
            '.pdf': 'application/pdf',
            '.json': 'application/json', '.xml': 'application/xml',
            '.csv': 'text/csv',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.xls': 'application/vnd.ms-excel',
            '.et': 'application/vnd.ms-excel',
            '.ppt': 'application/vnd.ms-powerpoint',
            '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            '.dps': 'application/vnd.ms-powerpoint',
        }
        mime_type = mime_map.get(ext, 'application/octet-stream')
        
        # 5. 保存文件到目标位置
        safe_name = filename.replace("/", "_").replace("\\", "_")
        stored_filename = f"{uuid.uuid4().hex}_{safe_name}"
        
        node_path = generate_node_storage_path(node)
        storage = get_document_storage()
        
        # 读取临时文件内容并保存
        with open(temp_file_path, 'rb') as f:
            file_path = storage.save(stored_filename, f, node_path)
        
        logger.info(f'[BatchImport] 文件已保存: {filename} -> {file_path}')
        
        max_version = Document.objects.filter(
            node=node, file_name=filename, is_deleted=False
        ).aggregate(models.Max('version'))['version__max'] or 0
        version_tag = f'v{max_version + 1}'

        # 可见性归一化：兼容旧版脚本传入的 int(1-4) 或 str(team/dept/public)
        from apps.knowledge.models import VisibilityLevel
        _VIS_INT_MAP = {
            1: VisibilityLevel.TEAM_ONLY,    # private → TEAM_ONLY
            2: VisibilityLevel.DEPT_ONLY,    # department → DEPT_ONLY
            3: VisibilityLevel.TEAM_ONLY,    # team → TEAM_ONLY
            4: VisibilityLevel.PUBLIC,  # public → PUBLIC
        }
        _VIS_STR_MAP = {
            'team': VisibilityLevel.TEAM_ONLY,
            'dept': VisibilityLevel.DEPT_ONLY,
            'department': VisibilityLevel.DEPT_ONLY,
            'public': VisibilityLevel.PUBLIC,
            'private': VisibilityLevel.TEAM_ONLY,
        }
        if isinstance(visibility, int):
            visibility_level = _VIS_INT_MAP.get(visibility, VisibilityLevel.TEAM_ONLY)
        elif isinstance(visibility, str) and visibility in VisibilityLevel.values:
            visibility_level = visibility
        elif isinstance(visibility, str):
            visibility_level = _VIS_STR_MAP.get(visibility.lower(), VisibilityLevel.TEAM_ONLY)
        else:
            visibility_level = VisibilityLevel.TEAM_ONLY

        # 从节点祖先链推导 dept_id（Level 2 节点 ref_id = dept.id）
        dept_id = None
        team_id = owner_team_id
        if node.node_level >= 2:
            ancestors = []
            current = node
            while current:
                ancestors.append(current)
                current = current.parent
            for n in ancestors:
                if n.node_level == 2 and n.ref_id:
                    dept_id = n.ref_id
                elif n.node_level == 3 and n.ref_id and not team_id:
                    # 若调用方未传 owner_team_id，从 Level 3 节点推导
                    team_id = n.ref_id
        # 回退到上传者的主部门
        if not dept_id:
            dept_id = getattr(owner, 'department_id', None)
        if not team_id:
            team_id = getattr(owner, 'team_id', None)
        # 归属约束校验：节点无组织祖先且导入者也无部门/团队归属时，
        # 非 PUBLIC 可见性既无部门/团队可挂靠，也会违反 doc_owner_scope_required 约束。
        # 不能静默降级为 PUBLIC（会造成越权公开），清理已保存文件并报错
        if not dept_id and not team_id and visibility_level != VisibilityLevel.PUBLIC:
            error_msg = (f"节点无组织归属且导入者无部门/团队，仅支持导入为全局公开(PUBLIC)文档: {filename}")
            logger.error(f'[BatchImport] {error_msg}')
            _log_batch_import_failure(filename, node.name, error_msg)
            try:
                storage.delete(file_path)
            except Exception:
                logger.exception(f"Failed to clean up orphan file: {file_path}")
            return {'ok': False, 'error': error_msg}

        doc = Document.objects.create(
            node=node,
            title=filename,
            file_name=filename,
            file_type=file_type,
            file_size=file_size,
            file_hash=file_hash,
            file_path=file_path,
            mime_type=mime_type,
            owner=owner,
            # node(FK) + dept_id + team_id 标识文档归属
            dept_id=dept_id,
            team_id=team_id,
            # visibility_level 控制可见范围
            visibility_level=visibility_level,
            root_type=node.root_type,
            status='pending',
            version=max_version + 1,
            version_tag=version_tag,
        )
        
        logger.info(f'[BatchImport] 文档记录已创建: {filename} (id={doc.id})')
        
        # 7. 删除临时文件
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
            logger.info(f'[BatchImport] 临时文件已删除: {temp_file_path}')
        
        # 8. 触发解析任务
        parse_document.delay(doc.id)
        
        return {'ok': True, 'doc_id': doc.id, 'filename': filename}
    
    except Exception as e:
        error_msg = str(e)[:1000]
        logger.error(f'[BatchImport] 导入失败: {filename} - {error_msg}')
        _log_batch_import_failure(filename, f"node_id={node_id}", error_msg)
        
        # 失败时保留临时文件，方便手动处理
        return {'ok': False, 'error': error_msg}
