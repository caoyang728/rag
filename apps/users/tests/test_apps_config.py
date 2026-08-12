"""
apps.users.apps 覆盖率补充 —— ready() 中 GenericIPAddressField monkey-patch 分支

覆盖缺失行 30-33（_patched_get_prep_value 的 try/except/pass）：
- 非完整 IPv6（冒号数 != 7）→ clean_ipv6_address 抛 ValidationError → except/pass 原样返回
- 完整 IPv6 → 走 try/return 正常压缩路径
- None / 纯 IPv4 字符串 → 冒号判断前短路
"""
from django.db.models import GenericIPAddressField


class TestAppsConfigIPv6Patch:
    """GenericIPAddressField.get_prep_value monkey-patch 分支测试"""

    def test_get_prep_value_none(self):
        """None 直接返回（"冒号判断" 前短路）"""
        assert GenericIPAddressField().get_prep_value(None) is None

    def test_get_prep_value_invalid_ipv6_falls_back(self):
        """非完整 IPv6 → clean_ipv6_address 抛异常 → 走 except 分支原样返回（覆盖 30-33 的异常路径）"""
        assert GenericIPAddressField().get_prep_value('1:2:3') == '1:2:3'

    def test_get_prep_value_valid_ipv6_compressed(self):
        """完整 IPv6 → 正常返回压缩形式（覆盖 try/return 路径）"""
        val = GenericIPAddressField().get_prep_value('2001:0db8:85a3:0000:0000:8a2e:0370:7334')
        assert val == '2001:db8:85a3::8a2e:370:7334'

    def test_get_prep_value_plain_ipv4(self):
        """纯 IPv4 字符串不含冒号 → 原样返回"""
        assert GenericIPAddressField().get_prep_value('192.168.1.1') == '192.168.1.1'
