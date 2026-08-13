"""
apps.knowledge 文档敏感内容扫描测试 —— scan_text / build_scan_response / DocSensitiveScanView

覆盖范围：
- scan_text：内置隐私模式（手机号/邮箱/IP/身份证/银行卡）检测 + 敏感词命中取实际文本
- 虚拟/示例数据过滤：RFC 5737 IP、测试手机号、RFC 2606 示例邮箱、测试卡号、
  非法/超年限身份证出生日期
- build_scan_response：分类型统计 + 同值片段去重聚合 + 超上限截断 + 零命中
- DocSensitiveScanView：权限（页面权限/归属范围）、文档不存在、原始文件扫描、
  切片降级、无内容空结果、读取异常降级

测试策略：
- 敏感词库（SensitiveFilter）为进程级单例，跨测试会残留状态，
  一律 patch apps.security.sensitive_filter.get_sensitive_filter 提供确定命中；
- 内置隐私模式为纯正则，不依赖 DB，直接断言。
"""
from unittest.mock import patch

import pytest

from apps.security.sensitive_filter import HitResult
from apps.knowledge.models import DocumentChunk
from apps.knowledge.sensitive_scan import build_scan_response, scan_text

from apps.knowledge.tests.test_views import (
    _auth_headers, _create_document, KnowledgeViewsExtraBase,
)
from apps.users.models import Team


# ============================================================================
# scan_text —— 内置隐私模式 + 敏感词命中 + 虚拟数据过滤
# ============================================================================
class TestSensitiveScanText:
    """scan_text 检测能力测试"""

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_detects_phone_email_ip(self, mock_get):
        """手机号/邮箱/IP 一次扫描全部命中"""
        mock_get.return_value.check.return_value = []
        text = '联系人：13812341234，邮箱 zhang.san@corp.example.cn，服务器 10.10.0.5'
        hits = scan_text(text)
        cats = {h['category'] for h in hits}
        assert cats == {'phone', 'email', 'ip'}
        phone = next(h for h in hits if h['category'] == 'phone')
        assert phone['matched'] == '13812341234'

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_ignores_invalid_ip(self, mock_get):
        """非法 IP（999.999.999.999）不误报，正常 IPv4 命中"""
        mock_get.return_value.check.return_value = []
        text = '非法地址 999.999.999.999 与正常地址 192.168.1.1'
        hits = scan_text(text)
        ip_hits = [h for h in hits if h['category'] == 'ip']
        assert len(ip_hits) == 1
        assert ip_hits[0]['matched'] == '192.168.1.1'

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_sensitive_word_hit_uses_text_span(self, mock_get):
        """敏感词命中时 matched 取实际匹配文本（正则词 h.word 是模式串，不可直接展示）"""
        text = '会议纪要：内部机密内容'
        mock_get.return_value.check.return_value = [
            HitResult(word='内部机密', category='secret', action='warn',
                      start=5, end=9),
        ]
        hits = scan_text(text)
        sw = [h for h in hits if h['category'] == 'secret']
        assert len(sw) == 1
        assert sw[0]['matched'] == '内部机密'
        assert sw[0]['label'] == '敏感词'

    @pytest.mark.unit
    def test_empty_text_returns_empty(self):
        """空文本返回空列表（不触发词库加载）"""
        assert scan_text('') == []

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_filters_virtual_data(self, mock_get):
        """测试/示例数据被过滤，真实数据保留"""
        mock_get.return_value.check.return_value = []
        text = (
            '测试IP 192.0.2.1、198.51.100.8、203.0.113.9、127.0.0.1 忽略，内网 10.10.0.5 保留；'
            '测试号 13800000000、13888888888、13812345678、13898765432、13800138000 忽略，'
            '13812341234 保留；'
            '示例邮箱 test@example.com、demo@sub.example 忽略，zhang@corp.example.cn 保留；'
            '测试卡 4111111111111111、8888888888888888、1234567890123456、9876543210987654、'
            '6217003810012345678 忽略，6228480402564890018 保留'
        )
        hits = scan_text(text)
        phones = [h['matched'] for h in hits if h['category'] == 'phone']
        emails = [h['matched'] for h in hits if h['category'] == 'email']
        ips = [h['matched'] for h in hits if h['category'] == 'ip']
        cards = [h['matched'] for h in hits if h['category'] == 'bank_card']
        assert phones == ['13812341234']
        assert emails == ['zhang@corp.example.cn']
        assert ips == ['10.10.0.5']
        assert cards == ['6228480402564890018']

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_id_card_valid_date_not_filtered(self, mock_get):
        """出生日期合法的身份证（含闰年 2 月 29 日）不做虚拟过滤"""
        mock_get.return_value.check.return_value = []
        text = '身份证号 11010519491231002X 归档；闰年出生 110105202402290011'
        hits = scan_text(text)
        id_hits = [h['matched'] for h in hits if h['category'] == 'id_card']
        assert id_hits == ['11010519491231002X', '110105202402290011']

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_virtual_id_card_filtered(self, mock_get):
        """出生日期非法或超出近 200 年的身份证视为示例数据被过滤

        - 11010518000101001X：年份 1800，超出近 200 年
        - 110105111111110011：年份 1111，明显示例
        - 110105202602300011：2 月 30 日，日历不存在（正则放行、datetime 兜底）
        - 110105202402290011：闰年 2 月 29 日合法，保留
        - 110105299901010011：未来年份，视为示例
        """
        mock_get.return_value.check.return_value = []
        text = ('过旧 11010518000101001X、公元1111年 110105111111110011、'
                '无效日期 110105202602300011、未来年份 110105299901010011 忽略；'
                '正常 11010519491231002X、闰年 110105202402290011 保留')
        hits = scan_text(text)
        ids = [h['matched'] for h in hits if h['category'] == 'id_card']
        assert ids == ['11010519491231002X', '110105202402290011']

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_sensitive_word_virtual_value_not_filtered(self, mock_get):
        """敏感词库显式配置的命中不被虚拟过滤（即使数值形态像测试号）"""
        text = '示例手机号 13800000000 属于演示'
        mock_get.return_value.check.return_value = [
            HitResult(word='13800000000', category='phone', action='warn',
                      start=6, end=17),
        ]
        hits = scan_text(text)
        # 内置隐私模式命中 13800000000 被虚拟过滤，但敏感词命中保留
        phones = [h['matched'] for h in hits if h['category'] == 'phone']
        assert phones == ['13800000000']

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter',
           side_effect=RuntimeError('sensitive filter down'))
    def test_filter_failure_degrades_to_patterns(self, mock_get):
        """词库加载异常不阻断扫描，内置隐私模式仍可用"""
        hits = scan_text('电话 13812341234')
        assert len(hits) == 1
        assert hits[0]['category'] == 'phone'


