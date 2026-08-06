"""
PostgreSQL 连接池后端 - 基于 psycopg_pool 的 Django 数据库包装器

核心设计:
- 类级别共享 ConnectionPool: 同一进程内所有线程复用同一连接池
- get_new_connection() 从池中获取连接而非新建
- close() 将连接归还池中而非关闭
- close_if_unusable_or_obsolete() 检测坏连接并丢弃（不归还池中）
- 池自动自愈: max_lifetime 到期自动回收重建，断连由 psycopg_pool 内部机制重连

与 Django 原生 CONN_MAX_AGE 区别:
- CONN_MAX_AGE 控制单连接存活时间，到期后由 Django 关闭并重建
- 连接池管理一组连接的获取/归还/回收，粒度更粗、控制更灵活
- 启用连接池后 CONN_MAX_AGE 设为 0（禁用单连接年龄控制），由池统一管理

自愈机制:
- max_lifetime 到期: 连接在归还时被池自动回收重建
- 断连检测: close_if_unusable_or_obsolete 通过 rollback 探测，坏连接直接丢弃
- 连接耗尽: getconn 超时后抛 PoolTimeout，避免无限等待

psycopg 3.3.x 兼容性:
- psycopg 3.3 将 TimestamptzLoader.timezone 改名为 _timezone
- Django 5.2 的 create_cursor 仍访问 .timezone，导致 AttributeError
- 本模块在 create_cursor 中捕获该异常并手动处理 timezone 注册

JSONB 兼容性:
- psycopg3 默认将 JSONB 列自动反序列化为 Python 对象（list/dict）
- Django 5.2.0 的 JSONField.from_db_value 期望收到原始字符串再自行 json.loads
- 对已反序列化的 Python 对象调用 json.loads 会抛 TypeError
- 修复：通过连接池 configure 回调覆盖 JSONB loader，返回原始字符串
"""
import atexit
import logging

from django.db.backends.postgresql.base import DatabaseWrapper as PostgreSQLDatabaseWrapper

logger = logging.getLogger(__name__)


