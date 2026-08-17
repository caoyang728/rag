"""
腾讯云 OCR 服务

功能：
- 基于腾讯云「通用印刷体识别」系列接口（GeneralBasicOCR 标准 / GeneralAccurateOCR 高精 / GeneralEfficientOCR 精简）
  对图片进行文字识别，供图片文档与扫描件 PDF 提取文本。
- 凭证（SecretId / SecretKey / Region）属敏感信息，从 .env 经 settings 注入，不入库；
  接口选择与总开关走 SystemConfig（OCR_ENABLED / OCR_API），由运营在系统配置页管理。
- 全链路降级：SDK 未安装 / 凭证未配置 / 接口调用失败时返回空串并记录日志，
  绝不抛出异常阻断文档解析主流程（OCR 可丢、业务不可丢）。

使用示例：
    from apps.knowledge.ocr import ocr_image_file
    text = ocr_image_file('/tmp/page.png')   # OCR 未启用/未配置时返回 ''
"""
import base64
import io

from django.conf import settings
from loguru import logger

from apps.system.config_loader import get_config_value

# 腾讯云 OCR 接口域名与默认地域
TENCENT_OCR_ENDPOINT = 'ocr.tencentcloudapi.com'
DEFAULT_REGION = 'ap-guangzhou'

# 腾讯云接口要求：图片经 Base64 编码后不超过 7M
TENCENT_IMAGE_BASE64_LIMIT = 7 * 1024 * 1024
# 超限时压缩参数：长边上限与 JPEG 质量（体积与清晰度折中）
COMPRESS_MAX_SIDE = 3000
COMPRESS_QUALITY = 85

# 支持调用的腾讯云 OCR 接口（配置 OCR_API 时取此范围）
OCR_API_CHOICES = ('GeneralBasicOCR', 'GeneralAccurateOCR', 'GeneralEfficientOCR')
DEFAULT_OCR_API = 'GeneralBasicOCR'

# ============================================================================
# 开发期 OCR 调用次数限额（临时防护，生产环境删除本段代码）：
# 腾讯云 OCR 按调用量计费，开发/联调期间可能高频触发，
# 硬编码限制累计调用次数，达到上限后 OCR 自动停止，防止误耗云费用。
# ============================================================================
DEV_OCR_QUOTA = 800
# 计数器固定使用 knowledge_ocr_usage_counter 表主键为 1 的单行记录（见 OcrUsageCounter）
OCR_QUOTA_COUNTER_PK = 1


def _consume_ocr_quota() -> bool:
    """开发期 OCR 限额：原子占取一次调用额度，已达上限返回 False（调用方应停止识别）

    实现：OcrUsageCounter 单行计数器 + select_for_update 行锁，
    保证并发下「检查是否超限 → 自增计数」不丢失、不超额放行。
    首次调用自动建行（count=1）；累计达到 DEV_OCR_QUOTA 后不再放行。
    """
    from django.db import transaction
    from django.db.models import F

    from apps.knowledge.models import OcrUsageCounter
    with transaction.atomic():
        counter = OcrUsageCounter.objects.select_for_update().filter(pk=OCR_QUOTA_COUNTER_PK).first()
        if counter is None:
            # 首次调用：建行并计 1 次（第 1 次调用即消费 1 次额度）
            OcrUsageCounter.objects.create(pk=OCR_QUOTA_COUNTER_PK, count=1)
            return True
        if counter.count >= DEV_OCR_QUOTA:
            return False
        OcrUsageCounter.objects.filter(pk=OCR_QUOTA_COUNTER_PK).update(count=F('count') + 1)
        return True


def is_ocr_enabled() -> bool:
    """OCR 总开关（SystemConfig.OCR_ENABLED），未配置时默认关闭"""
    return bool(get_config_value('OCR_ENABLED', default=False, value_type='bool'))


def is_pdf_ocr_enabled() -> bool:
    """扫描件/低文本页 PDF 是否自动 OCR（总开关 + OCR_PDF_AUTO 双重控制）"""
    if not is_ocr_enabled():
        return False
    return bool(get_config_value('OCR_PDF_AUTO', default=True, value_type='bool'))


def pdf_ocr_page_limit() -> int:
    """单份 PDF 最大 OCR 页数（SystemConfig.OCR_PDF_PAGE_LIMIT，成本控制）

    配置缺失/非法 → 默认 50；配置为 0 → 至少保留 1 页，避免整份跳过。
    """
    value = get_config_value('OCR_PDF_PAGE_LIMIT', default=50, value_type='int')
    if value is None or value == '':
        return 50
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 50


