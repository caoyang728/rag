#!/bin/bash
# ==========================================================================
# 在 rag_xinference 容器内注册并启动 Embedding / Rerank 模型（幂等）
#
# 注册的模型（model_uid 与应用侧 SystemConfig/LLMModel 的 model_name 完全一致，
# 这样 Xinference 的 /v1/embeddings、/v1/rerank 才能按 model=xxx 命中模型实例）：
#   - BAAI/bge-m3                  embedding，dim=1024（与 EMBEDDING_DIM 一致）
#   - BAAI/bge-reranker-v2-m3      rerank（与 RERANK_MODEL 一致）
#
# 通过 REST API（POST /v1/models + 轮询 /v1/models 就绪）启动，而不是 xinference CLI：
# Xinference v3 的 CLI 在 model_uid 含 "/" 时查询进度接口会 404（CLI bug），
# 但模型实际已在后台启动；REST API 方式可正确拿到就绪状态，语义更清晰。
#
# 首次启动会下载模型权重（HF 镜像站 hf-mirror.com，见 compose 的 HF_ENDPOINT），
# 耗时较长；权重与 Xinference 元数据持久化在 xinference_data 卷，重启容器后保留，
# 再次执行本脚本会自动跳过已注册模型。
#
# 用法：bash scripts/launch_xinference_models.sh
# ==========================================================================
set -e

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
XIN_HTTP="http://127.0.0.1:9997/v1"
# 等待超时（秒）：首次下载 bge-m3(2.8G)+reranker(1.4G) 权重可能超过 30 分钟
READY_TIMEOUT=3600

# 等待 Xinference 服务就绪（健康检查就绪前 launch 会失败）
echo "[launch] 等待 Xinference 服务就绪..."
for i in $(seq 1 60); do
  if docker compose -f "$COMPOSE_FILE" exec -T xinference python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9997/v1/models')" >/dev/null 2>&1; then
    echo "[launch] Xinference 已就绪"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "[launch] 错误：Xinference 服务 5 分钟内未就绪" >&2
    exit 1
  fi
  echo "  ...等待中 ($i/60)"
  sleep 5
done

# 启动指定模型并轮询就绪；已注册（幂等）时直接返回
# 参数：model_uid model_name model_type
launch_model() {
  local uid="$1" name="$2" mtype="$3"
  echo "[launch] 检查模型 ${uid} ..."
  if docker compose -f "$COMPOSE_FILE" exec -T xinference python -c "
import urllib.request, json, sys
models = json.load(urllib.request.urlopen('$XIN_HTTP/models'))['data']
sys.exit(0 if any(m['model_name'] == '$name' for m in models) else 1)
"; then
    echo "[launch] ${uid} 已注册，跳过"
    return 0
  fi

  echo "[launch] 注册并启动 ${uid}（${mtype}）..."
  docker compose -f "$COMPOSE_FILE" exec -T xinference python -c "
import urllib.request, json
payload = json.dumps({
    'model_uid': '$uid',
    'model_name': '$name',
    'model_type': '$mtype',
    'model_format': 'pytorch',
    'device': 'cpu',
}).encode()
req = urllib.request.Request('$XIN_HTTP/models', data=payload, headers={'Content-Type': 'application/json'})
urllib.request.urlopen(req, timeout=30)
print('launch 请求已提交')
"

  # 轮询直到模型就绪（下载权重 + 加载模型均在此阶段完成）
  echo "[launch] 等待 ${uid} 就绪（首次需下载权重，可能耗时较长）..."
  for i in $(seq 1 $((READY_TIMEOUT / 10))); do
    if docker compose -f "$COMPOSE_FILE" exec -T xinference python -c "
import urllib.request, json, sys
models = json.load(urllib.request.urlopen('$XIN_HTTP/models'))['data']
sys.exit(0 if any(m['model_name'] == '$name' for m in models) else 1)
" 2>/dev/null; then
      echo "[launch] ${uid} 就绪"
      return 0
    fi
    if [ $((i % 30)) -eq 0 ]; then
      echo "  ...等待中 ($((i * 10))s)"
    fi
    sleep 10
  done
  echo "[launch] 错误：${uid} 在 ${READY_TIMEOUT}s 内未就绪" >&2
  return 1
}

launch_model "BAAI/bge-m3" "bge-m3" "embedding"
launch_model "BAAI/bge-reranker-v2-m3" "bge-reranker-v2-m3" "rerank"

echo "[launch] 当前已注册模型："
docker compose -f "$COMPOSE_FILE" exec -T xinference python -c "
import urllib.request, json
for m in json.load(urllib.request.urlopen('$XIN_HTTP/models'))['data']:
    print('  -', m['model_name'], '| type:', m['model_type'], '| dim:', m.get('dimensions', '-'))
"
