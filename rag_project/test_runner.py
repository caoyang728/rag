"""
自定义 Django Test Runner
- 在测试数据库创建后立即安装 pgvector 扩展
- 解决 Django TestCase 创建的测试数据库缺少 pgvector 扩展的问题
"""
from django.test.runner import DiscoverRunner
from django.db import connection


class VectorTestRunner(DiscoverRunner):
    """
    自定义测试运行器，在创建测试数据库后安装 pgvector 扩展。
    
    Django TestCase 创建的测试数据库不会自动包含 pgvector，
    因为 pgvector 是 PostgreSQL 扩展，需要在数据库创建后手动安装。
    这个 TestRunner 在 setup_databases 阶段完成后立即安装扩展，
    确保后续的迁移操作（如创建 VectorField 列）能够正常执行。
    """

    def setup_databases(self, **kwargs):
        """设置测试数据库并安装 pgvector 扩展
        
        执行顺序：
        1. 调用父类方法创建测试数据库
        2. 在新创建的数据库中安装 pgvector 扩展
        3. 返回配置信息供后续使用
        """
        # 先创建测试数据库
        old_config = super().setup_databases(**kwargs)
        
        # 在新创建的测试数据库中安装 pgvector 扩展
        try:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            print("[VectorTestRunner] pgvector extension installed successfully")
        except Exception as e:
            print(f"[VectorTestRunner] Failed to install pgvector: {e}")
            raise RuntimeError(
                f"pgvector extension installation failed. "
                f"Please ensure pgvector is installed on the PostgreSQL server. "
                f"Error: {e}"
            )
        
        return old_config
