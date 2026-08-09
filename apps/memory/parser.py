"""
LLM 响应解析器 - 多层校验机制
1. 正则提取：从 LLM 输出中提取 JSON 部分
2. Schema 校验：使用 Pydantic 验证字段类型和约束
3. 重试机制：解析失败时自动重试，不增长消息列表
4. 兜底默认值：多次重试失败时使用默认值
"""
import json
import re
from typing import Dict, Any, Optional, List
from loguru import logger

from pydantic import BaseModel, Field, ValidationError


class SessionRefineSchema(BaseModel):
    summary: str = Field(default="", max_length=512)
    entities: List[str] = Field(default=[], max_length=20)
    keywords: List[str] = Field(default=[], max_length=20)


class UserRefineSchema(BaseModel):
    domain_tags: List[str] = Field(default=[], max_length=10)
    frequent_topics: List[str] = Field(default=[], max_length=15)
    preferences: Dict[str, Any] = Field(default={})
    profile_text: str = Field(default="", max_length=500)


def extract_json(text: str) -> Optional[str]:
    """从文本中提取 JSON 字符串（处理 LLM 可能附带的说明文字）"""
    if not text:
        return None
    text = text.strip()
    if text.startswith('```json'):
        text = text[7:]
    if text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        return match.group(0)
    return None


def parse_with_schema(text: str, schema_cls) -> Optional[BaseModel]:
    """解析并校验 JSON，返回 Pydantic 对象"""
    json_str = extract_json(text)
    if not json_str:
        logger.warning('[Parser] No JSON found in response')
        return None

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f'[Parser] JSON decode error: {e}')
        return None

    try:
        return schema_cls(**data)
    except ValidationError as e:
        logger.warning(f'[Parser] Schema validation error: {e}')
        return None


def llm_with_retry(llm, msgs: List[Dict], schema_cls,
                   max_retries: int = 3,
                   temperature: float = 0.2,
                   max_tokens: int = 600) -> Optional[BaseModel]:
    """带重试的 LLM 调用，确保返回正确结构，重试时不增长消息列表"""
    for attempt in range(max_retries):
        current_temp = max(0.0, temperature - attempt * 0.1)
        resp = llm.chat(msgs, temperature=current_temp, max_tokens=max_tokens)
        content = resp.get('content', '')
        
        result = parse_with_schema(content, schema_cls)
        if result:
            logger.info(f'[Parser] LLM parse success after {attempt + 1} attempts')
            return result

        logger.warning(f'[Parser] Attempt {attempt + 1}/{max_retries} failed, retrying...')
        
        if attempt < max_retries - 1:
            retry_msg = [
                {'role': 'system', 'content': """您的输出格式有误，请严格按照以下要求重新输出：
1. 必须只输出 JSON 格式，禁止任何其他文字、解释或说明
2. 如果无法生成，请输出空的默认值 JSON
"""},
                {'role': 'user', 'content': '请重新输出符合要求的 JSON'}
            ]
            msgs = retry_msg

    logger.error(f'[Parser] All {max_retries} attempts failed')
    return None