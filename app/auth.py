"""租户凭据解析与校验。

凭据格式：``tk_<tenant_id>.<secret>``
- 无头                   -> MISSING_CREDENTIALS (401)
- 格式错误/租户不存在    -> FORGED_CREDENTIAL  (401)
- secret 与库中哈希不符  -> FORGED_CREDENTIAL  (401)
- 凭据为真但停用/过期    -> INVALID_CREDENTIALS (401)
"""

from __future__ import annotations

import hashlib
import hmac
import time

from collections.abc import Callable

from fastapi import Request

from app.audit import audit_log
from app.errors import (
    ForgedCredentialError,
    InvalidCredentialError,
    MissingCredentialError,
)
from app.store import Store

API_KEY_HEADER = "X-API-Key"


def hash_api_key(api_key: str) -> str:
    """对原始凭据做 SHA-256 哈希。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def authenticate(
    request: Request,
    store: Store,
    *,
    time_func: Callable[[], float] = time.time,
) -> str:
    """识别并校验租户凭据，成功返回 tenant_id。"""
    raw = request.headers.get(API_KEY_HEADER)
    if not raw:
        audit_log(None, "auth", "DENY", "missing_api_key_header",
                  path=request.url.path)
        raise MissingCredentialError(
            "缺少租户凭据：请在 X-API-Key 头中提供 API Key"
        )

    prefix, sep, rest = raw.partition("_")
    if prefix != "tk" or not sep or "." not in rest:
        audit_log(None, "auth", "DENY", "malformed_api_key",
                  path=request.url.path)
        raise ForgedCredentialError("凭据格式不合法，疑似伪造")

    tenant_id, _, secret = rest.partition(".")
    row = store.get_tenant_row(tenant_id)
    # 用空占位哈希做一次比较，避免“租户是否存在”成为可探测的时序侧信道
    expected = row["api_key_hash"] if row else "0" * 64
    if not hmac.compare_digest(hash_api_key(raw), expected) or row is None:
        audit_log(tenant_id if row else None, "auth", "DENY",
                  "api_key_mismatch", path=request.url.path)
        raise ForgedCredentialError("凭据无效或签名不匹配，疑似伪造")

    if not bool(row["enabled"]):
        audit_log(tenant_id, "auth", "DENY", "tenant_disabled",
                  path=request.url.path)
        raise InvalidCredentialError("凭据已失效：租户已被停用")

    expires_at = row["expires_at"]
    if expires_at is not None and time_func() >= float(expires_at):
        audit_log(tenant_id, "auth", "DENY", "api_key_expired",
                  expires_at=expires_at, path=request.url.path)
        raise InvalidCredentialError("凭据已失效：API Key 已过期")

    audit_log(tenant_id, "auth", "ALLOW", "api_key_verified",
              path=request.url.path)
    return tenant_id
