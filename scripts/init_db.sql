-- ==========================================================================
-- 企业私有化多场景智能 RAG-Agent 统一知识库平台 - 数据库初始化脚本
-- 只做扩展安装。所有表由 Django migrate 生成。
-- 由 Docker 的 docker-entrypoint-initdb.d 自动执行（仅在首次创建时）。
-- ==========================================================================

-- 扩展安装（需要 superuser 权限）
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector 向量扩展
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- 三元组模糊匹配（BM25/LIKE 加速）
CREATE EXTENSION IF NOT EXISTS btree_gin;   -- 组合 GIN 索引支持
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid()

-- 中文分词扩展 zhparser 若未安装，回退用 simple 分词器
-- 生产环境可另外装 zhparser 并 CREATE TEXT SEARCH CONFIGURATION zh
-- 本项目在 Model 层直接把 tsvector 计算放 Python 端，避免强依赖

-- 时区
SET timezone TO 'Asia/Shanghai';
