"""共享类型定义：租户、拒绝原因、判定结果。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class RejectCode(str, enum.Enum):
    """可区分的拒绝原因。"""

    CREDENTIAL_MISSING = "TENANT_CREDENTIAL_MISSING"
    CREDENTIAL_INVALID = "TENANT_CREDENTIAL_INVALID"
    CREDENTIAL_REVOKED = "TENANT_CREDENTIAL_REVOKED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    api_key: str
    quota_limit: int
    revoked: bool = False


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    tenant: Tenant | None = None
    reject_code: RejectCode | None = None
    detail: str = ""


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    used: int
    limit: int
    window_epoch: int
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class IdempotencyDecision:
    """is_replay=True 表示只是重放首个请求的结果；conflict=True 表示同键不同载荷。"""

    is_replay: bool
    conflict: bool
    stored_response: dict | None = None


@dataclass
class AuditEntry:
    tenant_id: str | None
    action: str
    decision: str
    reason: str
    extra: dict = field(default_factory=dict)
