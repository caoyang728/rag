"""
retrieval views - Debug 检索接口，用于快速验证混合检索链路
"""
from loguru import logger
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView



class DebugSearchView(APIView):
    """POST /api/v1/retrieval/search/  {query, root_types?, top_k?, do_rerank?}"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        q = (request.data.get("query") or "").strip()
        if not q:
            return Response({"detail": "query 必填"}, status=400)
        root_types = request.data.get("root_types")
        if not root_types or not root_types[0]:
            from apps.knowledge.models import KnowledgeNode
            default_root = KnowledgeNode.objects.filter(
                node_type='root', is_deleted=False
            ).first()
            root_types = [default_root.root_type] if default_root else ['company_doc']
        do_rerank = bool(request.data.get("do_rerank", True))
        top_k = int(request.data.get("top_k") or 5)

        try:
            from apps.retrieval.hybrid import hybrid_search
            result = hybrid_search(q, request.user, root_types=root_types,
                                   do_rerank=do_rerank, top_k=top_k)
        except Exception as e:
            logger.exception("search debug error")
            return Response({"detail": f"检索失败: {e}"}, status=500)
        return Response(result)