class DatabaseWrapper(PostgreSQLDatabaseWrapper):
    """使用 psycopg_pool 连接池的 PostgreSQL DatabaseWrapper。

    线程安全: ConnectionPool 内部已实现线程锁，可安全跨线程共享。
    生命周期: 池在首次获取连接时懒初始化，随 Python 进程退出而销毁。
    """

    # 类级别共享池: 同一进程内所有 DatabaseWrapper 实例共用
    _pool = None
    _pool_initialized = False
    # 记录池配置用于日志
    _pool_config = {}
    # atexit 是否已注册
    _atexit_registered = False
    # JSONB OID（PostgreSQL 固定值）
    _JSONB_OID = 3802

    def get_connection_params(self):
        """提取连接参数和池配置

        Django 调用此方法时不传参数（使用 self.settings_dict）。
        池配置在首次调用时从 settings_dict 中提取并缓存。
        """
        params = super().get_connection_params()
        # 仅首次提取池配置（之后 self._pool_options 已缓存）
        if not hasattr(self, '_pool_options'):
            self._pool_options = self.settings_dict.get('POOL_OPTIONS', {})
        return params

    def create_cursor(self, name=None):
        """创建游标，兼容 psycopg 3.3.x 与 Django 5.2 的 timezone 属性差异

        psycopg 3.3 将 TimestamptzLoader.timezone 改名为 _timezone，
        Django 5.2 仍访问 .timezone，导致 AttributeError。
        此处捕获该异常并使用 _timezone 作为兜底。
        """
        try:
            return super().create_cursor(name)
        except AttributeError as e:
            if 'timezone' not in str(e):
                raise
            # psycopg 3.3.x timezone 属性名变更导致的异常，手动处理
            logger.debug(f'create_cursor 触发 psycopg timezone 兼容修复: {e}')
            return self._create_cursor_with_timezone_fallback(name)

    def _create_cursor_with_timezone_fallback(self, name=None):
        """手动创建游标并处理 timezone 注册

        psycopg 3.3.x 的 TimestamptzLoader 使用 _timezone 而非 timezone，
        此处使用兼容的方式获取 loader 的 timezone 属性。
        """
        if self.connection is None:
            self.ensure_connection()

        from psycopg import adapters
        from psycopg.pq import Format
        from django.db.backends.postgresql.psycopg_any import register_tzloader

        TIMESTAMPTZ_OID = adapters.types["timestamptz"].oid

        cursor = self.connection.cursor() if name is None else self.connection.cursor(
            name, scrollable=False, withhold=self.connection.autocommit
        )
        try:
            tzloader = self.connection.adapters.get_loader(TIMESTAMPTZ_OID, Format.TEXT)
            # psycopg 3.3.x 使用 _timezone 而非 timezone，使用 getattr 兼容
            loader_tz = getattr(tzloader, 'timezone', None) or getattr(tzloader, '_timezone', None)
            if self.timezone != loader_tz:
                register_tzloader(self.timezone, cursor)
        except Exception:
            # timezone 注册失败不影响核心查询功能
            logger.debug('timezone 注册失败，继续使用游标')
        return cursor

    def get_new_connection(self, conn_params):
        """从连接池获取连接，池未初始化时先初始化池"""
        if not DatabaseWrapper._pool_initialized:
            self._init_pool(conn_params)
        try:
            conn = DatabaseWrapper._pool.getconn()
        except Exception as e:
            logger.error(f'从连接池获取连接失败: {e}')
            raise
        return conn

    def _init_pool(self, conn_params):
        """初始化连接池（仅首次调用时执行一次）

        Args:
            conn_params: psycopg 连接参数字典
        """
        pool_options = getattr(self, '_pool_options', {})
        # 构造 conninfo 字符串，psycopg_pool 支持 DSN 格式
        conninfo = self._build_conninfo(conn_params)
        try:
            from psycopg_pool import ConnectionPool
            DatabaseWrapper._pool = ConnectionPool(
                conninfo=conninfo,
                min_size=pool_options.get('min_size', 2),
                max_size=pool_options.get('max_size', 20),
                timeout=pool_options.get('timeout', 30),
                max_lifetime=pool_options.get('max_lifetime', 1800),
                max_idle=pool_options.get('max_idle', 600),
                # 每个新连接创建后执行配置回调
                configure=DatabaseWrapper._configure_pooled_connection,
            )
            DatabaseWrapper._pool_initialized = True
            DatabaseWrapper._pool_config = {
                'min_size': pool_options.get('min_size', 2),
                'max_size': pool_options.get('max_size', 20),
                'timeout': pool_options.get('timeout', 30),
                'max_lifetime': pool_options.get('max_lifetime', 1800),
                'max_idle': pool_options.get('max_idle', 600),
            }
            logger.info(
                f'PostgreSQL 连接池初始化成功: '
                f"minconn={DatabaseWrapper._pool_config['min_size']}, "
                f"maxconn={DatabaseWrapper._pool_config['max_size']}, "
                f"max_lifetime={DatabaseWrapper._pool_config['max_lifetime']}s, "
                f"max_idle={DatabaseWrapper._pool_config['max_idle']}s"
            )
            # 注册进程退出时的池清理
            if not DatabaseWrapper._atexit_registered:
                atexit.register(DatabaseWrapper.close_pool)
                DatabaseWrapper._atexit_registered = True
        except Exception as e:
            logger.error(f'连接池初始化失败: {e}')
            raise

    @classmethod
    def _configure_pooled_connection(cls, conn):
        """池化连接创建后的配置回调

        覆盖 JSONB loader 使其返回原始字符串而非 Python 对象。
        psycopg3 默认自动反序列化 JSONB → Python list/dict，
        但 Django 5.2.0 的 JSONField.from_db_value 期望字符串再自行 json.loads，
        对 Python 对象调用 json.loads 会抛 TypeError。
        """
        from psycopg.types.json import _JsonLoader

        class RawJsonLoader(_JsonLoader):
            """返回 JSONB 原始字符串，由 Django JSONField 负责反序列化"""
            def load(self, data):
                if isinstance(data, memoryview):
                    data = bytes(data)
                return data.decode('utf-8') if isinstance(data, bytes) else data

        conn.adapters.register_loader(cls._JSONB_OID, RawJsonLoader)

    @staticmethod
    def _build_conninfo(conn_params):
        """将连接参数字典转为 PostgreSQL DSN 字符串

        Args:
            conn_params: psycopg 连接参数字典
        Returns:
            postgresql://user:password@host:port/dbname 格式的 DSN
        """
        host = conn_params.get('host', 'localhost')
        port = conn_params.get('port', '5432')
        dbname = conn_params.get('dbname', '')
        user = conn_params.get('user', '')
        password = conn_params.get('password', '')
        dsn = f'postgresql://{user}:{password}@{host}:{port}/{dbname}'
        return dsn

    def close(self):
        """将连接归还池中而非物理关闭

        先将 self.connection 置 None 防止重复归还，再调用 putconn 归还连接。
        若归还失败（如池已关闭），则静默忽略。
        """
        if self.connection is not None and DatabaseWrapper._pool is not None:
            conn_to_return = self.connection
            self.connection = None
            try:
                DatabaseWrapper._pool.putconn(conn_to_return)
            except Exception as e:
                logger.warning(f'归还连接到池失败（连接将被丢弃）: {e}')
        else:
            self.connection = None

    def close_if_unusable_or_obsolete(self):
        """健康检查: 检测并丢弃不可用的连接

        先执行 Django 原生检查（autocommit 不匹配、错误状态、CONN_MAX_AGE 过期），
        再通过 rollback 探测连接存活状态。若连接已断开，将 self.connection 置 None
        （不归还池中，避免污染池），下次 get_new_connection 将从池中获取新连接。
        """
        # 先执行 Django 原生检查
        super().close_if_unusable_or_obsolete()
        # 若 Django 检查已关闭连接，self.connection 已为 None，无需继续
        if self.connection is None:
            return
        # 通过 rollback 探测连接存活
        try:
            self.connection.rollback()
        except Exception:
            logger.warning('检测到已断开的数据库连接，已从池中丢弃')
            self.connection = None

    @classmethod
    def close_pool(cls):
        """关闭整个连接池（进程退出时由 atexit 自动调用）

        也可在 Django 应用关闭时手动调用，确保优雅关闭。
        """
        if cls._pool is not None:
            try:
                cls._pool.close()
                logger.info('PostgreSQL 连接池已关闭')
            except Exception as e:
                logger.warning(f'关闭连接池时出错: {e}')
            finally:
                cls._pool = None
                cls._pool_initialized = False
                cls._pool_config = {}

    @classmethod
    def reset_pool(cls):
        """强制关闭并重置连接池（用于测试或紧急场景）

        当数据库被重建（如 Django 测试 runner 创建新库）时，
        池中的旧连接可能指向已不存在的数据库。调用此方法
        可强制关闭所有连接并在下次访问时重新初始化。
        """
        cls.close_pool()
        logger.info('PostgreSQL 连接池已重置')

    @classmethod
    def get_pool_status(cls):
        """获取连接池当前状态（用于健康检查和监控）

        通过 psycopg_pool 的 get_stats() 获取池的内部指标，
        使用 get_stats() 而非直接访问内部属性以保证兼容性。

        Returns:
            dict: 池状态信息，包含连接数、等待数等
        """
        if cls._pool is None:
            return {'status': 'not_initialized'}
        if cls._pool.closed:
            return {'status': 'closed'}
        try:
            stats = cls._pool.get_stats()
            return {
                'status': 'ok',
                'min_size': cls._pool_config.get('min_size', 0),
                'max_size': cls._pool_config.get('max_size', 0),
                'total_connections': stats.get('pool_size', 0),
                'idle_connections': stats.get('pool_available', 0),
                'waiting_requests': stats.get('requests_waiting', 0),
                'stats': stats,
            }
        except Exception as e:
            return {'status': 'error', 'message': str(e)}


def setup_pool_cleanup():
    """预注册连接池清理逻辑（连接池在首次使用时懒初始化，atexit 自动清理）

    在 settings.py 末尾调用，确保 Django 完全初始化后再注册。
    实际清理由 atexit 处理器完成（在 _init_pool 中注册），
    此处仅作日志记录和预热检查。
    """
    logger.info('连接池机制已就绪（懒初始化，首次访问数据库时自动创建连接池）')