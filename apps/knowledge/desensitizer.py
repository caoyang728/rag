"""
数据脱敏 - 五重安全之一
入库前脱敏（手机号/身份证/邮箱/银行卡）
"""
import re
from typing import Tuple

# 手机号：11 位 1 开头
_PHONE = re.compile(r'(?<!\d)(1[3-9]\d{9})(?!\d)')
# 身份证：18 位末位可为 X
_ID_CARD = re.compile(r'(?<!\d)(\d{6}[12]\d{3}(0\d|1[012])([012]\d|3[01])\d{3}[\dXx])(?!\d)')
# 银行卡：13-19 位
_BANK = re.compile(r'(?<!\d)(\d{16,19})(?!\d)')
# 邮箱
_EMAIL = re.compile(r'([\w.+-]+)@([\w-]+\.[\w.-]+)')


def desensitize(text: str) -> Tuple[str, int]:
    """返回 (脱敏后文本, 命中数)"""
    if not text:
        return text, 0
    count = 0

    def _mask_phone(m):
        nonlocal count
        count += 1
        s = m.group(1)
        return s[:3] + '****' + s[-4:]

    def _mask_id(m):
        nonlocal count
        count += 1
        s = m.group(1)
        return s[:6] + '********' + s[-4:]

    def _mask_bank(m):
        nonlocal count
        count += 1
        s = m.group(1)
        return s[:4] + '*' * (len(s) - 8) + s[-4:]

    def _mask_email(m):
        nonlocal count
        count += 1
        u, d = m.group(1), m.group(2)
        return (u[:2] + '***' if len(u) > 2 else '***') + '@' + d

    text = _PHONE.sub(_mask_phone, text)
    text = _ID_CARD.sub(_mask_id, text)
    text = _BANK.sub(_mask_bank, text)
    text = _EMAIL.sub(_mask_email, text)
    return text, count
