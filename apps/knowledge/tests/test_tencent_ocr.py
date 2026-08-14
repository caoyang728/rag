"""
apps.knowledge.ocr.tencent_ocr 单元测试 —— 腾讯云 OCR 服务

覆盖范围：
- 开关读取（OCR_ENABLED / OCR_PDF_AUTO / OCR_PDF_PAGE_LIMIT）
- 客户端构建（凭证缺失降级 / 正常构建）
- OCR 调用（禁用跳过 / 无客户端跳过 / 正常识别并拼接 / 接口异常降级返回空串 / 开发期配额超限停止）
- 开发期调用次数限额（OcrUsageCounter 单行计数，DB 集成测试）
- 接口名解析（非法配置回退默认）、超限图片压缩、响应文本提取

纯 pytest + mock（无 DB / 网络）：
- 开关读取通过 patch get_config_value 注入
- 腾讯云 SDK 通过 patch.dict(sys.modules) 注入假模块，避免真实网络调用
- 配额相关用例（TestOcrQuota）需 DB，单独用 @pytest.mark.django_db 标记
"""
import base64
import io
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings

from apps.knowledge.ocr import tencent_ocr


# ============================================================================
# 测试辅助：注入假腾讯云 SDK 模块
# ============================================================================
def _fake_tencent_sdk():
    """向 sys.modules 注入假 tencentcloud 模块树，使 SDK 导入成功但不会发起网络请求"""
    fake = {}
    for name in (
        'tencentcloud',
        'tencentcloud.common',
        'tencentcloud.common.credential',
        'tencentcloud.common.profile',
        'tencentcloud.common.profile.http_profile',
        'tencentcloud.common.profile.client_profile',
        'tencentcloud.ocr',
        'tencentcloud.ocr.v20181119',
        'tencentcloud.ocr.v20181119.ocr_client',
        'tencentcloud.ocr.v20181119.models',
    ):
        fake[name] = types.ModuleType(name)

    fake['tencentcloud.common.credential'].Credential = MagicMock(return_value='cred')
    fake['tencentcloud.common.profile.http_profile'].HttpProfile = MagicMock()
    fake['tencentcloud.common.profile.client_profile'].ClientProfile = MagicMock()
    fake['tencentcloud.ocr.v20181119.ocr_client'].OcrClient = MagicMock(return_value='client')
    fake['tencentcloud.ocr.v20181119.models'].GeneralBasicOCRRequest = type('Req', (), {})
    fake['tencentcloud.ocr.v20181119.models'].GeneralAccurateOCRRequest = type('Req2', (), {})
    return patch.dict(sys.modules, fake)


# ============================================================================
# 开关读取
# ============================================================================
class TestOcrSwitch:
    def test_is_ocr_enabled_when_config_true_then_true(self):
        """OCR_ENABLED 配置为真 → 总开关开启"""
        with patch.object(tencent_ocr, 'get_config_value', return_value=True):
            assert tencent_ocr.is_ocr_enabled() is True

    def test_is_ocr_enabled_when_config_false_then_false(self):
        """OCR_ENABLED 配置为假 → 总开关关闭"""
        with patch.object(tencent_ocr, 'get_config_value', return_value=False):
            assert tencent_ocr.is_ocr_enabled() is False

    def test_is_pdf_ocr_enabled_when_master_off_then_false(self):
        """总开关关闭时，即使 OCR_PDF_AUTO 开启也不识别 PDF"""
        with patch.object(tencent_ocr, 'is_ocr_enabled', return_value=False):
            assert tencent_ocr.is_pdf_ocr_enabled() is False

    def test_is_pdf_ocr_enabled_when_pdf_auto_off_then_false(self):
        """总开关开启但 OCR_PDF_AUTO 关闭 → 不识别 PDF"""
        with patch.object(tencent_ocr, 'is_ocr_enabled', return_value=True), \
                patch.object(tencent_ocr, 'get_config_value', return_value=False):
            assert tencent_ocr.is_pdf_ocr_enabled() is False

    def test_is_pdf_ocr_enabled_when_both_on_then_true(self):
        """总开关与 OCR_PDF_AUTO 均开启 → 识别 PDF"""
        with patch.object(tencent_ocr, 'is_ocr_enabled', return_value=True), \
                patch.object(tencent_ocr, 'get_config_value', return_value=True):
            assert tencent_ocr.is_pdf_ocr_enabled() is True

    def test_pdf_ocr_page_limit_when_bad_value_then_default(self):
        """OCR_PDF_PAGE_LIMIT 配置缺失/非法 → 回退默认 50"""
        with patch.object(tencent_ocr, 'get_config_value', return_value=None):
            assert tencent_ocr.pdf_ocr_page_limit() == 50

    def test_pdf_ocr_page_limit_when_zero_then_min_one(self):
        """OCR_PDF_PAGE_LIMIT 配置为 0 → 至少保留 1 页，避免整份跳过"""
        with patch.object(tencent_ocr, 'get_config_value', return_value=0):
            assert tencent_ocr.pdf_ocr_page_limit() == 1


