"""
Ragas 全自动评估流水线 —— 部署前测试用

设计目标:
1. 零标注:直接复用知识库现有 DocumentChunk 作为语料,Ragas 自动合成测试集
2. 全自动:文档 → 测试集 → 检索+回答 → Ragas 标准指标评估 → 报告,一条命令跑通
3. 离线场景:部署前对黄金测试集做完整 RAG 评估,需要 reference 才能算的指标
   (context_recall/context_precision/answer_correctness)在此场景可用

与生产评估的关系:Ragas 用于部署前(有 reference,指标更全),DeepEval 用于生产中
(无 reference,采样+限速,见 production_eval.py);两者互补,Ragas 不在生产链路中调用。

选型说明:Ragas 原生支持 reference 指标且 TestsetGenerator 可自动合成测试集,
自托管数据不出域,符合企业内网审计要求。

模型接入:LLM 复用项目 DeepSeek(OpenAI 兼容协议),Embedding 复用项目
get_embedding_client()(BAAI/bge-m3);评估所用模型与生产一致,结果更具代表性。

版本兼容:适配 ragas 0.2.x / 0.3.x(evaluate + EvaluationDataset + SingleTurnSample);
ragas 0.4+ 有 breaking change,需按官方迁移指南调整。
"""
import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# Django ORM / 配置延迟引入,避免模块加载期触发 DB 连接


# ============================================================================
# 0. 兼容性补丁:让 ragas 兼容新版 langchain-community 0.4+
# ============================================================================
# ragas(0.3/0.4)在 ragas/llms/base.py 无条件 import
# langchain_community.chat_models.vertexai.ChatVertexAI,但 langchain-community 0.4+
# 已 sunset 并移除该子模块路径,导致 ragas 无法 import。
# 本项目只用 ChatOpenAI(DeepSeek 兼容),不使用 VertexAI 系列;此处注入占位类
# 让 ragas 完成导入,占位类仅出现在 ragas 的 MULTIPLE_COMPLETION_SUPPORTED 列表中,
# 不会影响 ChatOpenAI 路径的任何行为。幂等:已存在真实模块时不覆盖。
def _patch_ragas_langchain_compat():
    import sys
    import types
    try:
        import langchain_community.chat_models.vertexai  # noqa: F401
    except ImportError:
        import langchain_community.chat_models as _cm
        _shim = types.ModuleType('langchain_community.chat_models.vertexai')

        class ChatVertexAI:
            """占位类,仅满足 ragas 内部引用,不会被实际实例化"""
            pass

        _shim.ChatVertexAI = ChatVertexAI
        sys.modules['langchain_community.chat_models.vertexai'] = _shim
        setattr(_cm, 'vertexai', _shim)
    try:
        from langchain_community.llms import VertexAI  # noqa: F401
    except ImportError:
        import langchain_community.llms as _llms

        class VertexAI:
            """占位类,同上"""
            pass

        _llms.VertexAI = VertexAI


_patch_ragas_langchain_compat()


# ============================================================================
# 1. 适配层:把项目的 DeepSeek LLM 和 Embedding 适配为 Ragas 可用的接口
# ============================================================================

def _get_evaluator_llm(model: Optional[str] = None):
    """构建 Ragas 评估用 LLM

    复用项目 DeepSeek 配置，用 langchain_openai.ChatOpenAI 接入
    (DeepSeek 兼容 OpenAI 协议)，再用 LangchainLLMWrapper 包装为 Ragas 的 BaseRagasLLM。

    配置读取：
    - model_name：SystemConfig.LLM_BASE_MODEL
    - base_url：LLMModel 表按 model_name 反查
    - api_key：从 env 读取（敏感凭证不入库）

    Args:
        model: 指定模型名;None 则用 SystemConfig.LLM_BASE_MODEL

    Returns:
        LangchainLLMWrapper 实例
    """
    from django.conf import settings
    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper
    from apps.system.config_loader import get_config_value, get_llm_model_config

    model_name = model or get_config_value('LLM_BASE_MODEL', default='', value_type='string')
    if not model_name:
        raise ValueError('SystemConfig.LLM_BASE_MODEL 未配置，无法启动 Ragas 评估')

    llm_row = get_llm_model_config(model_name, model_type='llm')
    if llm_row and llm_row.get('base_url'):
        base_url = llm_row['base_url']
    else:
        logger.warning(f'[Ragas] LLMModel 表未配置 model={model_name}，base_url 为空可能导致评估失败')
        base_url = ''

    api_key = getattr(settings, 'LLM_API_KEY', '')

    # temperature=0 降低评估随机性,保证同一输入打分可复现
    chat = ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        temperature=0,
        timeout=120,
    )
    return LangchainLLMWrapper(chat)


