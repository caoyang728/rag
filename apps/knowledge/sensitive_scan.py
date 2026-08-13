"""
文档审核敏感内容扫描

检测能力：
- 敏感词：复用 apps.security.SensitiveFilter（AC 自动机 + 配置正则 + 词库）。
  扫描时传 record=False，不累计 LLM 流式命中统计（SensitiveWord.hit_count），
  避免一次性扫描整篇文档污染敏感词全局命中数。
- 隐私模式：手机号 / 邮箱 / 身份证 / 银行卡 / IP 地址（IPv4），
  正则与 apps/knowledge/desensitizer.py 对齐（额外增加 IP 检测，脱敏器不覆盖 IP）。

虚拟数据过滤（仅审查辅助，避免示例数据刷屏）：
- IP：RFC 5737 文档保留段（TEST-NET-1/2/3）与回环地址 127.0.0.0/8
- 手机号：尾 8 位全相同 / 尾 8 位连续 / 经典示例号（如 13800138000）
- 邮箱：RFC 2606 保留示例域（example.com/net/org、*.example）
- 银行卡：全相同 / 连续 / 公开测试卡号
- 身份证：出生日期（中间 8 位）非法或超出近 200 年视为示例数据
- 敏感词库中的命中不过滤（显式配置的即视为真实）

输出形态：
- 统计：总命中数 + 分类型命中数
- 片段：按 (类型, 命中值) 去重聚合，保留首现上下文与出现次数，
  避免同一手机号出现几十次时在弹窗里刷屏；
  上下文按行截取（命中行 + 上下各 2 行）而非固定字符窗口，便于对照原文定位
"""
import re
from datetime import date
from typing import Dict, List, Tuple

from loguru import logger

# 分类 key -> 展示文案
CATEGORY_LABELS = {
    'sensitive_word': '敏感词',
    'phone': '手机号',
    'id_card': '身份证',
    'email': '邮箱',
    'bank_card': '银行卡',
    'ip': 'IP 地址',
}

# 内置隐私模式（与 desensitizer 同源，另增 IP）：
# - 手机号：11 位 1 开头
# - 邮箱：标准 user@domain 形态
# - 身份证：18 位末位可为 X
# - 银行卡：16-19 位连续数字
# - IPv4：每段 0-255 校验，避免 999.999.999.999 这类非法地址误报
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ('phone', re.compile(r'(?<!\d)(1[3-9]\d{9})(?!\d)')),
    ('email', re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')),
    ('id_card', re.compile(r'(?<!\d)(\d{6}[12]\d{3}(0\d|1[012])([012]\d|3[01])\d{3}[\dXx])(?!\d)')),
    ('bank_card', re.compile(r'(?<!\d)(\d{16,19})(?!\d)')),
    ('ip', re.compile(r'(?<!\d)((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\d)')),
]

# 片段上下文：命中行上下各保留的行数（按行截取，而非固定字符窗口）
_CONTEXT_LINES = 2
# 兜底上限：窗口总长超限时从远端逐行丢弃；仅剩单行仍超长时按字符截取，
# 防止无换行的巨长行（如粘贴的整段文字）撑爆弹窗
_MAX_CONTEXT_CHARS = 500
# 片段最大返回条数（按 (类型, 命中值) 去重后的组数上限，超出截断并标记）
MAX_FRAGMENTS = 30


# ============================================================================
# 虚拟 / 示例数据识别（仅过滤内置隐私模式命中，不影响敏感词库与身份证）
# ============================================================================

# 公开测试卡号（支付行业文档广泛使用，误伤真实卡号风险极低）
_TEST_CARD_NUMBERS = {
    '4111111111111111', '4242424242424242', '4444333322221111',
    '5555555555554444', '6011111111111117',
    '6217003810012345678', '6222020200112345',
}
# RFC 5737 文档保留段：TEST-NET-1/2/3（前三个八位组）
_RFC5737_RANGES = ((192, 0, 2), (198, 51, 100), (203, 0, 113))
# RFC 2606 保留示例域
_RFC2606_EXAMPLE_DOMAINS = ('example.com', 'example.net', 'example.org')


