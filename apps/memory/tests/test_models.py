"""
memory app Model 测试 —— Session / SessionMemory / UserMemory / GlobalMemory

覆盖范围：
- Session（D1）：默认值（title/root_type/is_archived/is_deleted/turn_count）、字符串表示
- SessionMemory（D2）：会话一对一摘要记忆默认值、反向关联与唯一约束
- UserMemory（D3）：用户单例长期记忆默认值、一对一唯一约束
- GlobalMemory（D4）：全局记忆默认值与唯一 key 约束
"""
import pytest
from django.db import IntegrityError

from apps.users.models import User
from apps.memory.models import Session, SessionMemory, UserMemory, GlobalMemory


@pytest.mark.django_db
class TestSessionModel:
    """D1 Session 会话主表测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入测试用户"""
        self.user = User.objects.create_user(
            username='mem-model-user', email='mem-model@test.com', password='testpass123')

    def test_create_with_defaults(self):
        """创建会话应带默认值：title=新会话、root_type=company_doc、is_archived/is_deleted=False、turn_count=0"""
        s = Session.objects.create(user=self.user)
        assert s.title == '新会话'
        assert s.root_type == 'company_doc'
        assert not s.is_archived
        assert not s.is_deleted
        assert s.turn_count == 0
        # auto_now / auto_now_add 时间字段创建后不应为空
        assert s.last_active_at is not None
        assert s.created_at is not None
        assert s.updated_at is not None

    def test_str(self):
        """字符串表示格式：Sess<id>title"""
        s = Session.objects.create(user=self.user, title='项目讨论')
        assert str(s) == f'Sess<{s.id}>项目讨论'


@pytest.mark.django_db
class TestSessionMemoryModel:
    """D2 SessionMemory 会话摘要记忆测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户与会话"""
        self.user = User.objects.create_user(
            username='sm-model-user', email='sm-model@test.com', password='testpass123')
        self.session = Session.objects.create(user=self.user, title='测试会话')

    def test_create_with_defaults(self):
        """创建会话记忆应带默认值：summary 空、entities/keywords 空列表、turn_refined=0"""
        sm = SessionMemory.objects.create(session=self.session)
        assert sm.summary == ''
        assert sm.entities == []
        assert sm.keywords == []
        assert sm.turn_refined == 0
        assert sm.updated_at is not None

    def test_one_to_one_with_session(self):
        """会话与记忆一对一：related_name='memory' 反向访问，重复创建抛 IntegrityError"""
        SessionMemory.objects.create(session=self.session, summary='摘要', entities=['实体'])
        assert self.session.memory.summary == '摘要'
        with pytest.raises(IntegrityError):
            SessionMemory.objects.create(session=self.session)


@pytest.mark.django_db
class TestUserMemoryModel:
    """D3 UserMemory 用户长期记忆测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入测试用户"""
        self.user = User.objects.create_user(
            username='um-model-user', email='um-model@test.com', password='testpass123')

    def test_create_with_defaults(self):
        """用户创建信号已自动初始化记忆，默认值：preferences 空字典、domain_tags/frequent_topics 空列表、profile_text 空"""
        um = UserMemory.objects.get(user=self.user)
        assert um.preferences == {}
        assert um.domain_tags == []
        assert um.frequent_topics == []
        assert um.profile_text == ''
        assert um.session_refined_count == 0
        assert um.updated_at is not None

    def test_one_to_one_user(self):
        """每用户一条记忆：用户创建时信号自动初始化，重复创建同一用户的记忆应抛 IntegrityError"""
        # 用户创建信号已自动初始化一条 UserMemory
        assert UserMemory.objects.filter(user=self.user).count() == 1
        with pytest.raises(IntegrityError):
            UserMemory.objects.create(user=self.user)


@pytest.mark.django_db
class TestGlobalMemoryModel:
    """D4 GlobalMemory 全局记忆测试"""

    def test_create_with_defaults(self):
        """创建全局记忆应带默认值：scope_root_types 空（表示 all）、priority=0、is_enabled=True"""
        gm = GlobalMemory.objects.create(key='company_rules', content='规则内容')
        assert gm.scope_root_types == []
        assert gm.priority == 0
        assert gm.is_enabled
        assert gm.created_at is not None
        assert gm.updated_at is not None

    def test_str(self):
        """字符串表示格式：GM<key>"""
        gm = GlobalMemory.objects.create(key='company_rules', content='规则')
        assert str(gm) == 'GM<company_rules>'

    def test_unique_key(self):
        """key 唯一：重复创建同一 key 应抛 IntegrityError"""
        GlobalMemory.objects.create(key='company_rules', content='规则一')
        with pytest.raises(IntegrityError):
            GlobalMemory.objects.create(key='company_rules', content='规则二')
