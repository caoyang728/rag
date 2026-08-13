"""
agent.tools.text2sql 单元测试

Text2SqlTool 全链路测试（纯 mock，不依赖真实数据库/LLM）：
- execute() 主流程：表结构读取 → SQL 生成 → 安全校验 → 执行 → 格式化
- 各错误分支：空问题、表结构读取失败、空 schema、SQL 生成失败、安全校验失败、
  执行失败
- _load_schema：白名单读取（SystemConfig）、tables 参数优先级、空白名单禁用
- _generate_sql：ctx.llm 优先、markdown 代码块剥离、末尾分号去除
- _validate_sql：非 SELECT / 禁止关键词 / 自动补充 LIMIT
- _execute_sql / _fetchall：statement_timeout 设置、LIMIT 兜底、连接释放
- _get_connection / _release_connection：DSN 直连与 Django 连接回退
- _format_result：空结果 / 表格渲染 / 超长单元格截断

Mock 说明：
- _load_schema / _generate_sql 内部通过函数内 import 引用
  apps.system.config_loader.get_config_value 与 apps.llm.factory.get_llm，
  按定义处 patch 即可生效。
- 测试 DSN 直连分支时向 sys.modules 注入一个假的 psycopg 模块，
  仅验证连接串传递逻辑。
"""
import sys
import types

import pytest
from unittest.mock import patch, MagicMock, PropertyMock  # noqa: F401

from apps.agent.tools.base import ToolContext, ToolRegistry
from apps.agent.tools.text2sql import Text2SqlTool

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# execute() 主流程与错误分支
# ---------------------------------------------------------------------------