class _ProjectEmbeddings:
    """将项目 EmbeddingClient 适配为 LangChain Embeddings 接口

    Ragas 的部分指标(AnswerRelevancy / AnswerCorrectness)需要 embeddings 计算语义相似度。
    这里直接复用项目 get_embedding_client()(docker 优先 / api 兜底),保证评估所用
    embedding 与生产检索一致,避免因 embedding 模型差异导致评估失真。

    LangChain Embeddings 接口要求实现 embed_documents / embed_query 两个方法。
    """

    def __init__(self):
        from apps.llm.embedding import get_embedding_client
        # 惰性持有客户端,首次调用时才初始化
        self._client = get_embedding_client()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """批量向量化(Ragas 评估多文本时调用)"""
        return self._client.embed(texts)

    def embed_query(self, text: str) -> List[float]:
        """单条向量化(Ragas 计算单条相似度时调用)"""
        return self._client.embed_one(text)


def _get_evaluator_embeddings():
    """构建 Ragas 评估用 Embedding(包装项目 embedding 客户端)"""
    from ragas.embeddings import LangchainEmbeddingsWrapper
    return LangchainEmbeddingsWrapper(_ProjectEmbeddings())


# ============================================================================
# 2. 文档加载:从知识库现有切片构建 Ragas 测试集生成输入
# ============================================================================

def load_corpus_chunks(
    root_type: Optional[str] = None,
    limit_docs: int = 50,
    min_chunk_chars: int = 80,
) -> List[Any]:
    """从知识库加载已解析完成的文档切片,转为 LangChain Document 列表

    用途:作为 Ragas TestsetGenerator 的输入语料。直接复用现有 DocumentChunk,
    而非让 Ragas 重新切分原文——这样生成的测试集与生产 RAG 检索的切片粒度一致,
    评估结果更具代表性。

    筛选规则:
    - Document.status='done' 且未逻辑删除,确保只对成功入库的文档生成测试
    - 仅取 text 类型切片(跳过 image/code),保证语料可读性
    - 跳过过短切片(< min_chunk_chars),避免生成低质量问题

    Args:
        root_type: 限定领域;None 表示全部
        limit_docs: 最多取多少篇文档(控制生成成本)
        min_chunk_chars: 切片最小字符数,过滤碎片

    Returns:
        LangChain Document 列表,metadata 含 doc_id/source 便于溯源
    """
    from langchain_core.documents import Document
    from apps.knowledge.models import Document, DocumentChunk

    doc_qs = Document.objects.filter(
        status='done',
        is_deleted=False,
    ).order_by('-updated_at')
    if root_type:
        doc_qs = doc_qs.filter(root_type=root_type)
    doc_qs = doc_qs[:limit_docs]

    doc_ids = list(doc_qs.values_list('id', flat=True))
    if not doc_ids:
        raise ValueError(
            '未找到 status=done 的文档,请先完成文档解析入库后再运行评估'
        )

    # 一次性取所有切片,按文档分组;只取 text 类型,避免图片/代码切片干扰问题生成
    chunks = DocumentChunk.objects.filter(
        document_id__in=doc_ids,
        chunk_type='text',
        content_length__gte=min_chunk_chars,
    ).select_related('document').order_by('document_id', 'chunk_index')

    docs: List[Document] = []
    for c in chunks:
        # metadata.filename 是 Ragas TestsetGenerator 识别同一文档切片的依据,
        # 用 doc_id+title 标识,确保跨切片归属正确
        docs.append(Document(
            page_content=c.content,
            metadata={
                'filename': f'doc_{c.document_id}_{c.document.title}',
                'doc_id': c.document_id,
                'title': c.document.title,
                'section_path': c.section_path or '',
                'chunk_id': c.id,
            },
        ))

    logger.info(
        f'[RagasPipeline] 加载语料: docs={len(doc_ids)}, chunks={len(docs)}, root_type={root_type or "ALL"}',
    )
    return docs


# ============================================================================
# 3. 测试集自动生成
# ============================================================================