def get_ocr_client():
    """构建腾讯云 OcrClient（延迟导入 SDK，凭证缺失/未安装时返回 None）

    每次调用重新创建而非缓存：OCR 调用频率低、客户端构造无网络开销，
    且避免测试打桩后残留全局状态影响后续调用。
    """
    secret_id = getattr(settings, 'TENCENT_OCR_SECRET_ID', '')
    secret_key = getattr(settings, 'TENCENT_OCR_SECRET_KEY', '')
    if not secret_id or not secret_key:
        logger.debug('[TencentOCR] 未配置 TENCENT_OCR_SECRET_ID/KEY，OCR 服务不可用')
        return None
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.ocr.v20181119 import ocr_client
    except ImportError:
        logger.warning('[TencentOCR] 未安装 tencentcloud-sdk-python-ocr，OCR 服务不可用')
        return None

    region = getattr(settings, 'TENCENT_OCR_REGION', '') or DEFAULT_REGION
    cred = credential.Credential(secret_id, secret_key)
    http_profile = HttpProfile()
    http_profile.endpoint = TENCENT_OCR_ENDPOINT
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    return ocr_client.OcrClient(cred, region, client_profile)


def ocr_image_file(file_path: str) -> str:
    """识别图片文件，返回识别文本；失败或不可用时返回空串"""
    try:
        with open(file_path, 'rb') as f:
            return ocr_image_bytes(f.read())
    except OSError as e:
        logger.error(f'[TencentOCR] 读取图片失败 {file_path}: {e}')
        return ''


def ocr_image_bytes(image_bytes: bytes) -> str:
    """识别图片字节，返回识别文本；失败或不可用时返回空串

    流程：总开关/凭证/接口任一不可用 → 直接返回空串；
    图片 Base64 超过 7M 时先用 Pillow 压缩再提交，避免被接口拒绝。
    """
    if not is_ocr_enabled():
        return ''
    client = get_ocr_client()
    if client is None:
        return ''

    api_name = _resolve_api_name()
    # 图片过大时压缩后重试一次，仍超限则放弃（不重复压缩死循环）
    image_bytes = _compress_if_needed(image_bytes)
    b64 = base64.b64encode(image_bytes).decode('ascii')
    if len(b64) > TENCENT_IMAGE_BASE64_LIMIT:
        logger.warning(f'[TencentOCR] 图片压缩后仍超过 7M 限制（{len(b64)}），跳过 OCR')
        return ''

    # 开发期调用次数限额（见 DEV_OCR_QUOTA 说明）：达到上限后停止识别，生产环境删除
    if not _consume_ocr_quota():
        logger.warning(f'[TencentOCR] 调用次数已达开发期上限（{DEV_OCR_QUOTA} 次），停止 OCR')
        return ''

    try:
        from tencentcloud.ocr.v20181119 import models
        req_class = getattr(models, api_name + 'Request')
        req = req_class()
        req.ImageBase64 = b64
        resp = getattr(client, api_name)(req)
        return _extract_text(resp)
    except Exception as e:
        logger.error(f'[TencentOCR] {api_name} 调用失败: {e}')
        return ''


def _resolve_api_name() -> str:
    """从 SystemConfig.OCR_API 解析接口名，非法值时回退默认接口"""
    api = get_config_value('OCR_API', default=DEFAULT_OCR_API, value_type='string')
    return api if api in OCR_API_CHOICES else DEFAULT_OCR_API


def _extract_text(resp) -> str:
    """从 OCR 响应中按行拼接 DetectedText"""
    detections = getattr(resp, 'TextDetections', None) or []
    lines = []
    for d in detections:
        text = getattr(d, 'DetectedText', '')
        if text:
            lines.append(text)
    return '\n'.join(lines)


def _compress_if_needed(image_bytes: bytes) -> bytes:
    """图片 Base64 可能超限时提前压缩（转 RGB + 限制长边 + JPEG 质量压缩）

    原始图片体积不大但长边极大的图（如超长截图），Base64 可能突破 7M 限制，
    统一先压缩以降低被接口拒绝的概率；压缩失败时原样返回，交由调用方兜底。
    """
    if len(base64.b64encode(image_bytes)) <= TENCENT_IMAGE_BASE64_LIMIT:
        return image_bytes
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert('RGB')
        if max(img.size) > COMPRESS_MAX_SIDE:
            img.thumbnail((COMPRESS_MAX_SIDE, COMPRESS_MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=COMPRESS_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f'[TencentOCR] 图片压缩失败: {e}')
        return image_bytes