# ============================================================================
# build_scan_response —— 统计 + 去重聚合 + 截断 + 零命中
# ============================================================================
class TestSensitiveScanResponse:
    """build_scan_response 聚合逻辑测试"""

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_aggregates_duplicate_values(self, mock_get):
        """同一手机号出现多次 → 统计计数、片段只保留一条并累计次数"""
        text = '联系人：13812341234；备用：13812341234；座机：13812341234'
        mock_get.return_value.check.return_value = []
        resp = build_scan_response(text, 'raw')
        assert resp['source'] == 'raw'
        assert resp['total'] == 3
        phone_cat = next(c for c in resp['categories'] if c['key'] == 'phone')
        assert phone_cat['count'] == 3
        phone_frags = [f for f in resp['fragments'] if f['category'] == 'phone']
        assert len(phone_frags) == 1
        assert phone_frags[0]['count'] == 3
        # 上下文与命中值拼接后应还原出原始文本片段
        assert '13812341234' in phone_frags[0]['context_before'] + \
            phone_frags[0]['matched'] + phone_frags[0]['context_after']

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_fragments_truncated_over_limit(self, mock_get):
        """去重组数超 MAX_FRAGMENTS 时截断并标记 truncated"""
        mock_get.return_value.check.return_value = []
        # 31 个互不相同的非虚拟手机号 → 31 个去重组，超出 30 上限
        text = '\n'.join(f'用户{i + 1}电话 138{i + 1:08d}' for i in range(31))
        resp = build_scan_response(text, 'raw')
        assert resp['truncated'] is True
        assert len(resp['fragments']) == 30

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_empty_hits_response(self, mock_get):
        """零命中 → 空统计与空片段，不报错"""
        mock_get.return_value.check.return_value = []
        resp = build_scan_response('纯文本内容，无任何敏感信息', 'raw')
        assert resp['total'] == 0
        assert resp['categories'] == []
        assert resp['fragments'] == []
        assert resp['truncated'] is False

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_context_preserves_original_format(self, mock_get):
        """片段上下文保留原文换行与空格（仅展示还原，不影响检测策略）"""
        mock_get.return_value.check.return_value = []
        text = '第一行\n\n   联系人 13812341234 第二行'
        resp = build_scan_response(text, 'raw')
        frag = resp['fragments'][0]
        # 上下文还原出原文片段：换行与多空格原样保留，命中值前后与原文一致
        assert frag['context_before'] == '第一行\n\n   联系人 '
        assert frag['context_after'] == ' 第二行'
        assert frag['context_before'] + frag['matched'] + frag['context_after'] == text

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_context_by_lines(self, mock_get):
        """上下文按行截取：命中行 + 上下各 2 行（而非固定字符窗口）"""
        mock_get.return_value.check.return_value = []
        lines = [f'第{i}行' for i in range(1, 5)]
        lines.append('第5行 电话13812341234 结束')
        lines += [f'第{i}行' for i in range(6, 10)]
        resp = build_scan_response('\n'.join(lines), 'raw')
        frag = resp['fragments'][0]
        # 上面 2 行 + 命中行前缀 / 命中行后缀 + 下面 2 行，换行原样保留
        assert frag['context_before'] == '第3行\n第4行\n第5行 电话'
        assert frag['context_after'] == ' 结束\n第6行\n第7行'

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_context_near_doc_edge_keeps_all(self, mock_get):
        """命中靠近文首/文末时，可用行不足则保留全部（不越界）"""
        mock_get.return_value.check.return_value = []
        text = '第二行 电话13812341234 第三行\n第四行\n第五行'
        resp = build_scan_response(text, 'raw')
        frag = resp['fragments'][0]
        assert frag['context_before'] == '第二行 电话'
        assert frag['context_after'] == ' 第三行\n第四行\n第五行'

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_context_cap_drops_far_lines(self, mock_get):
        """窗口总长超上限时从远端逐行丢弃，保住靠近命中行的行"""
        mock_get.return_value.check.return_value = []
        line_a = 'A' * 300
        line_b = 'B' * 300
        line_hit = 'C' * 200 + '13812341234' + 'D' * 200
        text = line_a + '\n' + line_b + '\n' + line_hit + '\n' + 'E' * 300 + '\n' + 'F' * 300
        resp = build_scan_response(text, 'raw')
        frag = resp['fragments'][0]
        # 3 行窗口 800+ 字符超 500：先后丢弃 A 行、B 行，最终保留命中行前缀 C*200
        assert frag['context_before'] == 'C' * 200
        assert frag['context_after'] == 'D' * 200

    @pytest.mark.unit
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_context_single_line_capped(self, mock_get):
        """无换行的巨长行按字符上限兜底截取，防止撑爆弹窗"""
        mock_get.return_value.check.return_value = []
        text = '甲' * 600 + '13812341234' + '乙' * 600
        resp = build_scan_response(text, 'raw')
        frag = resp['fragments'][0]
        assert frag['context_before'] == '甲' * 500
        assert frag['context_after'] == '乙' * 500


