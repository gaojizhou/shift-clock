"""RFC 6238 TOTP（基于时间的两步验证动态码），标准库实现，无第三方依赖。

超级管理员入口用：服务端持有 base32 密钥（env FDC_ADMIN_TOTP_SECRET），
管理员把同一密钥录入身份验证器 App（Google Authenticator / 1Password 等），
登录时输入 6 位动态码。容忍 ±1 个时间窗（30s）的时钟偏差。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

STEP = 30
DIGITS = 6


def generate_secret() -> str:
    """生成 160-bit base32 密钥（Authenticator App 标准格式）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    key = base64.b32decode(secret_b32.strip().upper() + "=" * (-len(secret_b32.strip()) % 8))
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp_at(secret_b32: str, ts: float | None = None) -> str:
    """某时刻的动态码（默认当前时间）。测试与排障用。"""
    t = time.time() if ts is None else ts
    return _hotp(secret_b32, int(t // STEP))


def verify_totp(secret_b32: str, code: str, window: int = 1) -> bool:
    """校验动态码，容忍前后 window 个时间窗。"""
    code = (code or "").strip()
    if not secret_b32 or not code.isdigit() or len(code) != DIGITS:
        return False
    counter = int(time.time() // STEP)
    for w in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, counter + w), code):
            return True
    return False


def otpauth_uri(secret_b32: str, account: str = "admin", issuer: str = "三班倒打卡") -> str:
    """生成 otpauth:// URI（可转二维码供 Authenticator 扫码录入）。"""
    from urllib.parse import quote

    label = f"{quote(issuer)}:{quote(account)}"
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&period={STEP}&digits={DIGITS}"
