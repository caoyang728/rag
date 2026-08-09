"""
apps.knowledge.views 文档活跃版本测试 —— is_active / set_active / 版本切换

覆盖范围：
- 纯逻辑（unit）：_capture_content_sample / _text_similarity / _is_version_upload
- 上传新版本（integration）：相似内容 → 旧版本自动置非活跃；同名不同内容 → 全部保留；
  二进制文件无法即时读文 → 默认按新版本处理
- 跨团队/跨部门隔离：同名同内容互不影响（版本组按 node+file_name+dept_id+team_id 四元组判定）
- 列表默认仅返回活跃版本，?version=all 返回全部
- versions 版本列表 / set_active 切换活跃 / 非 Owner 403 / 已删除 400
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.knowledge.models import Document, VisibilityLevel
from apps.users.models import Team
from apps.knowledge.views import (
    _capture_content_sample, _text_similarity, _is_version_upload,
    DocumentUploadView,
)
from apps.knowledge.tests.test_views import (
    _auth_headers, _create_document, KnowledgeViewsExtraBase,
)


def _results(resp):
    """兼容分页/非分页响应，提取结果列表"""
    data = resp.json()
    return data['results'] if isinstance(data, dict) and 'results' in data else data


# ============================================================================
# 纯逻辑：内容样本 / 相似度 / 版本判定
# ============================================================================
class TestVersionHelpers:
    """版本判定辅助函数单元测试（无 DB 依赖）"""

    @pytest.mark.unit
    def test_capture_content_sample_normalizes_whitespace(self):
        """文本样本折叠空白：换行/缩进不影响相似度判定"""
        sample = _capture_content_sample(b'line1\n  line2\tline3', 'txt')
        assert sample == 'line1 line2 line3'

    @pytest.mark.unit
    def test_capture_content_sample_binary_returns_empty(self):
        """二进制文件类型无法即时读文 → 样本为空串（调用方按新版本处理）"""
        assert _capture_content_sample(b'%PDF-1.4 fake', 'pdf') == ''

    @pytest.mark.unit
    def test_text_similarity_similar_content_high(self):
        """相似内容相似度应明显高于阈值"""
        a = '会议纪要：2026 年度总结。参会人：张三、李四。'
        b = '会议纪要：2026 年度总结。参会人：张三、李四。补充：Q3 计划已确认。'
        assert _text_similarity(a, b) > 0.5

    @pytest.mark.unit
    def test_text_similarity_different_content_low(self):
        """截然不同内容相似度明显低于相似内容（代码文件须低于 code 阈值 0.8）"""
        a = 'def add(a, b):\n    return a + b'
        b = 'import re\n\ndef clean_text(raw):\n    return re.sub(r"<[^>]+>", "", raw)'
        similar = 'def add(a, b):\n    return a + b  # 补充注释'
        assert _text_similarity(a, b) < _text_similarity(a, similar)
        assert _text_similarity(a, b) < 0.8

    @pytest.mark.unit
    def test_is_version_upload_no_siblings_false(self):
        """无同组文档 → 首传，不是新版本"""
        assert _is_version_upload('txt', 'some sample', []) is False

    @pytest.mark.unit
    def test_is_version_upload_binary_true(self):
        """二进制文件（无文本样本）→ 保守按新版本处理"""
        sib = SimpleNamespace(content_sample='')
        assert _is_version_upload('pdf', '', [sib]) is True

    @pytest.mark.unit
    def test_is_version_upload_similar_text_true(self):
        """同组存在相似文本 → 视为新版本"""
        sib = SimpleNamespace(content_sample='会议纪要：2026 年度总结。参会人：张三、李四。')
        sample = '会议纪要：2026 年度总结。参会人：张三、李四。补充：Q3 计划已确认。'
        assert _is_version_upload('txt', sample, [sib]) is True

    @pytest.mark.unit
    def test_is_version_upload_dissimilar_text_false(self):
        """同组存在同名但内容迥异的文本 → 视为独立文档，全部保留"""
        sib = SimpleNamespace(content_sample='select name from t_user where status = 1')
        sample = 'def add(a, b):\n    return a + b'
        assert _is_version_upload('code', sample, [sib]) is False

    @pytest.mark.unit
    def test_is_version_upload_code_boilerplate_not_version(self):
        """不同项目同名代码文件（共享框架样板代码）→ 非版本，全部保留

        两个 Django views.py 即使都含 import/def/return 等常见样板，
        只要整体内容高度不同，就不应互相覆盖（code 阈值 0.8）。
        """
        view1 = ("from django.http import HttpResponse\n"
                 "def index(request):\n    return HttpResponse('home')\n")
        view2 = ("from django.http import JsonResponse\n"
                 "def api_stats(request):\n    return JsonResponse({'ok': True})\n")
        assert _is_version_upload('code', view1, [SimpleNamespace(content_sample=view2)]) is False

    @pytest.mark.unit
    def test_is_version_upload_code_near_identical_true(self):
        """同一代码文件仅小幅改动 → 视为新版本（code 阈值下仍能命中）"""
        v1 = ("from django.http import HttpResponse\n"
              "def index(request):\n    return HttpResponse('home')\n")
        v2 = ("from django.http import HttpResponse\n"
              "def index(request):\n    return HttpResponse('home')  # add cache\n")
        assert _is_version_upload('code', v2, [SimpleNamespace(content_sample=v1)]) is True

    @pytest.mark.unit
    def test_is_version_upload_config_dissimilar_false(self):
        """同名字段不同的配置文件 → 独立文档（config 阈值 0.8）"""
        sib = SimpleNamespace(content_sample='version: "3"\nservices:\n  web:\n    image: nginx\n')
        sample = ('server {\n    listen 80;\n    server_name example.com;\n'
                  '    location / { proxy_pass http://app; }\n}\n')
        assert _is_version_upload('config', sample, [sib]) is False


# ============================================================================
# 上传：版本判定与活跃标记
# ============================================================================
class TestActiveVersionUpload(KnowledgeViewsExtraBase):
    """上传新版本 / 同名独立文档 / 跨团队隔离的活跃标记测试"""

    def _upload(self, user, filename, content, mime='text/plain', node_id=None, **overrides):
        """构造上传请求（super_admin 使用，可指定 node）"""
        data = {
            'file': SimpleUploadedFile(
                filename, content if isinstance(content, bytes) else content.encode('utf-8'),
                content_type=mime),
            'node_id': node_id or self.category_node.id,
            'visibility_level': 'TEAM_ONLY',
        }
        data.update(overrides)
        return self.client.post(
            '/api/v1/knowledge/documents/upload/', data=data, **_auth_headers(user))

    def _make_second_team(self):
        """创建第二个团队及其节点链（root → dept → team2 → category2），返回 category2 节点"""
        team2 = Team.objects.create(name='前端组', code='rd-fe', department=self.dept)
        team_node2 = self._create_node(
            '前端组', 'folder', node_level=3, parent=self.dept_node, ref_id=team2.id)
        category2 = self._create_node(
            '前端业务', 'folder', node_level=4, parent=team_node2)
        return team2, category2

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/meeting.txt')
    def test_upload_similar_content_deactivates_old(self, mock_save, mock_magic):
        """同组相似内容再次上传 → 判定为新版本，旧版本自动置非活跃"""
        mock_magic.from_buffer.return_value = 'text/plain'
        first = self._upload(
            self.super_admin, '会议纪要.txt',
            '会议纪要：2026 年度年中总结。参会人：张三、李四。结论：Q2 目标达成率 95%。')
        assert first.status_code == 201
        doc1 = Document.objects.get(pk=first.json()['document_id'])
        assert first.json()['is_version_upload'] is False
        assert doc1.is_active is True

        second = self._upload(
            self.super_admin, '会议纪要.txt',
            '会议纪要：2026 年度年中总结。参会人：张三、李四。结论：Q2 目标达成率 95%。'
            '补充：Q3 将上线新版本检索系统。')
        assert second.status_code == 201
        assert second.json()['is_version_upload'] is True
        doc2 = Document.objects.get(pk=second.json()['document_id'])
        assert doc2.version == 2
        doc1.refresh_from_db()
        assert doc1.is_active is False
        assert doc2.is_active is True

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/views.py')
    def test_upload_same_name_different_content_keeps_both(self, mock_save, mock_magic):
        """同名代码文件内容迥异 → 视为独立文档，两个版本全部保留活跃"""
        mock_magic.from_buffer.return_value = 'text/plain'
        code1 = ("from rest_framework.views import APIView\n"
                 "class PingView(APIView):\n"
                 "    def get(self, request):\n"
                 "        return Response({'pong': True})\n")
        code2 = ("import re\n"
                 "\n"
                 "def clean_text(raw):\n"
                 "    return re.sub(r'<[^>]+>', '', raw)\n")
        first = self._upload(self.super_admin, 'views.py', code1)
        assert first.status_code == 201
        doc1 = Document.objects.get(pk=first.json()['document_id'])
        second = self._upload(self.super_admin, 'views.py', code2)
        assert second.status_code == 201
        assert second.json()['is_version_upload'] is False
        doc2 = Document.objects.get(pk=second.json()['document_id'])
        doc1.refresh_from_db()
        assert doc1.is_active is True
        assert doc2.is_active is True

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/plan.pdf')
    def test_upload_binary_same_name_defaults_to_new_version(self, mock_save, mock_magic):
        """二进制文件无法即时读文 → 同名上传默认按新版本处理"""
        mock_magic.from_buffer.return_value = 'application/pdf'
        first = self._upload(
            self.super_admin, '方案.pdf', b'%PDF-1.4\nfake pdf body', mime='application/pdf')
        assert first.status_code == 201
        doc1 = Document.objects.get(pk=first.json()['document_id'])
        second = self._upload(
            self.super_admin, '方案.pdf', b'%PDF-1.4\nfake pdf body v2', mime='application/pdf')
        assert second.status_code == 201
        assert second.json()['is_version_upload'] is True
        doc1.refresh_from_db()
        assert doc1.is_active is False

    @pytest.mark.integration
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file', return_value='/tmp/contacts.txt')
    def test_upload_cross_team_same_content_isolated(self, mock_save, mock_magic):
        """不同团队上传同名同内容文档 → 互不影响，各自独立活跃

        版本组按 node+file_name+dept_id+team_id 四元组隔离，
        避免团队 B 的同名同内容上传把团队 A 的文档误置非活跃。
        """
        mock_magic.from_buffer.return_value = 'text/plain'
        team2, category2 = self._make_second_team()
        content = '全员通讯录：张三 13800000000 / 李四 13900000000'

        first = self._upload(self.super_admin, '全员通讯录.txt', content)
        assert first.status_code == 201
        doc_a = Document.objects.get(pk=first.json()['document_id'])
        assert doc_a.team_id == self.team.id

        second = self._upload(self.super_admin, '全员通讯录.txt', content, node_id=category2.id)
        assert second.status_code == 201
        doc_b = Document.objects.get(pk=second.json()['document_id'])
        assert doc_b.team_id == team2.id

        doc_a.refresh_from_db()
        assert doc_a.is_active is True
        assert doc_b.is_active is True
        assert doc_a.version == 1 and doc_b.version == 1


# ============================================================================
# 列表过滤 / 版本列表 / 活跃切换
# ============================================================================
class TestActiveVersionSwitch(KnowledgeViewsExtraBase):
    """列表活跃过滤、versions 版本列表、set_active 切换测试"""

    def _make_versions(self, file_name='年会名单.txt', active_id=None):
        """ORM 构造同组两个版本（v1 活跃 / v2 非活跃），返回 (doc1, doc2)"""
        doc1 = _create_document(
            self.category_node, self.super_admin, title='年会名单',
            file_name=file_name, team_id=self.team.id, dept_id=self.dept.id,
            version=1, version_tag='v1', is_active=True)
        doc2 = _create_document(
            self.category_node, self.super_admin, title='年会名单',
            file_name=file_name, team_id=self.team.id, dept_id=self.dept.id,
            version=2, version_tag='v2', is_active=False)
        return doc1, doc2

    def _list_ids(self, params=''):
        resp = self.client.get(
            f'/api/v1/knowledge/documents/?{params}', **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        return {d['id'] for d in _results(resp)}

    @pytest.mark.integration
    def test_list_default_only_active(self):
        """列表默认仅返回活跃版本"""
        doc1, doc2 = self._make_versions()
        ids = self._list_ids()
        assert doc1.id in ids
        assert doc2.id not in ids

    @pytest.mark.integration
    def test_list_version_all_returns_all(self):
        """?version=all 返回全部版本（含旧版本，用于回溯）"""
        doc1, doc2 = self._make_versions()
        ids = self._list_ids('version=all')
        assert doc1.id in ids
        assert doc2.id in ids

    @pytest.mark.integration
    def test_list_version_count_field(self):
        """列表序列化器 version_count = 同组版本总数"""
        doc1, doc2 = self._make_versions()
        resp = self.client.get(
            '/api/v1/knowledge/documents/?version=all', **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        row = next(d for d in _results(resp) if d['id'] == doc1.id)
        assert row['version_count'] == 2
        assert row['is_active'] is True

    @pytest.mark.integration
    def test_versions_action_returns_siblings(self):
        """versions 接口返回同组版本列表（含活跃标记与可切换标记）"""
        doc1, doc2 = self._make_versions()
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{doc1.id}/versions/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        docs = {d['id']: d for d in resp.json()['documents']}
        assert set(docs) == {doc1.id, doc2.id}
        assert docs[doc1.id]['is_active'] is True
        assert docs[doc2.id]['is_active'] is False
        # super_admin 视同 Owner，可执行切换
        assert docs[doc2.id]['is_owner'] is True

    @pytest.mark.integration
    def test_set_active_switches_to_old_version(self):
        """set_active 将非活跃旧版本切为活跃 → 原活跃版本同步失效"""
        doc1, doc2 = self._make_versions()
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{doc2.id}/set_active/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        doc1.refresh_from_db()
        doc2.refresh_from_db()
        assert doc1.is_active is False
        assert doc2.is_active is True
        # 列表默认视角切换到 doc2
        ids = self._list_ids()
        assert doc2.id in ids
        assert doc1.id not in ids

    @pytest.mark.integration
    def test_set_active_already_active_idempotent(self):
        """已是活跃版本 → 幂等返回成功"""
        doc1, doc2 = self._make_versions()
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{doc1.id}/set_active/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        doc1.refresh_from_db()
        doc2.refresh_from_db()
        assert doc1.is_active is True
        assert doc2.is_active is False

    @pytest.mark.integration
    def test_set_active_non_owner_403(self):
        """非 Owner / 非管理员切换他人文档 → 403

        用 PUBLIC 文档测试：普通用户可读（get_object 通过），
        但写权限校验（_require_write）应拦截为 403。
        """
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/set_active/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_set_active_deleted_400(self):
        """已删除文档不能设为活跃版本 → 400（需 include_deleted=true 才能取到已删记录）"""
        doc = _create_document(
            self.category_node, self.super_admin, title='已删文档',
            file_name='deleted.txt', team_id=self.team.id, dept_id=self.dept.id,
            version=1, version_tag='v1', is_deleted=True)
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{doc.id}/set_active/?include_deleted=true',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '已删除' in resp.json()['detail']
