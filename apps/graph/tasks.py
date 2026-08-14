"""知识图谱 Celery 任务"""
import json
import time

from celery import current_task, shared_task
from celery.exceptions import SoftTimeLimitExceeded
from loguru import logger

# 单次任务最多处理的文档数，避免切片过多时超时（每文档可能有数十上百个切片，
# 每个切片需一次 LLM 调用，总耗时不可预估；超时后 SoftTimeLimitExceeded 会中断循环，
# 剩余文档回退 pending 并由本任务末尾自动续派下一轮）
MAX_DOCS_PER_TASK = 5

# 时间预算安全缓冲（秒）：任务实际可用处理时间 = 软超时 - 缓冲。
# 预留缓冲是为了主动在软超时前退出（正常路径），避开 Celery 硬超时直接
# SIGKILL 子进程——硬超时不会执行 except 分支，extracting 状态将无法回退。
SAFETY_BUFFER_SEC = 120

# 抽取进度 / 节点活跃标记的 Redis key 与 TTL
_PROGRESS_KEY = 'graph:progress:{doc_id}'
_ACTIVE_KEY = 'graph:active:{node_id}'
# 进度保留 7 天：大文档续传可能跨多天完成；版本不匹配时进度作废
_PROGRESS_TTL_SEC = 7 * 24 * 3600
# 活跃标记 30 分钟：单次任务实际耗时约 7 分钟（软超时 540s - 缓冲），
# 30 分钟足以覆盖整个任务周期，任务结束即删除
_ACTIVE_TTL_SEC = 1800


# ---------------------------------------------------------------------------
# Redis 辅助：进度/活跃标记读写（全部容忍 Redis 不可用，失败时降级不报错）
# ---------------------------------------------------------------------------
def _redis():
    """获取默认 Redis 连接；不可用时返回 None（调用方降级处理）"""
    try:
        from django_redis import get_redis_connection
        return get_redis_connection('default')
    except Exception:
        return None


def _get_doc_progress(doc_id):
    """读取文档抽取进度；Redis 不可用或记录缺失返回 None"""
    try:
        conn = _redis()
        if conn is None:
            return None
        raw = conn.get(_PROGRESS_KEY.format(doc_id=doc_id))
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning(f'[Graph Task] 读取抽取进度失败 doc={doc_id}: {e}')
        return None


def _set_doc_progress(doc_id: int, chunk_index: int, version: int) -> None:
    """记录文档抽取进度（切片序号 + 文档版本）

    文档重新上传（版本变化）后旧进度作废，下次抽取从头开始。
    """
    try:
        conn = _redis()
        if conn is None:
            return
        conn.set(_PROGRESS_KEY.format(doc_id=doc_id),
                 json.dumps({'chunk_index': chunk_index, 'version': version}),
                 ex=_PROGRESS_TTL_SEC)
    except Exception as e:
        logger.warning(f'[Graph Task] 写入抽取进度失败 doc={doc_id}: {e}')


def _clear_doc_progress(doc_id: int) -> None:
    """清除文档抽取进度（文档抽取完成 / 更新 / 删除时调用）"""
    try:
        conn = _redis()
        if conn is None:
            return
        conn.delete(_PROGRESS_KEY.format(doc_id=doc_id))
    except Exception as e:
        logger.warning(f'[Graph Task] 清除抽取进度失败 doc={doc_id}: {e}')


def _mark_node_active(node_id: int) -> None:
    """记录节点任务进行中标记（自愈任务据此判断该节点任务是否存活）"""
    try:
        conn = _redis()
        if conn is None:
            return
        conn.set(_ACTIVE_KEY.format(node_id=node_id), '1', ex=_ACTIVE_TTL_SEC)
    except Exception as e:
        logger.warning(f'[Graph Task] 活跃标记写入失败 node={node_id}: {e}')


def _clear_node_active(node_id: int) -> None:
    """清除节点活跃标记（任务结束/异常时调用，防止标记残留误判）"""
    try:
        conn = _redis()
        if conn is None:
            return
        conn.delete(_ACTIVE_KEY.format(node_id=node_id))
    except Exception as e:
        logger.warning(f'[Graph Task] 活跃标记清除失败 node={node_id}: {e}')


def _node_active(node_id: int) -> bool:
    """节点是否存在进行中任务（自愈任务判定卡死用）

    Redis 不可用时返回 True（保守判定"有任务"），避免自愈误回退正在抽取的文档。
    """
    try:
        conn = _redis()
        if conn is None:
            return True
        return bool(conn.exists(_ACTIVE_KEY.format(node_id=node_id)))
    except Exception as e:
        logger.warning(f'[Graph Task] 活跃标记读取失败 node={node_id}: {e}')
        return True


