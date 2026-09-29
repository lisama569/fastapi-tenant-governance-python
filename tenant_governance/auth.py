"""租户凭据识别：明确区分缺失 / 无效 / 已吊销三种失败。"""
from __future__ import annotations

from .models import AuthResult, RejectCode
from .store import Store


def authenticate(store: Store, api_key: str | None) -> AuthResult:
    if api_key is None or not api_key.strip():
        return AuthResult(ok=False, reject_code=RejectCode.CREDENTIAL_MISSING, detail="缺少 X-Api-Key 请求头")
    tenant = store.get_tenant_by_key(api_key)
    if tenant is None:
        return AuthResult(ok=False, reject_code=RejectCode.CREDENTIAL_INVALID, detail="凭据无效或系伪造")
    if tenant.revoked:
        return AuthResult(ok=False, reject_code=RejectCode.CREDENTIAL_REVOKED, detail="凭据已被吊销")
    return AuthResult(ok=True, tenant=tenant)
