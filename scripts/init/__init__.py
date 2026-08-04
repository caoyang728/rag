"""
系统初始化子模块包

按数据类型拆分，每个模块提供 create_xxx(config, dry_run) 入口，
由 scripts/init_system.py 统一编排调用。

模块清单：
- common:           公共工具（DB 连接、Django 启动、迁移检查、yaml 加载）
- roles:            角色初始化
- permissions:      权限点初始化
- role_permissions: 角色-权限映射
- departments:      部门初始化
- teams:            团队初始化
- users:            初始用户
- global_memories:  全局记忆
- system_configs:   系统配置（KV）—— 由 .env 迁移而来，运行期可前端修改
- models:           模型配置（LLM/Embedding/Rerank）—— 不含 API Key
"""