# ============================================================================
# DocSensitiveScanView —— 接口测试
# ============================================================================
class TestDocSensitiveScan(KnowledgeViewsExtraBase):
    """DocSensitiveScanView 权限 / 扫描 / 降级测试"""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path):
        self._init_env()
        self.tmp_path = tmp_path

    def _make_doc(self, content, title='扫描文档', file_path=None, **extra):
        """创建 txt 文档；默认写入真实临时文件，file_path 可覆盖（如指向不存在的文件）"""
        if file_path is None:
            p = self.tmp_path / f'{title}.txt'
            p.write_text(content, encoding='utf-8')
            file_path = str(p)
        return _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title=title, file_name=f'{title}.txt',
            file_path=file_path, file_type='txt', **extra)

    @pytest.mark.integration
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_super_admin_scan_detects_sensitive(self, mock_get):
        """超管扫描含手机号/邮箱的文档 → 检出并返回统计与片段"""
        mock_get.return_value.check.return_value = []
        doc = self._make_doc('联系人：13812341234，邮箱 zhang.san@corp.example.cn')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{doc.id}/sensitive-scan/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['source'] == 'raw'
        assert data['total'] == 2
        keys = {c['key'] for c in data['categories']}
        assert keys == {'phone', 'email'}
        assert data['fragments'][0]['matched'] == '13812341234'

    @pytest.mark.integration
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_team_leader_can_scan_own_team_doc(self, mock_get):
        """团队组长可扫描本团队文档，无敏感内容 → total 0"""
        mock_get.return_value.check.return_value = []
        doc = self._make_doc('本团队文档，无敏感内容')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{doc.id}/sensitive-scan/',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 200
        assert resp.json()['total'] == 0

    @pytest.mark.integration
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_out_of_scope_team_leader_denied_403(self, mock_get):
        """他人团队的文档 → 403（归属范围校验）"""
        mock_get.return_value.check.return_value = []
        other_team = Team.objects.create(
            name='前端组', code='rd-frontend', department=self.dept,
            leader=self.other_user)
        other_doc = _create_document(
            self.category_node, self.normal_user, team_id=other_team.id,
            dept_id=self.dept.id, title='他人团队文档', file_name='other_team.txt',
            audit_status='pending_team')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{other_doc.id}/sensitive-scan/',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 403

    @pytest.mark.integration
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_normal_user_denied_403(self, mock_get):
        """无审核页面权限的普通用户 → 403"""
        mock_get.return_value.check.return_value = []
        doc = self._make_doc('普通内容')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{doc.id}/sensitive-scan/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_missing_doc_404(self):
        """文档不存在 → 404"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/999999/sensitive-scan/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404

    @pytest.mark.integration
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_file_missing_falls_back_to_chunks(self, mock_get):
        """原始文件缺失时降级扫描已入库切片（source='chunks'）"""
        doc = self._make_doc('正文将被忽略', file_path=str(self.tmp_path / 'not_exists.txt'))
        chunk_text = '会议纪要：内部机密内容'
        DocumentChunk.objects.create(
            document=doc, chunk_index=0, chunk_type='text', content=chunk_text)
        # 敏感词命中区间落在切片文本内
        mock_get.return_value.check.return_value = [
            HitResult(word='内部机密', category='secret', action='warn',
                      start=5, end=9),
        ]
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{doc.id}/sensitive-scan/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['source'] == 'chunks'
        assert data['total'] == 1
        assert data['fragments'][0]['matched'] == '内部机密'

    @pytest.mark.integration
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_no_content_returns_empty_result(self, mock_get):
        """文档无文件且无切片 → 返回空结果（source='none'），不报错"""
        mock_get.return_value.check.return_value = []
        doc = self._make_doc('', file_path=str(self.tmp_path / 'missing.txt'))
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{doc.id}/sensitive-scan/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['source'] == 'none'
        assert data['total'] == 0
        assert data['categories'] == []

    @pytest.mark.integration
    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_read_error_falls_back_to_chunks(self, mock_get):
        """原始文件读取抛非 Http404 异常 → 降级扫描切片（不 500）"""
        doc = self._make_doc('正文将被忽略')
        DocumentChunk.objects.create(
            document=doc, chunk_index=0, chunk_type='text',
            content='内部机密内容')
        mock_get.return_value.check.return_value = [
            HitResult(word='内部机密', category='secret', action='warn',
                      start=0, end=4),
        ]
        with patch('apps.knowledge.views._read_doc_bytes',
                   side_effect=RuntimeError('io error')):
            resp = self.client.get(
                f'/api/v1/knowledge/documents/{doc.id}/sensitive-scan/',
                **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['source'] == 'chunks'
        assert data['total'] == 1
