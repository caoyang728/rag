"""知识图谱实体关系抽取 Prompt"""

EXTRACT_PROMPT = """你是一个知识图谱抽取专家。请从以下文本中提取实体和关系。

实体类型：
- PERSON: 人物（员工、领导、客户等）
- ORG: 组织（部门、团队、公司等）
- CONCEPT: 概念（方法论、流程、框架等）
- TERM: 术语（专业名词、缩写等）
- PRODUCT: 产品/项目

关系类型：使用自然短语，如"负责"、"参与"、"隶属于"、"属于"、"包含"、"管理"、"汇报给"等

请严格按以下 JSON 格式输出，不要添加额外内容：
{{
  "entities": [
    {{"name": "实体名称", "type": "PERSON|ORG|CONCEPT|TERM|PRODUCT", "description": "实体描述，包含关键属性"}}
  ],
  "relations": [
    {{"source": "实体名称1", "target": "实体名称2", "type": "关系类型", "description": "关系描述"}}
  ]
}}

文本：
{content}
"""