@shared_task(queue='parse')
def graph_extract_task(node_id: int):
    """按节点防抖合并的图谱抽取任务：处理该节点下待抽取文档（分批 + 可续传）

    节点内多文档连续完成时合并为一次任务（防抖派发见 graph/sync.py）：
    1. 先写活跃标记，再清除节点待处理标记（graph_pending），任务崩溃后不残留，
       后续文档可重新触发；活跃标记供自愈任务判断任务是否存活
    2. 收集该节点所有 graph_status='pending' 的已完成文档（小文档优先）
    3. 统一标记 extracting，避免并发任务重复处理同一批文档
    4. 逐文档抽取，结果回写 done/failed；单次任务最多处理 MAX_DOCS_PER_TASK 个
    5. 时间预算控制：任务在软超时前主动退出（正常路径），避免被硬超时 SIGKILL
       导致 extracting 状态无法回退；大文档通过 Redis 进度续传，一次处理一部分、
       下一轮任务接着处理，剩余文档回退 pending 并续派
    6. 超时/崩溃兜底：仍在 extracting 的文档回退 pending，由续派或自愈任务恢复

    Args:
        node_id: 知识节点 ID
    """
    from apps.knowledge.models import Document, KnowledgeNode

    # 先标记活跃再清 graph_pending，消除自愈任务在"清标记与写活跃"间隙误判的窗口
    _mark_node_active(node_id)
    KnowledgeNode.objects.filter(id=node_id).update(graph_pending=False)

    from apps.graph.sync import _graph_enabled, _clean_graph_data
    if not _graph_enabled():
        Document.objects.filter(node_id=node_id, graph_status='pending').update(graph_status='skipped')
        _clear_node_active(node_id)
        return {'ok': True, 'processed': 0, 'skipped': True}

    # 本次任务时间预算：软超时提前 SAFETY_BUFFER 退出，正常路径不走硬超时
    try:
        soft_limit = current_task.request.timelimit[0] or 540
    except Exception:
        soft_limit = 540
    started = time.monotonic()
    deadline = started + max(soft_limit - SAFETY_BUFFER_SEC, 60)

    # 取待处理文档，切片少者优先：单次任务可完成更多文档，大文档靠续传逐轮推进
    docs = list(Document.objects.filter(
        node_id=node_id, is_deleted=False, status='done', graph_status='pending'
    ).order_by('chunk_count')[:MAX_DOCS_PER_TASK])

    if not docs:
        _clear_node_active(node_id)
        return {'ok': True, 'processed': 0}

    # 仅标记本批次文档为 extracting，而非全部 pending 文档
    doc_ids = [d.id for d in docs]
    Document.objects.filter(id__in=doc_ids).update(graph_status='extracting')

    from apps.graph.extractor import batch_extract_for_document
    processed = 0
    failed = 0
    timed_out = False
    try:
        for doc in docs:
            # 预算耗尽：不再开始新文档，剩余 extracting 文档回退 pending 交下一轮任务
            if time.monotonic() > deadline:
                timed_out = True
                break
            try:
                # 有匹配版本的进度则续传（跳过清理与已抽切片），否则清理后从头抽取
                progress = _get_doc_progress(doc.id)
                if progress and progress.get('version') == doc.version:
                    start_chunk = int(progress.get('chunk_index') or 0)
                else:
                    start_chunk = 0
                    _clean_graph_data(doc.id)
                result = batch_extract_for_document(doc.id, start_chunk=start_chunk, deadline=deadline)
                if result.get('completed'):
                    _clear_doc_progress(doc.id)
                    Document.objects.filter(id=doc.id).update(graph_status='done')
                    processed += 1
                else:
                    # 大文档预算耗尽未完成：保存续传进度，回退 pending 交续派任务继续
                    _set_doc_progress(doc.id, result.get('next_chunk', start_chunk), doc.version)
                    Document.objects.filter(id=doc.id).update(graph_status='pending')
                    timed_out = True
                    logger.info(
                        f'[Graph Task] 节点 {node_id} 文档 {doc.id} 预算耗尽，'
                        f'进度 {start_chunk}->{result.get("next_chunk", start_chunk)}，续派继续')
                    break
            except SoftTimeLimitExceeded:
                # 软超时兜底：当前文档回退 pending，等待续派任务重试
                timed_out = True
                Document.objects.filter(id=doc.id).update(graph_status='pending')
                logger.warning(f'[Graph Task] 节点 {node_id} 文档 {doc.id} 软超时，回退 pending')
                break
            except Exception as e:
                failed += 1
                logger.exception(f'[Graph Task] 文档 {doc.id} 图谱抽取失败: {e}')
                Document.objects.filter(id=doc.id).update(graph_status='failed')
    except Exception as e:
        # 外层兜底：批量级异常（如 LLM 整体不可用），本批已标记 extracting 的
        # 文档全部回退 pending，避免卡死；后续任务或自愈任务会重试
        logger.exception(f'[Graph Task] 节点 {node_id} 批量抽取异常: {e}')
        Document.objects.filter(
            node_id=node_id, graph_status='extracting', is_deleted=False
        ).update(graph_status='pending')
        timed_out = True
    finally:
        _clear_node_active(node_id)

    # 提前结束后，将本批已标记 extracting 但未处理到的文档回退 pending
    if timed_out:
        remaining = Document.objects.filter(
            node_id=node_id, graph_status='extracting', is_deleted=False
        ).update(graph_status='pending')
        if remaining:
            logger.info(f'[Graph Task] 节点 {node_id} 提前结束，{remaining} 个文档回退 pending')

    # 检查是否还有待处理文档，有则续派任务（避免依赖外部触发）
    has_more = Document.objects.filter(
        node_id=node_id, is_deleted=False, status='done', graph_status='pending'
    ).exists()
    if has_more:
        graph_extract_task.delay(node_id)
        logger.info(f'[Graph Task] 节点 {node_id} 还有待处理文档，续派新任务')

    logger.info(f'[Graph Task] 节点 {node_id} 图谱抽取完成: 成功 {processed}, 失败 {failed}, 共 {len(docs)}, 超时={timed_out}')
    return {'ok': True, 'processed': processed, 'failed': failed, 'total': len(docs), 'timed_out': timed_out}


