#!/usr/bin/env python
"""
清理所有文档相关数据（开发环境专用）

包括（按依赖顺序）：
- Celery：清空 broker 消息队列（purge）+ 结果 backend（db2）+ CeleryTaskLog 任务日志
- 图谱：GraphEntity / GraphRelation / GraphCommunity（实体均从文档切片 LLM 抽取而来）
- 文档数据：Document / DocumentChunk / DocumentVector / CodeChunk / ImageResource /
  DocOperationLog
- 权限残留：DOCUMENT 类型的 ResourceShare / ResourceBlockList（逻辑外键，文档删除后成为残留）
- Media：文档物理文件（Document.file_path）及其空目录

注意：本脚本仅允许在开发环境执行，生产环境（DEBUG=False）禁止运行，避免误清空生产知识库数据。
"""
import os
import sys
import django

# 将项目根目录加入 sys.path，保证 rag_project / apps 包可导入（无论从哪个目录执行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')
django.setup()

from django.conf import settings

# 生产环境守卫：DEBUG=False 时直接退出，防止误删生产数据
if not settings.DEBUG:
    print("错误：clean_docs.py 仅允许在开发环境执行，生产环境禁止使用！")
    sys.exit(1)

from rag_project.celery import app as celery_app
from rag_project.config import RedisConfig
from apps.knowledge.models import (
    Document, DocumentChunk, CodeChunk, ImageResource,
    DocOperationLog, ResourceShare, ResourceBlockList, ResourceType,
)
from apps.retrieval.models import DocumentVector
from apps.system.models import CeleryTaskLog
from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity


def clean_celery():
    """清理 Celery：purge 待处理消息、清空结果 backend、删除任务日志"""
    print("=== 清理 Celery 队列与结果集 ===")

    # 1. 清空 broker 消息队列（default/parse/memory/email/analytics 全部 purge）。
    #    purge 会丢弃所有已发布但未消费的消息，防止残留任务再次触发解析。
    try:
        purged = celery_app.control.purge()
        print(f"  purge 待处理任务: {purged} 条")
    except Exception as e:
        print(f"  purge 失败（worker 未启动或网络异常，可稍后重试）: {e}")

    # 2. 清空结果 backend：独立 redis db2，仅存放 Celery 任务结果，可整体清空。
    try:
        import redis
        rb = redis.Redis.from_url(RedisConfig.build_url(db=2))
        keys = rb.keys('*')
        if keys:
            rb.delete(*keys)
        print(f"  清空结果集: {len(keys)} 个 key")
    except Exception as e:
        print(f"  清空结果集失败: {e}")

    # 3. 删除任务日志表记录（历史任务执行/失败痕迹一并清掉）
    log_count = CeleryTaskLog.objects.count()
    CeleryTaskLog.objects.all().delete()
    print(f"  删除 CeleryTaskLog: {log_count} 条")


def clean_graph():
    """清理知识图谱数据：实体/关系/社区均从文档切片 LLM 抽取而来，
    文档清空后这些记录失去来源（source_doc_ids 指向已删除文档），一并清理"""
    print("=== 清理图谱数据 ===")
    rel = GraphRelation.objects.count()
    com = GraphCommunity.objects.count()
    ent = GraphEntity.objects.count()
    # 先删关系与社区（外键依赖实体），最后删实体
    GraphRelation.objects.all().delete()
    GraphCommunity.objects.all().delete()
    GraphEntity.objects.all().delete()
    print(f"  删除 GraphRelation: {rel} 条, GraphCommunity: {com} 条, GraphEntity: {ent} 条")


def clean_media(doc_paths):
    """清理文档物理文件及其空目录。

    doc_paths: 删除 Document 记录前收集的 file_path 列表。
    只删除确实存在的文件，再自下而上清理空目录，避免误删 media 其它内容。
    """
    print("=== 清理 media 物理文件 ===")
    media_root = str(settings.MEDIA_ROOT)
    deleted_files = 0
    dirs = set()
    for fp in doc_paths:
        if not fp:
            continue
        # file_path 可能是绝对路径（/app/media/...）或相对路径，统一解析
        if not os.path.isabs(fp):
            fp = os.path.join(media_root, fp)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
                deleted_files += 1
                dirs.add(os.path.dirname(fp))
            except OSError as e:
                print(f"  删除文件失败 {fp}: {e}")
    # 自底向上清理空目录（最多到 MEDIA_ROOT 为止，避免误删 media 根目录）
    pruned = 0
    for d in sorted(dirs, key=len, reverse=True):
        if not d.startswith(media_root) or d == media_root:
            continue
        try:
            while os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                pruned += 1
                d = os.path.dirname(d)
                if d == media_root or not d.startswith(media_root):
                    break
        except OSError:
            pass
    print(f"  删除物理文件: {deleted_files} 个, 清理空目录: {pruned} 个")


def clean_docs_data():
    """清理文档相关数据库记录（含权限残留与图片资源）"""
    print("=== 清理文档相关数据 ===")

    # DOCUMENT 类型的主动共享/黑名单为逻辑外键，文档删除后成为残留，一并清理
    share_count = ResourceShare.objects.filter(resource_type=ResourceType.DOCUMENT).count()
    ResourceShare.objects.filter(resource_type=ResourceType.DOCUMENT).delete()
    print(f"  删除 DOCUMENT 共享记录: {share_count} 条")

    block_count = ResourceBlockList.objects.filter(resource_type=ResourceType.DOCUMENT).count()
    ResourceBlockList.objects.filter(resource_type=ResourceType.DOCUMENT).delete()
    print(f"  删除 DOCUMENT 黑名单: {block_count} 条")

    vec_count = DocumentVector.objects.count()
    DocumentVector.objects.all().delete()
    print(f"  删除 DocumentVector: {vec_count} 条")

    code_count = CodeChunk.objects.count()
    CodeChunk.objects.all().delete()
    print(f"  删除 CodeChunk: {code_count} 条")

    img_count = ImageResource.objects.count()
    ImageResource.objects.all().delete()
    print(f"  删除 ImageResource: {img_count} 条")

    chunk_count = DocumentChunk.objects.count()
    DocumentChunk.objects.all().delete()
    print(f"  删除 DocumentChunk: {chunk_count} 条")

    log_count = DocOperationLog.objects.count()
    DocOperationLog.objects.all().delete()
    print(f"  删除 DocOperationLog: {log_count} 条")

    doc_count = Document.objects.count()
    Document.objects.all().delete()
    print(f"  删除 Document: {doc_count} 条")


def clean_all_docs():
    """整体清理入口：先收集物理文件路径，再按 Celery → 图谱 → Media → 文档数据顺序清理"""
    # Document 记录删除后就拿不到 file_path 了，必须先收集
    doc_paths = list(Document.objects.exclude(file_path='').values_list('file_path', flat=True))
    clean_celery()
    clean_graph()
    clean_media(doc_paths)
    clean_docs_data()
    print("=== 清理完成 ===")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--confirm':
        clean_all_docs()
    else:
        print("请使用 --confirm 参数确认清理")
        print("示例: python scripts/clean_docs.py --confirm")
