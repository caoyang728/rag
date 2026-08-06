"""
Django 数据库后端入口 - 连接池版 PostgreSQL

Django 通过 load_backend 加载此文件（查找 backend_name + '.base'）。
此处仅重新导出 DatabaseWrapper，供 Django 初始化连接时使用。
"""
from . import DatabaseWrapper  # noqa: F401