"""统一的业务错误类型与错误码。

判定结论按 HTTP 状态码即可区分：
- 401 认证失败（凭据缺失/失效/伪造，见 code 细分）
- 403 已认证但跨租户越权
- 404 资源在当前租户下不存在
- 409 幂等键冲突 / 资源已存在
- 429 配额超限（含重试等待时间）
"""

from __future__ import annotations

from typing import Any


class GovernanceError(Exception):
    """所有访问治理错误的基类。"""

    code: str = "GOVERNANCE_ERROR"
    status_code: int = 400

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_response(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "status_code": self.status_code,
                "message": self.message,
                "detail": self.detail,
            }
        }


class MissingCredentialError(GovernanceError):
    """请求未携带租户凭据。"""

    code = "MISSING_CREDENTIALS"
    status_code = 401


class InvalidCredentialError(GovernanceError):
    """凭据为真但已失效（租户停用或凭据过期）。"""

    code = "INVALID_CREDENTIALS"
    status_code = 401


class ForgedCredentialError(GovernanceError):
    """凭据伪造（格式错误、租户不存在或密钥不匹配）。"""

    code = "FORGED_CREDENTIAL"
    status_code = 401


class CrossTenantDeniedError(GovernanceError):
    """已认证租户试图访问其他租户的资源。"""

    code = "CROSS_TENANT_DENIED"
    status_code = 403


class ResourceNotFoundError(GovernanceError):
    """资源不存在，或不属于当前租户（不泄露他租户资源存在性）。"""

    code = "RESOURCE_NOT_FOUND"
    status_code = 404


class ResourceExistsError(GovernanceError):
    """同租户下资源已存在。"""

    code = "RESOURCE_EXISTS"
    status_code = 409


class QuotaExceededError(GovernanceError):
    """固定时间窗内超出租户配额；detail.retry_after_seconds 给出恢复等待。"""

    code = "QUOTA_EXCEEDED"
    status_code = 429


class IdempotencyConflictError(GovernanceError):
    """同一幂等键被用于内容不同的请求。"""

    code = "IDEMPOTENCY_CONFLICT"
    status_code = 409


class MissingIdempotencyKeyError(GovernanceError):
    """写操作缺少幂等键。"""

    code = "MISSING_IDEMPOTENCY_KEY"
    status_code = 400
