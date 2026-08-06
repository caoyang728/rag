"""Wiki 页面生成 Prompt"""

WIKI_PAGE_PROMPT = """你是一个企业知识库编辑专家。请根据以下信息，生成一份结构化的 Wiki 知识页面。

页面主题：{title}

参考信息：
{source_info}

请生成 Markdown 格式的 Wiki 页面，包含以下章节（按需调整）：
## 概述
## 详细说明
## 相关概念
## 参考资料

要求：
- 语言专业、清晰
- 使用标题层级（## / ###）
- 适当使用列表、表格
- 总字数 500-1500 字
- 直接输出 Markdown 内容，不要用代码块包裹
"""

COMMUNITY_WIKI_PROMPT = """你是一个企业知识库编辑专家。请根据以下知识领域信息，生成一份结构化的 Wiki 知识页面。

领域主题：{topic}
领域摘要：{summary}
关键词：{keywords}
主要实体：{entities_text}
实体关系：{relations_text}

请生成 Markdown 格式的 Wiki 页面，包含以下章节：
## 领域概述
## 核心实体
## 实体关系
## 相关文档

要求：
- 语言专业、清晰
- 总字数 500-1500 字
- 直接输出 Markdown 内容，不要用代码块包裹
"""