@shared_task(queue='default')
def graph_recover_task():
    """兜底自愈：扫描卡死的图谱构建状态并恢复派发

    触发场景：图谱抽取任务被硬超时（SIGKILL）/worker 崩溃杀死后，
    extracting 状态无法回退、pending 文档失去触发源（原任务续派逻辑未执行）。
    本任务周期性扫描（默认每 5 分钟）：
    1. 卡死 extracting：所在节点无任务在跑（graph_pending=False）且无活跃标记
       （Redis graph:active 缺失）→ 回退 pending 并重新派发
    2. 无触发源的 pending：节点有待处理文档但无任务在跑 → 补派节点任务

    与正常任务互不干扰：任务运行期间 graph_pending=True 或活跃标记存在，
    扫描会跳过该节点，不会误回退正在抽取的文档。
    """
    from apps.graph.sync import _graph_enabled

    if not _graph_enabled():
        return {'ok': True, 'recovered': 0, 'dispatched_nodes': 0, 'skipped': True}

    stats = _recover_stuck_graph_docs()
    logger.info(f'[Graph Task] 自愈扫描完成: {stats}')
    return {'ok': True, **stats}


def _recover_stuck_graph_docs(force: bool = False) -> dict:
    """扫描并恢复卡死的图谱构建状态（自愈任务与 recover_graph 管理命令共用）

    卡死判定：节点 graph_pending=False 且无活跃标记（Redis graph:active 缺失）；
    force=True 时跳过活跃标记检查（供人工运维在确认 worker 空闲时使用）。

    Args:
        force: 是否忽略活跃标记检查，直接恢复

    Returns:
        {'recovered': 回退并重新派发的文档数, 'dispatched_nodes': 补派节点数}
    """
    from apps.knowledge.models import Document, KnowledgeNode

    recovered = 0
    dispatched_nodes = 0

    def _dispatch(node_id: int) -> bool:
        """原子 check-and-set 派发节点任务：仅当节点当前无任务时置位并派发"""
        dispatched = KnowledgeNode.objects.filter(
            id=node_id, is_deleted=False, graph_pending=False
        ).update(graph_pending=True)
        if dispatched:
            graph_extract_task.delay(node_id)
            return True
        return False

    # 1. 回退卡死的 extracting 文档（节点无任务在跑且无活跃标记）
    stuck_candidates = set(Document.objects.filter(
        status='done', is_deleted=False, graph_status='extracting'
    ).values_list('node_id', flat=True))
    for node_id in stuck_candidates:
        if not force and _node_active(node_id):
            continue
        node = KnowledgeNode.objects.filter(id=node_id, is_deleted=False).first()
        if node is None or node.graph_pending:
            continue
        count = Document.objects.filter(
            node_id=node_id, status='done', is_deleted=False, graph_status='extracting'
        ).update(graph_status='pending')
        if _dispatch(node_id):
            recovered += count
            dispatched_nodes += 1
            logger.info(f'[Graph Task] 自愈: 节点 {node_id} 回退 {count} 个 extracting 文档并重新派发')

    # 2. 补派"有 pending 文档但无触发源"的节点任务
    pending_nodes = set(Document.objects.filter(
        status='done', is_deleted=False, graph_status='pending'
    ).values_list('node_id', flat=True))
    for node_id in pending_nodes:
        if node_id in stuck_candidates or (not force and _node_active(node_id)):
            continue
        node = KnowledgeNode.objects.filter(id=node_id, is_deleted=False).first()
        if node is None or node.graph_pending:
            continue
        if _dispatch(node_id):
            dispatched_nodes += 1
            logger.info(f'[Graph Task] 自愈: 节点 {node_id} 存在待处理文档，补派任务')

    if recovered or dispatched_nodes:
        logger.info(f'[Graph Task] 自愈完成: 回退 {recovered} 个文档, 派发 {dispatched_nodes} 个节点任务')
    return {'recovered': recovered, 'dispatched_nodes': dispatched_nodes}


@shared_task(queue='parse')
def community_detection_task():
    """定时或手动触发：社区检测 + 摘要生成"""
    from apps.graph.community import run_community_detection
    from apps.llm.factory import get_llm
    llm = get_llm()
    count = run_community_detection(llm)
    logger.info(f'[Graph Task] 社区检测完成，共 {count} 个社区')
    return count
