"""治理流水线：认证 -> 幂等 -> 配额 -> 业务操作，并记录审计日志。骨架阶段为空实现。"""
from __future__ import annotations

import logging

from .models import AuditEntry
from .store import Store

logger = logging.getLogger("tenant_governance")


class GovernanceError(Exception):
    """携带 HTTP 状态码与可区分拒绝原因的业务异常。"""

    def __init__(self, status_code: int, code: str, detail: str, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retry_after = retry_after


class GovernanceService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.audit_log: list[AuditEntry] = []

    def create_document(self, api_key: str | None, idempotency_key: str | None, payload: dict) -> tuple[int, dict]:
        raise NotImplementedError

    def get_document(self, api_key: str | None, doc_id: str) -> dict:
        raise NotImplementedError
