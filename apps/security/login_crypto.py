"""登录密码传输加密 - 一次性会话密钥（RSA）

方案：后端每次调用 issue_encrypt_key() 生成一对 RSA 密钥，私钥以 key_id 存入
Redis（TTL 300 秒），公钥下发给前端；前端用公钥加密密码后随 key_id 提交登录，
后端取出私钥解密，取出即删（一次性使用，防重放）。

相比固定公私钥方案：
- 每次登录使用新密钥 → 截获的旧密文无法重放
- 私钥短生命周期 + 用后即弃 → 长期密钥泄露风险小

局限：密钥仍通过 HTTP 明文下发，无法抵抗主动中间人（MITM 可替换公钥，
前端即用攻击者公钥加密）。该方案仅对抗被动嗅探 + 防重放，生产环境仍应配置
HTTPS 作为根本保障。

流程：
GET /api/v1/security/encrypt-key/  → {key_id, public_key, expires_in}
POST /api/v1/auth/login/           → {..., password: <RSA 密文>, key_id, encrypted_password: true}
"""
import base64
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from loguru import logger

# 一次性密钥有效期（秒）：与验证码一致，过期由 Redis TTL 自动清理
_KEY_TTL = 300
_REDIS_PREFIX = 'login_key:'


def _redis():
    """获取 Redis 连接（复用 security.views._get_redis，避免重复实现）

    延迟导入：views 模块在视图方法内才 import login_crypto，避免循环依赖。
    """
    from apps.security.views import _get_redis
    return _get_redis()


def issue_encrypt_key():
    """生成一次性 RSA 密钥对并下发公钥

    Returns:
        dict: {key_id, public_key(PEM), expires_in(秒)}；私钥仅存 Redis，
        进程内不留存，过期自动清理。
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_id = str(uuid.uuid4())
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _redis().setex(f'{_REDIS_PREFIX}{key_id}', _KEY_TTL, private_pem.decode('utf-8'))
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode('utf-8')
    return {'key_id': key_id, 'public_key': public_key, 'expires_in': _KEY_TTL}


def decrypt_password(ciphertext_b64, key_id):
    """按 key_id 取一次性私钥解密登录密码

    一次性语义：取出私钥后立即删除（无论解密是否成功），保证同一密文不可重放。

    Args:
        ciphertext_b64: JSEncrypt 输出的 base64 密文（UTF-8 原文 + PKCS#1 v1.5 填充）
        key_id: 下发公钥时返回的 key_id

    Returns:
        解密后的明文密码字符串；key_id 缺失/密钥不存在/密文非法返回 None。
    """
    if not key_id:
        return None
    try:
        redis_key = f'{_REDIS_PREFIX}{key_id}'
        r = _redis()
        private_pem = r.get(redis_key)
        if not private_pem:
            return None
        # 取出即删：一次性使用，杜绝同一密钥被重放
        r.delete(redis_key)
        key = serialization.load_pem_private_key(private_pem.encode('utf-8'), password=None)
        plain = key.decrypt(base64.b64decode(ciphertext_b64), padding.PKCS1v15())
        return plain.decode('utf-8')
    except Exception:
        logger.warning('[login_crypto] 登录密码解密失败（密钥不存在或密文损坏）')
        return None