class TestText2SqlExecute:
    """Text2SqlTool.execute() 全流程与错误处理"""

    def _tool_with(self, **mocks):
        """构造工具实例并 patch 指定的内部方法，返回 (tool, mock 引用 dict)"""
        tool = Text2SqlTool()
        refs = {}
        for name, value in mocks.items():
            if isinstance(value, dict) and 'return_value' in value:
                p = patch.object(tool, name, return_value=value['return_value'])
            else:
                p = patch.object(tool, name, side_effect=value)
            p.start()
            refs[name] = p
        return tool, refs

    def test_execute_when_normal_then_returns_result(self):
        """正常链路：schema → SQL → 校验 → 执行 → 格式化"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', return_value={'users': []}), \
                patch.object(tool, '_generate_sql',
                             return_value='SELECT * FROM users LIMIT 5'), \
                patch.object(tool, '_validate_sql', return_value=None), \
                patch.object(tool, '_execute_sql', return_value=([(1, 'a')], ['id', 'name'])), \
                patch.object(tool, '_format_result', return_value='格式化结果'):
            ret = tool.execute(ToolContext(), '统计用户数', tables=['users'])

        assert ret['ok'] is True
        assert ret['result'] == '格式化结果'
        assert ret['meta'] == {'sql': 'SELECT * FROM users LIMIT 5', 'rows': 1,
                               'columns': ['id', 'name']}

    def test_execute_when_empty_question_then_returns_error(self):
        """空字符串 / 非字符串问题直接拒绝"""
        tool = Text2SqlTool()
        ret = tool.execute(ToolContext(), '')
        assert ret['ok'] is False
        assert '查询问题不能为空' in ret['result']
        ret = tool.execute(ToolContext(), 123)
        assert ret['ok'] is False
        assert '查询问题不能为空' in ret['result']

    def test_execute_when_load_schema_fails_then_returns_error(self):
        """读取表结构失败：返回错误信息与 error meta"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', side_effect=ValueError('db down')):
            ret = tool.execute(ToolContext(), '统计用户数')
        assert ret['ok'] is False
        assert '读取数据库表结构失败' in ret['result']
        assert ret['meta']['error'] == 'db down'

    def test_execute_when_empty_schema_then_returns_error(self):
        """白名单为空（无任何可查询表）：Text2SQL 不生效"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', return_value={}):
            ret = tool.execute(ToolContext(), '统计用户数')
        assert ret['ok'] is False
        assert '未找到可查询的表' in ret['result']

    def test_execute_when_generate_sql_fails_then_returns_error(self):
        """LLM 生成 SQL 失败"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', return_value={'users': []}), \
                patch.object(tool, '_generate_sql', side_effect=RuntimeError('llm down')):
            ret = tool.execute(ToolContext(), '统计用户数')
        assert ret['ok'] is False
        assert 'SQL 生成失败' in ret['result']

    def test_execute_when_empty_sql_then_returns_error(self):
        """LLM 未生成有效 SQL"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', return_value={'users': []}), \
                patch.object(tool, '_generate_sql', return_value=''):
            ret = tool.execute(ToolContext(), '统计用户数')
        assert ret['ok'] is False
        assert 'LLM 未能生成有效 SQL' in ret['result']
        assert ret['meta']['schema'] == {'users': []}

    def test_execute_when_validation_fails_then_returns_error(self):
        """安全校验失败：返回校验错误"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', return_value={'users': []}), \
                patch.object(tool, '_generate_sql',
                             return_value='DELETE FROM users'), \
                patch.object(tool, '_validate_sql', return_value='SQL 必须以 SELECT 开头'):
            ret = tool.execute(ToolContext(), '统计用户数')
        assert ret['ok'] is False
        assert 'SQL 安全校验失败' in ret['result']
        assert ret['meta']['sql'] == 'DELETE FROM users'

    def test_execute_when_execute_fails_then_returns_error(self):
        """SQL 执行失败：返回异常类名 + 截断的错误信息"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', return_value={'users': []}), \
                patch.object(tool, '_generate_sql', return_value='SELECT 1'), \
                patch.object(tool, '_validate_sql', return_value=None), \
                patch.object(tool, '_execute_sql', side_effect=RuntimeError('exec failed')):
            ret = tool.execute(ToolContext(), '统计用户数')
        assert ret['ok'] is False
        assert 'SQL 执行失败' in ret['result']
        assert 'RuntimeError' in ret['result']
        assert 'exec failed' in ret['result']

    def test_execute_when_non_select_then_validation_blocks(self):
        """不 mock _validate_sql：生成非 SELECT 语句被真实校验拦截"""
        tool = Text2SqlTool()
        with patch.object(tool, '_load_schema', return_value={'users': []}), \
                patch.object(tool, '_generate_sql',
                             return_value='DROP TABLE users'), \
                patch.object(tool, '_execute_sql') as mock_exec:
            ret = tool.execute(ToolContext(), '统计用户数')
        assert ret['ok'] is False
        assert 'SQL 安全校验失败' in ret['result']
        mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# _load_schema：表结构读取
# ---------------------------------------------------------------------------

class TestText2SqlLoadSchema:
    """_load_schema：白名单配置与表结构组装"""

    def test_load_schema_then_assembles_schema_from_whitelist(self):
        """从 SystemConfig 读取 BUSINESS_DB_TABLES 白名单并组装 schema"""
        tool = Text2SqlTool()
        rows = [
            ('users', 'id', 'integer', 'NO', '主键'),
            ('users', 'name', 'text', 'YES', None),
            ('orders', 'id', 'bigint', 'NO', '订单ID'),
        ]
        with patch('apps.system.config_loader.get_config_value',
                   return_value='users, orders') as mock_cfg, \
                patch.object(tool, '_fetchall', return_value=rows) as mock_fetch:
            schema = tool._load_schema()

        mock_cfg.assert_called_once_with('BUSINESS_DB_TABLES', default='',
                                         value_type='string')
        assert schema == {
            'users': [
                {'name': 'id', 'type': 'integer', 'nullable': False, 'comment': '主键'},
                {'name': 'name', 'type': 'text', 'nullable': True, 'comment': ''},
            ],
            'orders': [
                {'name': 'id', 'type': 'bigint', 'nullable': False, 'comment': '订单ID'},
            ],
        }
        # SQL 按白名单过滤，参数按表名透传
        sql, params = mock_fetch.call_args[0]
        assert 'IN (%s,%s)' in sql
        assert params == ['users', 'orders']

    def test_load_schema_when_tables_param_then_overrides_config(self):
        """显式 tables 参数优先级高于配置白名单"""
        tool = Text2SqlTool()
        with patch('apps.system.config_loader.get_config_value',
                   return_value='users'), \
                patch.object(tool, '_fetchall', return_value=[]) as mock_fetch:
            tool._load_schema(tables=['orders'])
        _, params = mock_fetch.call_args[0]
        assert params == ['orders']

    def test_load_schema_when_empty_whitelist_then_returns_empty(self):
        """配置为空且未传 tables：返回空 schema，不执行查询（Text2SQL 不生效）"""
        tool = Text2SqlTool()
        with patch('apps.system.config_loader.get_config_value', return_value=''), \
                patch.object(tool, '_fetchall') as mock_fetch:
            assert tool._load_schema() == {}
            mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# _generate_sql：SQL 生成
# ---------------------------------------------------------------------------

class TestText2SqlGenerateSql:
    """_generate_sql：表结构序列化 + LLM 调用 + 容错清理"""

    SCHEMA = {
        'users': [
            {'name': 'id', 'type': 'integer', 'nullable': False, 'comment': '主键'},
            {'name': 'name', 'type': 'text', 'nullable': True, 'comment': ''},
        ],
    }

    def test_generate_sql_when_ctx_llm_then_returns_cleaned_sql(self):
        """优先使用 ctx.llm 生成 SQL，剥离 markdown 代码块与末尾分号"""
        tool = Text2SqlTool()
        llm = MagicMock()
        llm.chat.return_value = {'content': '```sql\nSELECT * FROM users;\n```'}
        ctx = ToolContext(llm=llm)
        sql = tool._generate_sql(ctx, '统计用户数', self.SCHEMA)
        assert sql == 'SELECT * FROM users'
        # 序列化的表结构以 DDL 风格传入 prompt
        system_content = llm.chat.call_args[0][0][0]['content']
        assert 'CREATE TABLE users (' in system_content
        assert '  id integer NOT NULL -- 主键' in system_content
        assert '  name text' in system_content
        assert 'temperature=0.0' not in repr(llm.chat.call_args.kwargs)
        assert llm.chat.call_args.kwargs['temperature'] == 0.0

    def test_generate_sql_when_no_ctx_llm_then_falls_back_to_get_llm(self):
        """ctx.llm 为空时回退到 get_llm()"""
        tool = Text2SqlTool()
        llm = MagicMock()
        llm.chat.return_value = {'content': 'SELECT count(*) FROM users LIMIT 1'}
        with patch('apps.llm.factory.get_llm', return_value=llm) as mock_get_llm:
            sql = tool._generate_sql(ToolContext(), '统计用户数', self.SCHEMA)
        mock_get_llm.assert_called_once()
        assert sql == 'SELECT count(*) FROM users LIMIT 1'

    def test_generate_sql_when_content_none_then_returns_empty(self):
        """LLM 返回空 content：得到空字符串（由调用方判空）"""
        tool = Text2SqlTool()
        llm = MagicMock()
        llm.chat.return_value = {'content': None}
        assert tool._generate_sql(ToolContext(llm=llm), 'q', self.SCHEMA) == ''


# ---------------------------------------------------------------------------
# _validate_sql：安全校验
# ---------------------------------------------------------------------------

class TestText2SqlValidateSql:
    """_validate_sql：仅 SELECT + 禁止关键词 + LIMIT 兜底"""

    def test_validate_sql_when_non_select_then_rejected(self):
        tool = Text2SqlTool()
        assert tool._validate_sql('SELECT 1') is None  # SELECT 开头通过
        # 不以 SELECT 开头（WITH/EXPLAIN/DELETE）一律拒绝
        for sql in ('WITH x AS (SELECT 1) SELECT * FROM x',
                    'EXPLAIN SELECT * FROM users',
                    'DELETE FROM users'):
            assert tool._validate_sql(sql) == 'SQL 必须以 SELECT 开头'

    def test_validate_sql_when_forbidden_keyword_then_rejected(self):
        tool = Text2SqlTool()
        for sql in ('SELECT * FROM users; DROP TABLE users',
                    'SELECT * FROM users WHERE note LIKE \'%insert%\'',
                    'SELECT pg_sleep(10)'):
            err = tool._validate_sql(sql)
            assert err is not None
            assert '禁止的关键词' in err

    def test_validate_sql_when_select_then_passes(self):
        """无 LIMIT 时校验仍通过（执行阶段补充）；带 LIMIT 直接通过"""
        tool = Text2SqlTool()
        assert tool._validate_sql('SELECT * FROM users') is None
        assert tool._validate_sql('SELECT * FROM users LIMIT 10') is None
        assert tool._validate_sql('select * from users limit 10') is None


# ---------------------------------------------------------------------------
# _execute_sql / _fetchall：SQL 执行
# ---------------------------------------------------------------------------

class TestText2SqlExecuteSql:
    """_execute_sql：statement_timeout + LIMIT 兜底 + 连接释放"""

    def _fake_conn(self, description=None, rows=None):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.description = description
        cur.fetchmany.return_value = rows or []
        cur.fetchall.return_value = rows or []
        return conn, cur

    def test_execute_sql_when_normal_then_returns_rows_and_columns(self):
        tool = Text2SqlTool()
        conn, cur = self._fake_conn(description=[('id',), ('name',)], rows=[(1, 'a')])
        with patch.object(tool, '_get_connection', return_value=conn), \
                patch.object(tool, '_release_connection') as mock_release:
            rows, columns = tool._execute_sql('SELECT * FROM users LIMIT 10')

        assert rows == [(1, 'a')]
        assert columns == ['id', 'name']
        calls = [c[0][0] for c in cur.execute.call_args_list]
        assert calls[0] == 'SET statement_timeout = 10000'
        assert calls[1] == 'SELECT * FROM users LIMIT 10'
        mock_release.assert_called_once_with(conn)

    def test_execute_sql_when_no_limit_then_appends_limit(self):
        """SQL 无 LIMIT 时自动补充 LIMIT 100（双重保险）"""
        tool = Text2SqlTool()
        conn, cur = self._fake_conn(description=[('id',)], rows=[])
        with patch.object(tool, '_get_connection', return_value=conn), \
                patch.object(tool, '_release_connection'):
            tool._execute_sql('SELECT * FROM users')
        assert cur.execute.call_args_list[1][0][0] == 'SELECT * FROM users LIMIT 100'

    def test_execute_sql_when_no_description_then_returns_empty_columns(self):
        """cursor 无 description（非查询语句）时列名为空"""
        tool = Text2SqlTool()
        conn, cur = self._fake_conn(description=None)
        with patch.object(tool, '_get_connection', return_value=conn), \
                patch.object(tool, '_release_connection'):
            rows, columns = tool._execute_sql('SELECT 1')
        assert columns == []
        assert rows == []

    def test_execute_sql_when_exception_then_releases_connection(self):
        """即使执行抛异常也必须释放连接（finally 保证）"""
        tool = Text2SqlTool()
        conn, cur = self._fake_conn()
        cur.execute.side_effect = Exception('query timeout')
        with patch.object(tool, '_get_connection', return_value=conn), \
                patch.object(tool, '_release_connection') as mock_release:
            with pytest.raises(Exception):
                tool._execute_sql('SELECT 1')
        mock_release.assert_called_once_with(conn)

    def test_fetchall_when_normal_then_returns_all_rows(self):
        """_fetchall：带参数执行并返回全部行"""
        tool = Text2SqlTool()
        conn, cur = self._fake_conn(rows=[('a', 1), ('b', 2)])
        with patch.object(tool, '_get_connection', return_value=conn), \
                patch.object(tool, '_release_connection') as mock_release:
            rows = tool._fetchall('SELECT * FROM t WHERE x = %s', ['v'])
        assert rows == [('a', 1), ('b', 2)]
        cur.execute.assert_called_once_with('SELECT * FROM t WHERE x = %s', ['v'])
        mock_release.assert_called_once_with(conn)


# ---------------------------------------------------------------------------
# _get_connection / _release_connection：连接管理
# ---------------------------------------------------------------------------

class TestText2SqlConnection:
    """_get_connection / _release_connection：DSN 直连与 Django 连接回退"""

    def test_connect_when_dsn_then_direct_connection(self):
        """配置了 BUSINESS_DB_DSN：走 psycopg.connect（本环境注入假模块验证）"""
        from django.conf import settings
        fake = types.ModuleType('psycopg')
        fake.connect = MagicMock(return_value='dsn_conn')
        sys.modules['psycopg'] = fake
        try:
            with patch.object(settings, 'BUSINESS_DB_DSN',
                              'postgresql://u:p@h:5432/db', create=True):
                tool = Text2SqlTool()
                conn = tool._get_connection()
        finally:
            del sys.modules['psycopg']
        fake.connect.assert_called_once_with('postgresql://u:p@h:5432/db')
        assert conn == 'dsn_conn'

    def test_connect_when_no_dsn_then_falls_back_to_django(self):
        """未配置 DSN：回退到 django 默认数据库连接"""
        from django.conf import settings
        from django.db import connection
        with patch.object(settings, 'BUSINESS_DB_DSN', '', create=True):
            tool = Text2SqlTool()
            assert tool._get_connection() is connection

    def test_release_connection_when_django_then_commits(self):
        """Django 连接由框架管理：只 commit 不 close"""
        tool = Text2SqlTool()
        with patch('django.db.connection') as mock_django_conn:
            tool._release_connection(mock_django_conn)
        mock_django_conn.commit.assert_called_once()
        mock_django_conn.close.assert_not_called()

    def test_release_connection_when_psycopg_then_closes(self):
        """psycopg3 直连连接需要手动 close"""
        tool = Text2SqlTool()
        conn = MagicMock()
        with patch('django.db.connection', MagicMock()):
            tool._release_connection(conn)
        conn.close.assert_called_once()

    def test_release_connection_when_error_then_swallows(self):
        """连接释放异常静默吞掉（不阻断主流程）"""
        tool = Text2SqlTool()
        conn = MagicMock()
        conn.close.side_effect = Exception('close failed')
        with patch('django.db.connection', MagicMock()):
            tool._release_connection(conn)  # 不应抛异常
        conn.commit.side_effect = Exception('commit failed')
        with patch('django.db.connection', conn):
            tool._release_connection(conn)  # 不应抛异常


# ---------------------------------------------------------------------------
# _format_result：结果格式化
# ---------------------------------------------------------------------------

class TestText2SqlFormatResult:
    """_format_result：LLM 易读的表格文本"""

    def test_format_result_when_empty_rows_then_returns_empty_message(self):
        tool = Text2SqlTool()
        text = tool._format_result('SELECT 1', [], ['id'])
        assert '查询结果为空' in text

    def test_format_result_when_normal_then_renders_table(self):
        tool = Text2SqlTool()
        text = tool._format_result('SELECT id, name FROM users LIMIT 2',
                                   [(1, '张三'), (2, '李四')], ['id', 'name'])
        assert 'SQL: SELECT id, name FROM users LIMIT 2' in text
        assert '共 2 行' in text
        assert '| id | name |' in text
        assert '| --- | --- |' in text
        assert '| 1 | 张三 |' in text
        assert '| 2 | 李四 |' in text

    def test_format_result_when_long_cell_then_truncated(self):
        """超长单元格截断到 50 字符；None 渲染为空串"""
        tool = Text2SqlTool()
        long_val = 'x' * 80
        text = tool._format_result('SELECT 1', [(long_val, None)], ['a', 'b'])
        assert 'x' * 50 + '...' in text
        # None 显示为空单元格（'|  |' 形式）
        assert '| ' + 'x' * 50 + '... |  |' in text


# ---------------------------------------------------------------------------
# 工具注册表集成（text2sql 通过注册表执行）
# ---------------------------------------------------------------------------

class TestText2SqlRegistryIntegration:
    """text2sql 注册进默认注册表后可通过名称执行"""

    def test_registry_when_registered_then_executes_by_name(self):
        registry = ToolRegistry()
        registry.register(Text2SqlTool())
        assert 'text2sql' in registry.names()
        tool_schema = registry.to_openai_tools(['text2sql'])[0]
        assert tool_schema['function']['name'] == 'text2sql'
        # 空问题快速失败，不依赖任何外部服务
        ret = registry.execute('text2sql', {'question': ''}, ToolContext())
        assert ret['ok'] is False
        assert ret['tool_name'] == 'text2sql'
