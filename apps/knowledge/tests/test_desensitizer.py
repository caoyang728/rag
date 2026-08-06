"""
apps.knowledge.desensitizer 单元测试 —— 数据脱敏（五重安全之一）

覆盖范围：
- 空值与 None 输入的兜底返回
- 手机号 / 身份证 / 银行卡 / 邮箱 四类敏感信息的脱敏格式与命中计数
- 边界场景：非手机号前缀、相邻号码的数字边界保护、上下文中的敏感信息
- 多类型混合文本的累计命中数

用纯 pytest（不依赖 DB）：
desensitize 是无状态纯函数（仅依赖 re），不涉及 ORM/缓存/外部服务，
用 pytest 函数式断言即可覆盖，无需 DB 与事务隔离。
"""
import pytest

from apps.knowledge.desensitizer import desensitize


# ============================================================================
# 空值与 None 兜底
# ============================================================================
@pytest.mark.unit
def test_empty_text():
    """空字符串不应触发任何脱敏，命中数为 0"""
    assert desensitize('') == ('', 0)


@pytest.mark.unit
def test_none_text():
    """None 输入原样返回（falsy 短路），命中数为 0

    None 输入：解析器在文档无文本时可能传入 None，
    脱敏作为入库前最后一道管线需对 None 容错，避免阻断主流程。
    """
    assert desensitize(None) == (None, 0)


# ============================================================================
# 手机号脱敏
# ============================================================================
@pytest.mark.unit
def test_phone_mask():
    """标准 11 位手机号：保留前 3 后 4，中间用 **** 替换"""
    text, count = desensitize('13812345678')
    assert text == '138****5678'
    assert count == 1


@pytest.mark.unit
def test_phone_no_match():
    """非 1[3-9] 开头的 11 位数字不应被识别为手机号

    手机号正则要求第二位为 3-9，'238...' 第二位为 3 但首位非 1，
    故不匹配；同时该串仅 11 位不满足银行卡 16-19 位要求，整体命中数为 0。
    """
    text, count = desensitize('23812345678')
    assert text == '23812345678'
    assert count == 0


@pytest.mark.unit
def test_phone_in_context():
    """手机号嵌入中文上下文中应被正确识别并脱敏（数字边界由非数字字符保证）"""
    text, count = desensitize('电话是13812345678联系')
    assert '138****5678' in text
    assert count == 1


@pytest.mark.unit
def test_adjacent_phones():
    r"""两个手机号相邻（无分隔符）应整体不匹配

    手机号正则带 (?<!\d) 与 (?!\d) 边界断言，
    22 位连续数字中任意 11 位子串两侧均为数字，边界断言失败，
    从而避免把长数字串误切成两个手机号。
    """
    text, count = desensitize('1381234567813812345678')
    assert text == '1381234567813812345678'
    assert count == 0


# ============================================================================
# 身份证脱敏
# ============================================================================
@pytest.mark.unit
def test_id_card_mask():
    """18 位身份证：保留前 6 后 4，中间用 8 个 * 替换"""
    text, count = desensitize('110101199001011234')
    assert text == '110101********1234'
    assert count == 1


@pytest.mark.unit
def test_id_card_with_x():
    r"""身份证末位为 X/x 也应匹配（正则末位 [\dXx]）"""
    text, count = desensitize('11010119900101123X')
    assert text == '110101********123X'
    assert count == 1


# ============================================================================
# 银行卡脱敏
# ============================================================================
@pytest.mark.unit
def test_bank_card_mask():
    r"""19 位银行卡：保留前 4 后 4，中间星号数 = len - 8

    该串第 11-12 位 '56' 不满足身份证月份 (0\d|1[012])，
    身份证正则先行但匹配失败，随后银行卡正则命中整串。
    """
    text, count = desensitize('6222021234567890123')
    assert text == '6222***********0123'
    assert count == 1


# ============================================================================
# 邮箱脱敏
# ============================================================================
@pytest.mark.unit
def test_email_mask():
    """邮箱用户名长度 > 2：保留前 2 位 + *** + @域名"""
    text, count = desensitize('user@example.com')
    assert text == 'us***@example.com'
    assert count == 1


@pytest.mark.unit
def test_email_short_prefix():
    """邮箱用户名长度 <= 2：用户名整体替换为 ***，避免泄露单字符前缀"""
    text, count = desensitize('a@b.com')
    assert text == '***@b.com'
    assert count == 1


# ============================================================================
# 混合与无敏感信息
# ============================================================================
@pytest.mark.unit
def test_multiple_types():
    r"""同一文本中包含手机号 + 邮箱 + 身份证，命中数应为 3

    用「，」分隔各类敏感信息：Python3 默认 \w 匹配 Unicode 字符（含中文），
    邮箱用户名正则 [\w.+-]+ 会贪婪吞掉相邻中文与数字。用非 \w 的中文逗号隔断，
    保证三类敏感信息各自独立命中且脱敏结果可断言。
    顺序 phone→id→bank→email：先命中的规则替换为带 * 的串，
    后续银行卡正则因 * 断开数字串不会重复命中。
    """
    text, count = desensitize('电话13812345678，user@example.com，身份证110101199001011234')
    assert count == 3
    assert '138****5678' in text
    assert 'us***@example.com' in text
    assert '110101********1234' in text


@pytest.mark.unit
def test_no_sensitive():
    """普通中文文本不应命中任何脱敏规则"""
    text, count = desensitize('普通文本内容')
    assert text == '普通文本内容'
    assert count == 0
