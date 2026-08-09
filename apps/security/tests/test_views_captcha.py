"""
apps.security.views 验证码测试 —— CaptchaView / verify_captcha / 生成函数

覆盖范围：
- CaptchaView 生成（mock Redis 与验证码图片）、图片生成失败 500
- verify_captcha 正确/错误/超长/空参/一次性消费
- _generate_captcha_text 字符集与长度、_generate_captcha_image 真实生成与异常降级
- _get_redis 降级分支（无 REDIS_URL 时按环境变量拼接 / 有 REDIS_URL 走 from_url）

Mock 策略：验证码依赖 Redis 与 PIL 图片生成，测试中 mock 外部依赖。
"""
import base64
import os
from unittest.mock import patch, MagicMock

import pytest
from django.test import override_settings

from apps.security import views as security_views
from apps.security.tests.test_views import SecurityAPITestBase


class _FakeRedis:
    """内存版 Redis 客户端：支持 setex/get/delete/exists/ttl"""

    def __init__(self):
        self._data = {}

    def setex(self, key, ttl, value):
        self._data[key] = value

    def get(self, key):
        return self._data.get(key)

    def delete(self, key):
        self._data.pop(key, None)

    def exists(self, key):
        return key in self._data

    def ttl(self, key):
        return 300 if key in self._data else -2


class TestCaptcha(SecurityAPITestBase):
    """图形验证码生成与校验"""

    @pytest.mark.integration
    def test_captcha_generate_200(self):
        """验证码接口返回 captcha_id 与 base64 图片，且文本已写入 Redis"""
        fake_redis = _FakeRedis()
        with patch('apps.security.views._get_redis', return_value=fake_redis), \
             patch('apps.security.views._generate_captcha_image',
                   return_value=MagicMock(read=lambda: b'PNGDATA')):
            resp = self.client.get('/api/v1/security/captcha/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['captcha_id']
        assert base64.b64decode(data['image_b64']) == b'PNGDATA'
        # 验证码文本已写入 Redis（key 前缀 captcha:）
        keys = [k for k in fake_redis._data if k.startswith('captcha:')]
        assert len(keys) == 1

    @pytest.mark.integration
    def test_captcha_image_failure_500(self):
        """图片生成失败（PIL 异常）返回 500"""
        with patch('apps.security.views._get_redis', return_value=_FakeRedis()), \
             patch('apps.security.views._generate_captcha_image', return_value=None):
            resp = self.client.get('/api/v1/security/captcha/')
        assert resp.status_code == 500

    @pytest.mark.integration
    def test_captcha_text_charset_and_length(self):
        """验证码文本由大写字母+数字组成，长度默认 4"""
        with patch('random.choice', side_effect=list('a1B2')):
            text = security_views._generate_captcha_text()
        assert len(text) == 4
        assert text.isalnum()

    @pytest.mark.integration
    def test_verify_captcha_correct_and_one_time(self):
        """验证码正确时返回 True 并立即删除（一次性使用）"""
        fake_redis = _FakeRedis()
        fake_redis.setex('captcha:abc123', 300, 'K7P2')
        with patch('apps.security.views._get_redis', return_value=fake_redis):
            assert security_views.verify_captcha('abc123', 'k7p2') is True
        # 已消费删除，再次验证失败
        with patch('apps.security.views._get_redis', return_value=fake_redis):
            assert security_views.verify_captcha('abc123', 'K7P2') is False

    @pytest.mark.integration
    def test_verify_captcha_error_paths(self):
        """空参/超长/未命中均返回 False"""
        fake_redis = _FakeRedis()
        fake_redis.setex('captcha:xyz', 300, 'AAAA')
        with patch('apps.security.views._get_redis', return_value=fake_redis):
            assert security_views.verify_captcha('', 'AAAA') is False
            assert security_views.verify_captcha('xyz', '') is False
            assert security_views.verify_captcha('xyz', 'A' * 11) is False
            assert security_views.verify_captcha('xyz', 'WRONG') is False
            assert security_views.verify_captcha('not-exist', 'AAAA') is False

    @pytest.mark.integration
    def test_verify_captcha_redis_error_false(self):
        """Redis 异常时验证失败但不抛异常"""
        with patch('apps.security.views._get_redis',
                   side_effect=RuntimeError('redis down')):
            assert security_views.verify_captcha('abc', 'AAA') is False

    @pytest.mark.integration
    def test_get_redis_env_fallback(self):
        """无 REDIS_URL 时按 REDIS_DB_* 环境变量拼接连接"""
        fake_client = MagicMock()
        with override_settings(REDIS_URL=''), \
             patch.dict(os.environ, {
                 'REDIS_DB_HOST': 'redis-test', 'REDIS_DB_PORT': '6379',
                 'REDIS_DB_PASSWORD': 'pw', 'REDIS_DB_CAPTCHA': '3',
             }, clear=False), \
             patch('redis.Redis') as mock_cls:
            mock_cls.return_value = fake_client
            result = security_views._get_redis()
        assert result is fake_client
        # 确认按 env 拼接了 host/port/password/db
        _, kwargs = mock_cls.call_args
        assert kwargs['host'] == 'redis-test'
        assert kwargs['db'] == 3

    @pytest.mark.integration
    def test_get_redis_from_url_branch(self):
        """配置了 REDIS_URL 时走 from_url 快捷方式"""
        fake_client = MagicMock()
        with override_settings(REDIS_URL='redis://localhost:6379/2'), \
             patch('redis.Redis.from_url', return_value=fake_client) as mock_from_url:
            result = security_views._get_redis()
        assert result is fake_client
        mock_from_url.assert_called_once_with('redis://localhost:6379/2', decode_responses=True)

    @pytest.mark.integration
    def test_generate_captcha_image_real(self):
        """真实调用 PIL 生成验证码图片（本项目自带字体），返回 PNG 字节流"""
        buf = security_views._generate_captcha_image('AB12')
        assert buf is not None
        data = buf.read()
        # PNG 魔数头
        assert data[:8] == b'\x89PNG\r\n\x1a\n'

    @pytest.mark.integration
    def test_generate_captcha_image_exception_returns_none(self):
        """PIL 绘制抛异常时返回 None（接口层降级为 500）"""
        # Image 在 _generate_captcha_image 函数内部 import，模块级不存在该属性，须 patch PIL 源类
        with patch('PIL.Image.new', side_effect=RuntimeError('oom')):
            assert security_views._generate_captcha_image('AB12') is None
