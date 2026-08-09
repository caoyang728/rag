# RAG-Agent nginx 镜像：仅将反向代理配置打入官方 nginx
FROM nginx:alpine

# 将 RAG-Agent 站点配置作为默认站点配置
COPY nginx.conf /etc/nginx/conf.d/default.conf