def _is_virtual_ip(ip: str) -> bool:
    """是否文档/示例保留 IP：RFC 5737 TEST-NET 段或回环地址 127.0.0.0/8"""
    p = ip.split('.')
    if int(p[0]) == 127:
        return True
    return (int(p[0]), int(p[1]), int(p[2])) in _RFC5737_RANGES


def _is_virtual_phone(phone: str) -> bool:
    """是否明显测试/示例手机号：
    - 尾 8 位全相同（13800000000 / 13888888888）
    - 尾 8 位连续递增或递减（13812345678 / 13898765432）
    - 经典示例号（13800138000）
    """
    if phone in ('13800138000',):
        return True
    if len(set(phone[3:])) == 1:
        return True
    digits = [int(d) for d in phone[3:]]
    if len(digits) == 8:
        if digits == list(range(digits[0], digits[0] + 8)):
            return True
        if digits == list(range(digits[0], digits[0] - 8, -1)):
            return True
    return False


def _is_virtual_email(email: str) -> bool:
    """是否示例邮箱：域名落在 RFC 2606 保留示例域"""
    domain = email.rsplit('@', 1)[-1].lower()
    if domain in _RFC2606_EXAMPLE_DOMAINS or domain.endswith('.example'):
        return True
    return False


def _is_virtual_bank_card(number: str) -> bool:
    """是否明显测试/示例银行卡：全相同数字、连续（含模 10 循环）或公开测试卡号

    使用模 10 循环判断，可覆盖 1234567890123456 / 9876543210987654 这类
    经典测试卡；真实卡号需通过 Luhn 校验，不可能整段连续，误伤风险极低。
    """
    if number in _TEST_CARD_NUMBERS:
        return True
    if len(set(number)) == 1:
        return True
    digits = [int(d) for d in number]
    if all(b == (a + 1) % 10 for a, b in zip(digits, digits[1:])):
        return True
    if all(b == (a - 1) % 10 for a, b in zip(digits, digits[1:])):
        return True
    return False


def _is_virtual_id_card(number: str) -> bool:
    """身份证示例数据判定：出生日期（中间 8 位 YYYYMMDD）非法或超出近 200 年

    真实身份证的出生日期必须是合法日历日期（含大小月/闰年），且距今不超过 200 年；
    18000101、11111111 这类明显示例数据据此过滤。
    月份 13 等非法形态已在正则层排除（month 仅 01-12），此处 datetime 兜底
    覆盖 2 月 30 日、非闰年 2 月 29 日等正则放行但实际不存在的日期。
    """
    y = int(number[6:10])
    m = int(number[10:12])
    d = int(number[12:14])
    if not (date.today().year - 200 <= y <= date.today().year):
        return True
    try:
        date(y, m, d)
    except ValueError:
        return True
    return False


# 类型 -> 虚拟数据判定函数（敏感词库命中不做过滤，显式配置的即视为真实）
_VIRTUAL_CHECKERS = {
    'phone': _is_virtual_phone,
    'email': _is_virtual_email,
    'bank_card': _is_virtual_bank_card,
    'id_card': _is_virtual_id_card,
    'ip': _is_virtual_ip,
}


def scan_text(text: str) -> List[dict]:
    """扫描文本，返回命中列表

    Returns:
        [{'category', 'label', 'matched', 'start', 'end'}, ...]
        start/end 为命中区间（用于截取上下文），matched 为实际命中的文本。
    """
    hits: List[dict] = []
    if not text:
        return hits

    # 1. 敏感词库（AC 自动机 + 配置正则）；词库加载失败不阻断扫描（内置隐私模式仍可用）
    try:
        from apps.security.sensitive_filter import get_sensitive_filter
        sf = get_sensitive_filter()
        for h in sf.check(text, record=False):
            # 命中区间取实际匹配文本：普通词 h.word 即原文；正则词 h.word 是正则模式串
            matched = text[h.start:h.end] or h.word
            hits.append({
                'category': h.category or 'other',
                'label': CATEGORY_LABELS.get(h.category or 'other', '敏感词'),
                'matched': matched,
                'start': h.start,
                'end': h.end,
            })
    except Exception:
        logger.exception('[SensitiveScan] sensitive word filter failed')

    # 2. 内置隐私模式；显式的测试/示例数据（RFC 保留段、测试号等）跳过，避免噪音
    for category, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            matched = m.group()
            checker = _VIRTUAL_CHECKERS.get(category)
            if checker and checker(matched):
                continue
            hits.append({
                'category': category,
                'label': CATEGORY_LABELS[category],
                'matched': matched,
                'start': m.start(),
                'end': m.end(),
            })

    return hits