# ============================================================================
# 客户端构建
# ============================================================================
class TestGetOcrClient:
    def test_get_ocr_client_when_credentials_missing_then_returns_none(self):
        """SecretId/SecretKey 未配置 → 返回 None（降级，不抛异常）"""
        with patch.object(settings, 'TENCENT_OCR_SECRET_ID', ''), \
                patch.object(settings, 'TENCENT_OCR_SECRET_KEY', ''):
            assert tencent_ocr.get_ocr_client() is None

    def test_get_ocr_client_when_credentials_configured_then_builds_client(self):
        """凭证已配置 → 构建 OcrClient（假 SDK 返回固定 client）"""
        with _fake_tencent_sdk(), \
                patch.object(settings, 'TENCENT_OCR_SECRET_ID', 'id'), \
                patch.object(settings, 'TENCENT_OCR_SECRET_KEY', 'key'):
            assert tencent_ocr.get_ocr_client() == 'client'


# ============================================================================
# OCR 调用
# ============================================================================
class TestOcrImageBytes:
    def test_ocr_image_bytes_when_disabled_then_returns_empty(self):
        """总开关关闭 → 直接返回空串，不触碰 SDK"""
        with patch.object(tencent_ocr, 'is_ocr_enabled', return_value=False):
            assert tencent_ocr.ocr_image_bytes(b'abc') == ''

    def test_ocr_image_bytes_when_no_client_then_returns_empty(self):
        """凭证未配置 → 返回空串（降级）"""
        with patch.object(tencent_ocr, 'is_ocr_enabled', return_value=True), \
                patch.object(tencent_ocr, 'get_ocr_client', return_value=None):
            assert tencent_ocr.ocr_image_bytes(b'abc') == ''

    def test_ocr_image_bytes_when_success_then_extracts_text(self):
        """正常识别：按行拼接 DetectedText 返回"""
        fake_client = MagicMock()
        det1 = MagicMock(DetectedText='第一行')
        det2 = MagicMock(DetectedText='第二行')
        fake_client.GeneralBasicOCR.return_value = MagicMock(TextDetections=[det1, det2])
        with _fake_tencent_sdk(), \
                patch.object(tencent_ocr, 'is_ocr_enabled', return_value=True), \
                patch.object(tencent_ocr, 'get_ocr_client', return_value=fake_client), \
                patch.object(tencent_ocr, '_resolve_api_name', return_value='GeneralBasicOCR'), \
                patch.object(tencent_ocr, '_consume_ocr_quota', return_value=True):
            assert tencent_ocr.ocr_image_bytes(b'abc') == '第一行\n第二行'

    def test_ocr_image_bytes_when_api_raises_then_returns_empty(self):
        """接口调用抛异常 → 记录日志并返回空串，不阻断业务"""
        fake_client = MagicMock()
        fake_client.GeneralBasicOCR.side_effect = Exception('api boom')
        with _fake_tencent_sdk(), \
                patch.object(tencent_ocr, 'is_ocr_enabled', return_value=True), \
                patch.object(tencent_ocr, 'get_ocr_client', return_value=fake_client), \
                patch.object(tencent_ocr, '_resolve_api_name', return_value='GeneralBasicOCR'), \
                patch.object(tencent_ocr, '_consume_ocr_quota', return_value=True):
            assert tencent_ocr.ocr_image_bytes(b'abc') == ''

    def test_ocr_image_bytes_when_quota_exceeded_then_returns_empty(self):
        """开发期调用次数已达上限 → 停止识别，返回空串且不触碰 SDK"""
        fake_client = MagicMock()
        with _fake_tencent_sdk(), \
                patch.object(tencent_ocr, 'is_ocr_enabled', return_value=True), \
                patch.object(tencent_ocr, 'get_ocr_client', return_value=fake_client), \
                patch.object(tencent_ocr, '_consume_ocr_quota', return_value=False):
            assert tencent_ocr.ocr_image_bytes(b'abc') == ''
            # 已达上限时不应发起任何 API 调用
            fake_client.GeneralBasicOCR.assert_not_called()