def generate_testset(
    corpus_docs: List[Any],
    testset_size: int = 20,
    model: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """用 Ragas TestsetGenerator 从语料自动合成测试集

    采用 generate_with_chunks:跳过 Ragas 内部切分,直接用我们传入的切片,
    保留切片内容和元数据完整性(与生产 RAG 切片一致)。

    生成的测试集每条包含:
    - user_input: 合成问题
    - reference: 参考答案(ground_truth,用于 context_recall/answer_correctness)
    - retrieved_contexts: 生成该问题所依据的原文片段

    Args:
        corpus_docs: LangChain Document 列表(load_corpus_chunks 产出)
        testset_size: 生成测试样本数
        model: 生成用 LLM 模型;None 用 settings.LLM_BASE_MODEL

    Returns:
        (samples, testset_id) samples 为统一格式的字典列表
    """
    from ragas.testset import TestsetGenerator

    generator_llm = _get_evaluator_llm(model)
    generator_embeddings = _get_evaluator_embeddings()

    generator = TestsetGenerator(
        llm=generator_llm,
        embedding_model=generator_embeddings,
    )

    logger.info(f'[RagasPipeline] 开始生成测试集, target_size={testset_size}')
    t0 = time.time()

    # generate_with_langchain_docs 接受 LangChain Document 列表,
    # 内部构建 KnowledgeGraph 后合成多类型问题(simple/reasoning/multi_context)
    testset = generator.generate_with_langchain_docs(
        corpus_docs,
        testset_size=testset_size,
    )

    elapsed = int(time.time() - t0)
    logger.info(f'[RagasPipeline] 测试集生成完成, 耗时={elapsed}s')

    # 转为统一字典格式,屏蔽 Ragas 不同版本的 schema 差异
    df = testset.to_pandas()
    samples: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        samples.append({
            'user_input': str(row.get('user_input') or row.get('question') or ''),
            'reference': str(row.get('reference') or row.get('ground_truth') or ''),
            'retrieved_contexts': list(row.get('retrieved_contexts')
                                       or row.get('contexts') or []),
        })

    # 过滤掉问题或参考答案为空的无效样本(生成模型偶尔会产出空内容)
    samples = [s for s in samples if s['user_input'] and s['reference']]

    testset_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    logger.info('[RagasPipeline] 有效测试样本: {}, testset_id={}', len(samples), testset_id)
    return samples, testset_id


def save_testset(samples: List[Dict[str, Any]], testset_id: str, output_dir: str) -> str:
    """将测试集持久化为 JSON,便于复用(--skip-generate 模式)与人工抽检"""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f'testset_{testset_id}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(samples, f, ensure_ascii=False, indent=2)
    logger.info(f'[RagasPipeline] 测试集已保存: {path}')
    return path


def load_testset(path: str) -> List[Dict[str, Any]]:
    """从 JSON 加载已有测试集(复用模式)"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ============================================================================
# 4. RAG 推理:对测试问题跑生产检索 + 回答生成
# ============================================================================

def _get_eval_user():
    """获取评估用的系统用户

    hybrid_search 需要一个 user 做权限过滤。复用 periodic_retrieval_evaluation 的策略:
    优先 username='system',其次超管。这样评估的是"系统视角"的检索质量,
    若需评估特定用户视角的权限隔离效果,可扩展为传入指定 user_id。
    """
    from apps.users.models import User
    user = User.objects.filter(username='system').first()
    if user:
        return user
    user = User.objects.filter(is_superuser=True).first()
    if not user:
        raise ValueError('未找到 system 用户或超级管理员,无法执行评估')
    return user


def run_rag_for_question(question: str, user, model: Optional[str] = None) -> Dict[str, Any]:
    """对单个问题执行完整 RAG:混合检索 → 构建上下文 → LLM 生成回答

    复用生产 hybrid_search(含权限过滤 + 向量/BM25/RRF/Rerank 全链路),
    保证评估对象与生产管线一致,而非一个简化版 mock。

    Args:
        question: 测试问题
        user: 执行检索的用户(权限过滤)
        model: 回答生成用模型;None 用 settings.LLM_BASE_MODEL

    Returns:
        {'answer': str, 'contexts': List[str], 'retrieval_stats': dict, 'error': str|None}
    """
    from apps.retrieval.hybrid import hybrid_search
    from apps.llm.factory import get_llm

    result: Dict[str, Any] = {
        'answer': '', 'contexts': [], 'retrieval_stats': {}, 'error': None,
    }

    try:
        search_result = hybrid_search(query=question, user=user)
    except Exception as e:
        logger.warning(f'[RagasPipeline] 检索失败: {e}, question={question[:60]}')
        result['error'] = f'retrieval_failed: {e}'
        return result

    chunks = search_result.get('chunks', [])
    result['retrieval_stats'] = search_result.get('stats', {})

    # contexts 取检索到的切片原文,Ragas context_precision/recall 据此评估检索质量
    contexts = []
    for c in chunks[:5]:
        content = c.get('content') or ''
        if content:
            contexts.append(content[:500])  # 截断控制 token 成本
    result['contexts'] = contexts

    if not contexts:
        result['answer'] = '（检索未返回相关内容，无法回答）'
        return result

    context_text = '\n\n'.join(f'[片段{i+1}] {ctx}' for i, ctx in enumerate(contexts))
    messages = [
        {'role': 'system', 'content': (
            '你是一名知识库问答助手。请严格基于提供的上下文回答用户问题。'
            '若上下文中没有相关信息，请明确说明"根据现有资料无法回答该问题"。'
        )},
        {'role': 'user', 'content': f'问题：{question}\n\n参考资料：\n{context_text}\n\n请给出回答：'},
    ]

    try:
        llm = get_llm(model)
        resp = llm.chat(messages, temperature=0.3, max_tokens=1000)
        result['answer'] = resp.get('content', '')
    except Exception as e:
        logger.warning(f'[RagasPipeline] 回答生成失败: {e}')
        result['error'] = f'generation_failed: {e}'
        result['answer'] = f'[回答生成失败] {e}'

    return result


# ============================================================================
# 5. 评估:对每条样本跑 Ragas 标准指标
# ============================================================================

def _build_metrics(evaluator_llm, evaluator_embeddings) -> List[Any]:
    """构建评估指标集合

    选用 RAG 五大核心指标,覆盖检索 + 生成全链路:
    - Faithfulness: 回答是否忠于 context(无幻觉),无需 reference
    - AnswerRelevancy: 回答是否切中问题(需 embeddings)
    - LLMContextPrecisionWithReference: 检索结果中相关片段的排序质量(需 reference)
    - LLMContextRecall: context 是否覆盖 reference 所需信息(需 reference)
    - AnswerCorrectness: 回答与参考答案的事实一致性(需 reference + embeddings)

    metric 类名在 ragas 0.2/0.3 间有变化(LLMContextPrecisionWithReference 等),
    用 try/except 兼容新旧命名,避免版本绑定。
    """
    from ragas.metrics import Faithfulness, AnswerRelevancy, AnswerCorrectness

    metrics = [
        Faithfulness(llm=evaluator_llm),
        AnswerRelevancy(llm=evaluator_llm, embeddings=evaluator_embeddings),
        AnswerCorrectness(llm=evaluator_llm, embeddings=evaluator_embeddings),
    ]

    # context_precision / context_recall 类名兼容
    try:
        from ragas.metrics import LLMContextPrecisionWithReference as _CP
        from ragas.metrics import LLMContextRecall as _CR
    except ImportError:
        from ragas.metrics import ContextPrecision as _CP  # type: ignore
        from ragas.metrics import ContextRecall as _CR  # type: ignore
    metrics.append(_CP(llm=evaluator_llm))
    metrics.append(_CR(llm=evaluator_llm))

    return metrics


def _evaluate_sync(samples: List[Dict[str, Any]], evaluator_llm, evaluator_embeddings) -> List[Dict[str, Any]]:
    """同步执行 Ragas evaluate,逐条返回各指标得分

    用 ragas.evaluate + EvaluationDataset 一次性评估全部样本,
    ragas 内部并发执行(受 RunConfig.max_workers 控制)。

    每条样本的字段映射到 SingleTurnSample:
    - user_input ← 测试问题
    - response ← RAG 生成的回答
    - retrieved_contexts ← 检索到的切片
    - reference ← 测试集的参考答案

    Returns:
        每条样本的 {user_input, answer, contexts, reference, 各指标分数}
    """
    from ragas import evaluate
    from ragas.dataset_schema import EvaluationDataset, SingleTurnSample

    # 先对每条样本跑 RAG,拿到 answer/contexts
    enriched: List[Dict[str, Any]] = []
    for s in samples:
        rag = run_rag_for_question(s['user_input'], _get_eval_user())
        enriched.append({
            'user_input': s['user_input'],
            'reference': s['reference'],
            'response': rag['answer'],
            'retrieved_contexts': rag['contexts'],
            '_rag_error': rag['error'],
            '_retrieval_stats': rag['retrieval_stats'],
        })

    # 构建 EvaluationDataset(过滤掉检索彻底失败的样本,避免拖累整体评估)
    eval_samples = []
    for e in enriched:
        if not e['retrieved_contexts']:
            # 检索无结果:指标无法计算,保留记录但跳过 Ragas 评估
            continue
        eval_samples.append(SingleTurnSample(
            user_input=e['user_input'],
            response=e['response'],
            retrieved_contexts=e['retrieved_contexts'],
            reference=e['reference'],
        ))

    if not eval_samples:
        logger.warning('[RagasPipeline] 无可评估样本(检索全部失败)')
        return enriched

    metrics = _build_metrics(evaluator_llm, evaluator_embeddings)
    dataset = EvaluationDataset(eval_samples)

    logger.info('[RagasPipeline] 开始 Ragas 指标评估, 样本数={}', len(eval_samples))
    t0 = time.time()

    # evaluate 默认 allow_nest_asyncio=True,在 Django 同步命令环境可正常执行
    result = evaluate(
        dataset,
        metrics=metrics,
        show_progress=True,
        raise_exceptions=False,  # 单条失败返回 NaN,不中断整体
    )
    logger.info('[RagasPipeline] 评估完成, 耗时={}s', int(time.time() - t0))

    # 把评估结果合并回 enriched(result 是按行索引的)
    # result.to_pandas() 返回每行各指标分数
    try:
        scores_df = result.to_pandas()
    except Exception:
        scores_df = None

    for i, e in enumerate([x for x in enriched if x['retrieved_contexts']]):
        if scores_df is not None and i < len(scores_df):
            row = scores_df.iloc[i]
            for col in scores_df.columns:
                e[col] = _safe_score(row[col])
    return enriched


def _safe_score(val) -> Any:
    """安全提取分数,处理 NaN/None/对象类型"""
    try:
        import math
        if val is None:
            return None
        f = float(val)
        if math.isnan(f):
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None


# ============================================================================
# 6. 报告生成
# ============================================================================

def generate_report(
    enriched: List[Dict[str, Any]],
    testset_id: str,
    output_dir: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """生成评估报告:JSON(全量明细) + Markdown(可读摘要)

    报告内容:
    - 整体指标均值(faithfulness/relevancy/context_precision/context_recall/correctness)
    - 每条样本的明细(问题/回答/参考答案/各指标分/检索耗时)
    - 低分样本定位(便于人工排查 Prompt/检索问题)

    Args:
        enriched: 评估后的样本列表
        testset_id: 测试集 ID
        output_dir: 输出目录
        meta: 元信息(模型名、参数等)

    Returns:
        {'json': path, 'markdown': path}
    """
    os.makedirs(output_dir, exist_ok=True)

    metric_keys = ['faithfulness', 'answer_relevancy',
                   'context_precision', 'context_recall', 'answer_correctness']

    # --- 整体均值 ---
    summary = {}
    for k in metric_keys:
        scores = [e.get(k) for e in enriched if e.get(k) is not None]
        summary[k] = round(sum(scores) / len(scores), 4) if scores else None

    total = len(enriched)
    rag_errors = sum(1 for e in enriched if e.get('_rag_error'))

    # --- JSON 明细 ---
    report_data = {
        'testset_id': testset_id,
        'generated_at': datetime.now().isoformat(),
        'meta': meta or {},
        'summary': {
            'total_samples': total,
            'rag_errors': rag_errors,
            'metrics': summary,
        },
        'samples': enriched,
    }
    json_path = os.path.join(output_dir, f'report_{testset_id}.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2, default=str)

    # --- Markdown 摘要(便于直接阅读和分享) ---
    md_path = os.path.join(output_dir, f'report_{testset_id}.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(f'# Ragas 评估报告 {testset_id}\n\n')
        f.write(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n\n')
        if meta:
            f.write('## 元信息\n\n')
            for k, v in meta.items():
                f.write(f'- **{k}**: {v}\n')
            f.write('\n')
        f.write('## 整体指标\n\n')
        f.write(f'- 总样本数: {total}\n')
        f.write(f'- RAG 异常数: {rag_errors}\n\n')
        f.write('| 指标 | 均分 |\n|---|---|\n')
        for k in metric_keys:
            v = summary.get(k)
            f.write(f'| {k} | {v if v is not None else "N/A"} |\n')

        f.write('\n## 低分样本(均分 < 0.6,便于人工排查)\n\n')
        low_score = []
        for e in enriched:
            scores = [e.get(k) for k in metric_keys if e.get(k) is not None]
            avg = sum(scores) / len(scores) if scores else 0
            if avg < 0.6:
                low_score.append((avg, e))
        low_score.sort(key=lambda x: x[0])
        for avg, e in low_score[:20]:
            f.write(f'### 均分 {avg:.3f}\n')
            f.write(f'- **问题**: {e["user_input"][:200]}\n')
            f.write(f'- **回答**: {str(e.get("response", ""))[:200]}\n')
            f.write(f'- **参考答案**: {str(e.get("reference", ""))[:200]}\n')
            for k in metric_keys:
                f.write(f'  - {k}: {e.get(k)}\n')
            if e.get('_rag_error'):
                f.write(f'  - RAG错误: {e["_rag_error"]}\n')
            f.write('\n')

    logger.info(f'[RagasPipeline] 报告已生成: {json_path}, {md_path}')
    return {'json': json_path, 'markdown': md_path}


# ============================================================================
# 7. 全流程入口
# ============================================================================

def run_full_pipeline(
    testset_size: int = 20,
    limit_docs: int = 50,
    root_type: Optional[str] = None,
    model: Optional[str] = None,
    output_dir: str = 'eval_reports',
    corpus_docs: Optional[List[Any]] = None,
    samples: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """全自动评估流水线入口

    流程:加载语料 → 生成测试集 → 跑 RAG → Ragas 指标评估 → 生成报告

    支持两种调用模式:
    - 全自动:不传 corpus_docs/samples,内部自动加载语料并生成测试集
    - 复用测试集:传入 samples(--skip-generate 场景),跳过生成直接评估

    Args:
        testset_size: 测试集大小
        limit_docs: 取多少篇文档作为语料
        root_type: 限定领域
        model: 评估/生成用模型;None 用项目默认模型
        output_dir: 报告输出目录
        corpus_docs: 预加载的语料(可选)
        samples: 预生成的测试集(可选,复用模式)

    Returns:
        {'testset_id', 'samples', 'report_paths', 'summary'}
    """
    logger.info(
        f'[RagasPipeline] 启动全自动评估: testset_size={testset_size}, limit_docs={limit_docs}, root_type={root_type or "ALL"}',
    )

    if samples is None:
        if corpus_docs is None:
            corpus_docs = load_corpus_chunks(root_type=root_type, limit_docs=limit_docs)
        if not corpus_docs:
            raise ValueError('语料为空,无法生成测试集')
        samples, testset_id = generate_testset(corpus_docs, testset_size=testset_size, model=model)
        save_testset(samples, testset_id, output_dir)
    else:
        testset_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_reuse_' + uuid.uuid4().hex[:6]
        logger.info(f'[RagasPipeline] 复用已有测试集, 样本数={len(samples)}')

    evaluator_llm = _get_evaluator_llm(model)
    evaluator_embeddings = _get_evaluator_embeddings()
    enriched = _evaluate_sync(samples, evaluator_llm, evaluator_embeddings)

    meta = {
        'model': model or '(project default)',
        'testset_size': len(samples),
        'limit_docs': limit_docs,
        'root_type': root_type or 'ALL',
    }
    report_paths = generate_report(enriched, testset_id, output_dir, meta=meta)

    # 汇总日志
    metric_keys = ['faithfulness', 'answer_relevancy',
                   'context_precision', 'context_recall', 'answer_correctness']
    summary = {}
    for k in metric_keys:
        scores = [e.get(k) for e in enriched if e.get(k) is not None]
        summary[k] = round(sum(scores) / len(scores), 4) if scores else None
    logger.info(f'[RagasPipeline] 评估汇总: {summary}')

    return {
        'testset_id': testset_id,
        'samples': enriched,
        'report_paths': report_paths,
        'summary': summary,
    }
