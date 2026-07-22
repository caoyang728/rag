"""
记忆提炼 Prompt
会话摘要 + 用户画像更新，两阶段提炼
"""

SESSION_REFINE_SYSTEM = """你是「记忆提炼师」。请对下面这段用户与助手的对话，进行摘要提炼。
要求：
1. summary：一句话总结本次会话主题（≤ 30 字）
2. entities：从对话中抽取的关键实体（人名/产品/项目/技术名词），去重
3. keywords：中文关键词 5-10 个
只输出 JSON，禁止其他文字。
"""

SESSION_REFINE_USER_TEMPLATE = """会话内容：
{conversation}

请输出：
{{"summary":"...", "entities":["..."], "keywords":["..."]}}
"""


USER_REFINE_SYSTEM = """你是「用户画像师」。基于用户的多个会话摘要，更新用户长期画像。
要求：
1. domain_tags：用户擅长/关心的领域（≤ 8 个）
2. frequent_topics：用户高频提问主题（≤ 10 个）
3. preferences：偏好 JSON，如 {"tone":"专业","length":"详细"}
4. profile_text：一段可以直接拼进 System Prompt 的自然语言画像描述（80 字内）
只输出 JSON。
"""

USER_REFINE_USER_TEMPLATE = """历史会话摘要（最近 {count} 个）：
{summaries}

请更新用户画像。
"""
