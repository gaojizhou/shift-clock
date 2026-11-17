"""测试工具的签名令牌（HMAC Magic Link），标准库实现，无第三方依赖。

可选的本地身份启动器持有与服务端相同的 SECRET（env FDC_TEST_LOGIN_SECRET），
本地用 SECRET 生成短时效签名：登录 /h5/test/auth、拉员工列表 /h5/test/employees 都靠它授权。
服务端校验签名 + 未过期才放行。公网无 SECRET 者伪造不出 sig，杜绝裸冒充。

与 totp.py 同款：纯 hmac/hashlib + compare_digest 防时序侧信道。
规范串：v1|<part0>|<part1>|…|<exp>（各 part 为空用空串；对解码后的值签名）。
"""

from __future__ import annotations

import hashlib
import hmac
import time

# exp 不得超过当前这么久（防把有效期签得过长滥用）。略大于启动器 ttl(120s) 留时钟偏差余量。
MAX_TTL = 600


def _canonical(parts: list[str | None], exp: int) -> str:
    return "v1|" + "|".join((p or "") for p in parts) + f"|{exp}"


def sign(secret: str, parts: list[str | None], exp: int) -> str:
    """sig=hex(HMAC_SHA256(secret, 规范串))。启动器在 Node 侧用同算法。"""
    msg = _canonical(parts, exp).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(secret: str, parts: list[str | None], exp: str | int | None, sig: str | None) -> bool:
    """密钥已配置 且 now ≤ exp ≤ now+MAX_TTL 且 sig 匹配。任一不满足返回 False。"""
    if not secret or not sig:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    if exp_i < now or exp_i > now + MAX_TTL:
        return False
    return hmac.compare_digest(sign(secret, parts, exp_i), sig)


# ── 具体用途的薄封装（规范串顺序即契约，启动器须一致）──

def sign_login(secret: str, userid: str, role: str | None, redirect: str | None, exp: int) -> str:
    return sign(secret, [userid, role, redirect], exp)


def verify_login_sig(
    secret: str,
    userid: str,
    role: str | None,
    redirect: str | None,
    exp: str | int | None,
    sig: str | None,
) -> bool:
    """登录 URL 校验：parts = [userid, role, redirect]。"""
    return verify(secret, [userid, role, redirect], exp, sig)


def verify_employees_sig(secret: str, exp: str | int | None, sig: str | None) -> bool:
    """员工列表授权校验：parts = ["employees"]。"""
    return verify(secret, ["employees"], exp, sig)