def _line_window(text: str, start: int, end: int) -> Tuple[str, str]:
    """按行截取命中上下文：命中行 + 上下各 _CONTEXT_LINES 行

    以换行符为边界保留整行（含命中行本身），上下文按行而非字符窗口截取，
    便于审核人看到敏感数据所在段落的完整上下文。
    窗口总长超 _MAX_CONTEXT_CHARS 时从远端逐行丢弃（保住靠近命中行的行）；
    仅剩单行仍超长（如无换行的巨长行）时按字符截取兜底，防止撑爆弹窗。

    Returns:
        (before, after)，均保留原文换行/空格，前端 white-space: pre-wrap 原样展示。
    """
    before = text[:start]
    after = text[end:]

    before_lines = before.split('\n')
    if len(before_lines) > _CONTEXT_LINES + 1:
        before_lines = before_lines[-(_CONTEXT_LINES + 1):]
    while len(before_lines) > 1 and len('\n'.join(before_lines)) > _MAX_CONTEXT_CHARS:
        before_lines.pop(0)
    before = '\n'.join(before_lines)
    if len(before) > _MAX_CONTEXT_CHARS:
        before = before[-_MAX_CONTEXT_CHARS:]

    after_lines = after.split('\n')
    if len(after_lines) > _CONTEXT_LINES + 1:
        after_lines = after_lines[:_CONTEXT_LINES + 1]
    while len(after_lines) > 1 and len('\n'.join(after_lines)) > _MAX_CONTEXT_CHARS:
        after_lines.pop()
    after = '\n'.join(after_lines)
    if len(after) > _MAX_CONTEXT_CHARS:
        after = after[:_MAX_CONTEXT_CHARS]

    return before, after


def build_scan_response(text: str, source: str) -> dict:
    """聚合扫描结果为接口响应：统计 + 去重片段

    片段按 (category, matched) 去重聚合，同值只保留首现上下文并累计次数，
    组数超 MAX_FRAGMENTS 截断并以 truncated 标记（前端提示只展示前 N 条）。
    """
    hits = scan_text(text)
    total = len(hits)

    # 分类型计数，按命中数降序
    cat_counts: Dict[str, int] = {}
    for h in hits:
        cat_counts[h['category']] = cat_counts.get(h['category'], 0) + 1
    categories = [
        {'key': k, 'label': CATEGORY_LABELS.get(k, k), 'count': n}
        for k, n in sorted(cat_counts.items(), key=lambda x: -x[1])
    ]

    # 片段去重聚合：保留首现上下文与命中区间
    grouped: Dict[Tuple[str, str], dict] = {}
    for h in hits:
        key = (h['category'], h['matched'])
        g = grouped.get(key)
        if g is None:
            start, end = h['start'], h['end']
            # 上下文按行截取（命中行 + 上下各 _CONTEXT_LINES 行），
            # 保留原文换行/空格（前端 pre-wrap 原样展示），便于审核人对照原文定位
            before, after = _line_window(text, start, end)
            grouped[key] = {
                'category': h['category'],
                'label': h['label'],
                'matched': h['matched'],
                'context_before': before,
                'context_after': after,
                'count': 1,
            }
        else:
            g['count'] += 1

    # 按出现次数降序展示，超出上限截断
    fragments = sorted(grouped.values(), key=lambda x: -x['count'])[:MAX_FRAGMENTS]
    truncated = len(grouped) > MAX_FRAGMENTS

    return {
        'ok': True,
        'source': source,
        'scanned_chars': len(text),
        'total': total,
        'categories': categories,
        'fragments': fragments,
        'truncated': truncated,
    }