# ============================================================================
# 接口名解析 / 文本提取 / 图片压缩
# ============================================================================
class TestOcrHelpers:
    def test_resolve_api_name_when_invalid_config_then_returns_default(self):
        """OCR_API 配置非法 → 回退默认 GeneralBasicOCR"""
        with patch.object(tencent_ocr, 'get_config_value', return_value='NotExistAPI'):
            assert tencent_ocr._resolve_api_name() == tencent_ocr.DEFAULT_OCR_API

    def test_resolve_api_name_when_valid_config_then_keeps_it(self):
        """OCR_API 配置合法 → 原样返回"""
        with patch.object(tencent_ocr, 'get_config_value', return_value='GeneralAccurateOCR'):
            assert tencent_ocr._resolve_api_name() == 'GeneralAccurateOCR'

    def test_extract_text_when_detections_then_joins_lines(self):
        """TextDetections 非空 → 按行拼接（跳过空文本）"""
        resp = MagicMock(TextDetections=[
            MagicMock(DetectedText='a'), MagicMock(DetectedText=''), MagicMock(DetectedText='b'),
        ])
        assert tencent_ocr._extract_text(resp) == 'a\nb'

    def test_extract_text_when_empty_then_returns_empty(self):
        """TextDetections 为空 → 返回空串"""
        assert tencent_ocr._extract_text(MagicMock(TextDetections=[])) == ''

    def test_compress_if_needed_when_under_limit_then_unchanged(self):
        """图片未超限 → 原样返回，不做无谓压缩"""
        data = b'x' * 100
        assert tencent_ocr._compress_if_needed(data) == data

    def test_compress_if_needed_when_over_limit_then_smaller(self):
        """图片超限 → 压缩为 JPEG，体积显著减小"""
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (300, 300), (255, 255, 255)).save(buf, 'PNG')
        data = buf.getvalue()
        with patch.object(tencent_ocr, 'TENCENT_IMAGE_BASE64_LIMIT', 50):
            out = tencent_ocr._compress_if_needed(data)
        assert out != data
        assert len(out) < len(data)
        # 压缩产物应为合法 JPEG（Pillow 可再次打开）
        from PIL import Image as PILImage
        PILImage.open(io.BytesIO(out)).verify()


# ============================================================================
# 开发期 OCR 调用次数限额（DB 集成测试）
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestOcrQuota:
    def test_consume_first_call_then_creates_counter(self):
        """首次调用 → 自动建行且计数为 1，放行"""
        from apps.knowledge.models import OcrUsageCounter
        assert tencent_ocr._consume_ocr_quota() is True
        row = OcrUsageCounter.objects.get(pk=tencent_ocr.OCR_QUOTA_COUNTER_PK)
        assert row.count == 1

    def test_consume_multiple_calls_then_increments(self):
        """连续调用 → 计数逐步自增，未达上限时始终放行"""
        from apps.knowledge.models import OcrUsageCounter
        for _ in range(3):
            assert tencent_ocr._consume_ocr_quota() is True
        row = OcrUsageCounter.objects.get(pk=tencent_ocr.OCR_QUOTA_COUNTER_PK)
        assert row.count == 3

    def test_consume_when_reaching_quota_then_last_call_allowed_and_next_stopped(self):
        """达到 DEV_OCR_QUOTA 的那次调用仍放行，下一次起停止"""
        from apps.knowledge.models import OcrUsageCounter
        OcrUsageCounter.objects.create(pk=tencent_ocr.OCR_QUOTA_COUNTER_PK, count=tencent_ocr.DEV_OCR_QUOTA - 1)
        # 第 800 次调用：计数 799 → 自增到 800，放行
        assert tencent_ocr._consume_ocr_quota() is True
        assert OcrUsageCounter.objects.get(pk=tencent_ocr.OCR_QUOTA_COUNTER_PK).count == tencent_ocr.DEV_OCR_QUOTA
        # 第 801 次调用：已达上限，不放行
        assert tencent_ocr._consume_ocr_quota() is False
        assert OcrUsageCounter.objects.get(pk=tencent_ocr.OCR_QUOTA_COUNTER_PK).count == tencent_ocr.DEV_OCR_QUOTA
