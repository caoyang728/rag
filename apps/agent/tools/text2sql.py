"""
text2sql 工具 - 结构化数据查询
通过 LLM 将自然语言问题翻译为 SQL，在业务数据库执行 SELECT 查询。

安全设计：
1. 表结构通过代码读取 information_schema，传入 LLM 约束生成范围
2. 只允许 SELECT，SQL 执行前做关键词白名单校验
3. statement_timeout 限制执行时间，LIMIT 强制限制返回行数
4. 通过 BUSINESS_DB_TABLES 系统配置白名单控制可查询的表

配置读取：
- 表白名单从 SystemConfig 表读取（key=BUSINESS_DB_TABLES）
- 业务数据库连接串仍从 env 读取（BUSINESS_DB_DSN），属于基础设施配置
"""
import json
import re
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import BaseTool, ToolContext


class Text2SqlTool(BaseTool):
    """Text2SQL 工具

    当用户问题涉及结构化数据查询（如统计、汇总、筛选业务表数据）时调用。
    工具先读取业务数据库表结构，再让 LLM 根据表结构生成 SQL，
    最后安全执行 SELECT 并返回结果。

    安全要点：
    - 仅执行 SELECT 语句，写操作关键词一律拒绝
    - 强制 LIMIT 防止返回过多数据
    - statement_timeout 防止慢查询
    """

    name = 'text2sql'
    description = (
        '查询业务数据库中的结构化数据。当用户问题涉及统计、汇总、'
        '筛选业务记录（如用户数、订单量、文档统计等）时调用。'
        '工具会根据数据库表结构生成 SQL 并执行查询。'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'question': {
                'type': 'string',
                'description': '自然语言数据查询需求，如"统计每个部门的用户数量"',
            },
            'tables': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '限定查询涉及的表名列表（可选）；不传则由工具自动选择',
            },
        },
        'required': ['question'],
    }

    # 禁止的 SQL 关键词（写操作 / DDL / 危险操作）
    _FORBIDDEN_KEYWORDS = re.compile(
        r'\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|'
        r'execute|pg_sleep|copy|lo_export|lo_import|pg_read_file|pg_ls_dir)\b',
        re.IGNORECASE,
    )

    def execute(self, ctx: ToolContext, question: str, tables: List[str] = None,
                **kwargs) -> Dict[str, Any]:
        """执行 Text2SQL 查询

        流程：
        1. 读取业务数据库表结构（按 tables 参数过滤，或全表）
        2. LLM 根据表结构 + 用户问题生成 SQL
        3. 安全校验（仅 SELECT、无禁止关键词、强制 LIMIT）
        4. 执行 SQL，设置 statement_timeout
        5. 格式化结果返回

        Args:
            ctx: 执行上下文（需要 ctx.llm 生成 SQL）
            question: 自然语言查询需求
            tables: 限定查询的表名列表

        Returns:
            {'result': str, 'ok': bool, 'meta': {'sql': str, 'rows': int, 'columns': [...]}}
        """
        if not question or not isinstance(question, str):
            return {'result': '查询问题不能为空', 'ok': False, 'meta': {}}

        # 1. 读取表结构
        try:
            schema = self._load_schema(tables)
        except Exception as e:
            logger.exception('[Text2SqlTool] load schema error')
            return {'result': f'读取数据库表结构失败: {e}', 'ok': False,
                    'meta': {'error': str(e)}}

        if not schema:
            return {'result': '未找到可查询的表，请检查 BUSINESS_DB_TABLES 配置。',
                    'ok': False, 'meta': {}}

        # 2. LLM 生成 SQL
        try:
            sql = self._generate_sql(ctx, question, schema)
        except Exception as e:
            logger.exception('[Text2SqlTool] generate sql error')
            return {'result': f'SQL 生成失败: {e}', 'ok': False,
                    'meta': {'error': str(e)}}

        if not sql:
            return {'result': 'LLM 未能生成有效 SQL', 'ok': False,
                    'meta': {'schema': schema}}

        # 3. 安全校验
        check_err = self._validate_sql(sql)
        if check_err:
            return {'result': f'SQL 安全校验失败: {check_err}', 'ok': False,
                    'meta': {'sql': sql, 'error': check_err}}

        # 4. 执行 SQL
        try:
            rows, columns = self._execute_sql(sql)
        except Exception as e:
            logger.warning(f'[Text2SqlTool] sql execute error: {e} | sql: {sql[:200]}')
            return {'result': f'SQL 执行失败: {e.__class__.__name__}: {str(e)[:300]}',
                    'ok': False, 'meta': {'sql': sql, 'error': str(e)}}

        # 5. 格式化结果
        result_text = self._format_result(sql, rows, columns)
        return {
            'result': result_text,
            'ok': True,
            'meta': {
                'sql': sql,
                'rows': len(rows),
                'columns': columns,
            },
        }

    # ------------------------------------------------------------------
    # 表结构读取
    # ------------------------------------------------------------------

    def _load_schema(self, tables: List[str] = None) -> Dict[str, List[Dict]]:
        """读取业务数据库表结构

        从 information_schema 读取 public schema 下的表和列信息。
        可通过 BUSINESS_DB_TABLES 配置白名单，或通过 tables 参数临时限定。

        新语义：
        - BUSINESS_DB_TABLES 为空（未勾选任何表）→ 不允许查询任何表，Text2SQL 不生效
        - BUSINESS_DB_TABLES 有值 → 仅白名单内的表可查询
        - 显式传 tables 参数 → 临时覆盖配置白名单

        Returns:
            {table_name: [{'name': str, 'type': str, 'nullable': bool, 'comment': str}]}
            空字典表示无可查询的表
        """
        from apps.system.config_loader import get_config_value

        # 从 SystemConfig 表读取表白名单（已不在 env 中存储，统一从 DB 读取）
        # default 为空字符串时表示未配置白名单，此时 Text2SQL 不生效
        raw_tables = get_config_value('BUSINESS_DB_TABLES', default='', value_type='string') or ''
        env_table_list = [t.strip() for t in raw_tables.split(',') if t.strip()]

        # 优先级：参数 tables > DB 中的配置白名单
        # 空配置=未勾选任何表，Text2SQL 不生效
        whitelist = tables or env_table_list

        # 白名单为空时直接返回空 schema，阻止 Text2SQL 查询
        if not whitelist:
            return {}

        sql = """
            SELECT
                t.table_name,
                c.column_name,
                c.data_type,
                c.is_nullable,
                pgd.description
            FROM information_schema.tables t
            JOIN information_schema.columns c
              ON t.table_name = c.table_name AND c.table_schema = 'public'
            LEFT JOIN pg_catalog.pg_statio_all_tables st
              ON st.relname = t.table_name AND st.schemaname = 'public'
            LEFT JOIN pg_catalog.pg_description pgd
              ON pgd.objoid = st.relid AND pgd.objsubid = c.ordinal_position
            WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
        """
        params: List[str] = []
        if whitelist:
            placeholders = ','.join(['%s'] * len(whitelist))
            sql += f' AND t.table_name IN ({placeholders})'
            params = list(whitelist)
        sql += ' ORDER BY t.table_name, c.ordinal_position'

        rows = self._fetchall(sql, params)
        schema: Dict[str, List[Dict]] = {}
        for table_name, col_name, data_type, is_nullable, comment in rows:
            schema.setdefault(table_name, []).append({
                'name': col_name,
                'type': data_type,
                'nullable': is_nullable == 'YES',
                'comment': comment or '',
            })
        return schema

    # ------------------------------------------------------------------
    # SQL 生成
    # ------------------------------------------------------------------

    def _generate_sql(self, ctx: ToolContext, question: str,
                      schema: Dict[str, List[Dict]]) -> str:
        """调用 LLM 根据表结构生成 SQL

        将表结构序列化为 DDL 风格的文本，配合用户问题让 LLM 生成 SELECT。
        使用 temperature=0 保证生成稳定性。
        """
        from apps.llm.factory import get_llm

        llm = ctx.llm or get_llm()

        # 序列化表结构为 DDL 风格文本（紧凑，节省 token）
        schema_lines = []
        for table_name, columns in schema.items():
            col_defs = []
            for c in columns:
                nullable = '' if c['nullable'] else ' NOT NULL'
                comment = f' -- {c["comment"]}' if c['comment'] else ''
                col_defs.append(f'  {c["name"]} {c["type"]}{nullable}{comment}')
            schema_lines.append(f'CREATE TABLE {table_name} (\n' +
                                ',\n'.join(col_defs) + '\n);')
        schema_text = '\n\n'.join(schema_lines)

        system = (
            '你是 PostgreSQL SQL 生成专家。根据用户问题和提供的表结构，'
            '生成一条 SELECT 查询语句。\n\n'
            '规则：\n'
            '1. 只能生成 SELECT 语句，禁止任何写操作（INSERT/UPDATE/DELETE/DROP 等）\n'
            '2. 必须添加 LIMIT，最多返回 100 行\n'
            '3. 仅使用提供的表结构，不要假设不存在的表或列\n'
            '4. 仅输出 SQL 语句本身，不要输出任何解释、markdown 标记或代码块\n'
            '5. 使用标准 PostgreSQL 语法\n'
            f'可查询的表结构：\n{schema_text}'
        )
        messages = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': f'问题：{question}\n\n请生成 SQL：'},
        ]
        resp = llm.chat(messages, temperature=0.0, max_tokens=800)
        sql = (resp.get('content') or '').strip()
        # 容错：去除可能存在的 markdown 代码块包裹
        if sql.startswith('```'):
            sql = sql.strip('`')
            if sql.lower().startswith('sql'):
                sql = sql[3:]
            sql = sql.strip()
        # 去除末尾分号（cursor 执行时不需分号）
        sql = sql.rstrip(';').strip()
        return sql

    # ------------------------------------------------------------------
    # 安全校验
    # ------------------------------------------------------------------

    def _validate_sql(self, sql: str) -> Optional[str]:
        """校验 SQL 安全性

        检查：
        1. 必须以 SELECT 开头
        2. 不能包含禁止的关键词（写操作/DDL/危险函数）
        3. 必须包含 LIMIT（或自动补充）

        Returns:
            校验失败返回错误信息，通过返回 None
        """
        sql_upper = sql.upper().strip()
        if not sql_upper.startswith('SELECT'):
            return 'SQL 必须以 SELECT 开头'
        match = self._FORBIDDEN_KEYWORDS.search(sql)
        if match:
            return f'SQL 包含禁止的关键词: {match.group(0)}'
        # 如果没有 LIMIT，自动补充（防返回过多数据）
        if 'LIMIT' not in sql_upper:
            sql = sql.rstrip(';') + ' LIMIT 100'
        return None

    # ------------------------------------------------------------------
    # SQL 执行
    # ------------------------------------------------------------------

    def _execute_sql(self, sql: str):
        """执行 SELECT SQL，返回行数据与列名

        设置 statement_timeout=10s 防止慢查询，
        结果最多取 100 行（双重保险）。
        """
        # 确保 SQL 带 LIMIT（_validate_sql 可能已补充，但生成时也可能已带）
        if 'LIMIT' not in sql.upper():
            sql = sql.rstrip(';') + ' LIMIT 100'

        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                # 设置语句级超时（毫秒），防止慢查询拖垮数据库
                cur.execute('SET statement_timeout = 10000')
                cur.execute(sql)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchmany(100)
            return rows, columns
        finally:
            self._release_connection(conn)

    # ------------------------------------------------------------------
    # 数据库连接管理
    # ------------------------------------------------------------------

    def _get_connection(self):
        """获取业务数据库连接

        优先使用 BUSINESS_DB_DSN 直连（psycopg2），
        未配置时回退到 django 默认数据库连接。
        """
        from django.conf import settings

        dsn = getattr(settings, 'BUSINESS_DB_DSN', '') or ''
        if dsn:
            import psycopg2
            return psycopg2.connect(dsn)
        # 回退到 django 默认数据库
        from django.db import connection
        return connection

    def _release_connection(self, conn):
        """释放数据库连接

        psycopg2 直连的连接需要手动关闭；
        django 的连接由框架管理，不能关闭（只 commit）。
        """
        from django.db import connection as django_conn
        if conn is django_conn:
            try:
                conn.commit()
            except Exception:
                pass
            return
        try:
            conn.close()
        except Exception:
            pass

    def _fetchall(self, sql: str, params: List = None):
        """执行查询并返回所有行（用于表结构读取）"""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                return cur.fetchall()
        finally:
            self._release_connection(conn)

    # ------------------------------------------------------------------
    # 结果格式化
    # ------------------------------------------------------------------

    def _format_result(self, sql: str, rows: List, columns: List[str]) -> str:
        """格式化查询结果为 LLM 易读的表格文本"""
        if not rows:
            return f'SQL: {sql}\n\n查询结果为空。'
        # 表格文本格式（Markdown 风格，LLM 理解友好）
        lines = [f'SQL: {sql}', '', f'共 {len(rows)} 行：']
        # 表头
        lines.append('| ' + ' | '.join(columns) + ' |')
        lines.append('| ' + ' | '.join(['---'] * len(columns)) + ' |')
        # 数据行（每单元格截断到 50 字符，防止超长）
        for row in rows[:100]:
            cells = []
            for v in row:
                s = '' if v is None else str(v)
                if len(s) > 50:
                    s = s[:50] + '...'
                cells.append(s)
            lines.append('| ' + ' | '.join(cells) + ' |')
        return '\n'.join(lines)
