"""
apps.llm 模型测试 —— LLMModel 模型配置

说明：apps/llm 自身无 Django Model（models.py 仅有一行注释），
LLM/Embedding/Rerank 的模型配置 LLMModel 定义在 apps.system.models，
llm 的 factory/embedding 均通过 config_loader 读取该表。
本文件按任务要求覆盖 LLMModel 的创建默认值、字符串表示、
model_type 枚举校验与 provider 字段行为。
"""
import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.system.models import LLMModel


@pytest.mark.django_db
class TestLLMModel:
    """LLMModel 模型测试（模型本体位于 apps.system.models）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入测试模型"""
        self.model = LLMModel.objects.create(
            name='DeepSeek Chat',
            provider='deepseek',
            model_type='llm',
            model_name='deepseek-chat',
        )

    def test_create_with_defaults(self):
        """创建默认值：timeout 为空、is_active=True、base_url 空"""
        m = LLMModel.objects.create(
            name='BGE Embedding', provider='bge', model_type='embedding', model_name='bge-m3')
        assert m.timeout is None
        assert m.is_active
        assert m.base_url == ''
        # auto_now / auto_now_add 时间字段创建后不应为空
        assert m.created_at is not None
        assert m.updated_at is not None

    def test_str(self):
        """字符串表示格式：[model_type] name (model_name)"""
        assert str(self.model) == '[llm] DeepSeek Chat (deepseek-chat)'

    def test_model_type_valid_choices(self):
        """三种合法 model_type 均可创建成功"""
        for mt in ['llm', 'embedding', 'rerank']:
            LLMModel.objects.create(
                name=f'Model-{mt}', provider='p', model_type=mt, model_name=f'm-{mt}')
        assert LLMModel.objects.count() == 4

    def test_model_type_invalid_choice(self):
        """非法 model_type 应被 full_clean 校验拒绝（choices 枚举限制）"""
        bad = LLMModel(name='Bad', provider='p', model_type='chat', model_name='bad-model')
        with pytest.raises(ValidationError):
            bad.full_clean()

    def test_provider_stored(self):
        """provider 为自由字符串，应按原值存储"""
        assert self.model.provider == 'deepseek'

    def test_unique_together(self):
        """同一 (model_type, name) 重复创建应抛 IntegrityError"""
        with pytest.raises(IntegrityError):
            LLMModel.objects.create(
                name='DeepSeek Chat', provider='openai',
                model_type='llm', model_name='other-model')
