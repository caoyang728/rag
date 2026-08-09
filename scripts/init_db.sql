-- ==========================================================================
-- PostgreSQL 首次初始化脚本（由 docker-entrypoint-initdb.d 执行一次）
-- 1. 在 template1 中启用 pgvector，使之后所有新建数据库（含 pytest 测试库）自动带扩展
-- 2. 在主业务库中启用 pgvector（迁移层也有 CREATE EXTENSION 兜底，双保险）
-- ==========================================================================

-- template1 中启用，保证后续 create database 都继承
\connect template1
CREATE EXTENSION IF NOT EXISTS vector;

-- 当前主库（POSTGRES_DB）启用
CREATE EXTENSION IF NOT EXISTS vector;
