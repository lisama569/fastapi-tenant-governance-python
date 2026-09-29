"""治理流水线：认证 -> 幂等 -> 配额 -> 业务操作，全程记录审计日志。"""
from __future__ import annotations

import logging
import uuid

from . import auth, idempotency, quota
from .models import AuditEntry, RejectCode
from .store import Store

logger = logging.getLogger("tenant_governance")


class GovernanceError(Exception):
    """携带 HTTP 状态码与可区分拒绝原因的业务异常。"""

    def __init__(self, status_code: int, code: RejectCode, detail: str, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code.value
        self.detail = detail
        self.retry_after = retry_after


class GovernanceService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.audit_log: list[AuditEntry] = []

    # ---- 内部工具 ----
    def _audit(self, tenant_id: str | None, action: str, decision: str, reason: str, **extra) -> None:
        entry = AuditEntry(tenant_id=tenant_id, action=action, decision=decision, reason=reason, extra=extra)
        self.audit_log.append(entry)
        logger.info(
            "audit action=%s tenant=%s decision=%s reason=%s extra=%s",
            action, tenant_id, decision, reason, extra,
        )

    def _authenticate(self, api_key: str | None, action: str):
        result = auth.authenticate(self.store, api_key)
        if not result.ok:
            self._audit(None, action, "reject", result.detail, code=result.reject_code.value)
            raise GovernanceError(401, result.reject_code, result.detail)
        return result.tenant

    def _consume_quota(self, tenant_id: str, limit: int, action: str) -> None:
        decision = quota.check_and_consume(self.store, tenant_id, limit)
        if not decision.allowed:
            reason = (
                f"窗口 {decision.window_epoch} 内已用 {decision.used}/{decision.limit}，"
                f"{decision.retry_after_seconds}s 后窗口重置"
            )
            self._audit(tenant_id, action, "reject", reason, code=RejectCode.QUOTA_EXCEEDED.value)
            raise GovernanceError(429, RejectCode.QUOTA_EXCEEDED, reason, retry_after=decision.retry_after_seconds)
        self._audit(
            tenant_id, action, "quota_consume",
            f"窗口 {decision.window_epoch} 用量 {decision.used}/{decision.limit}",
        )

    # ---- 写操作：幂等 + 配额 ----
    def create_document(self, api_key: str | None, idempotency_key: str | None, payload: dict) -> tuple[int, dict]:
        action = "create_document"
        tenant = self._authenticate(api_key, action)
        if not idempotency_key:
            self._audit(tenant.tenant_id, action, "reject", "缺少 Idempotency-Key 请求头",
                        code=RejectCode.IDEMPOTENCY_KEY_MISSING.value)
            raise GovernanceError(400, RejectCode.IDEMPOTENCY_KEY_MISSING, "写操作必须携带 Idempotency-Key 请求头")

        decision = idempotency.begin(self.store, tenant.tenant_id, idempotency_key, payload)
        if decision.conflict:
            self._audit(tenant.tenant_id, action, "reject",
                        f"幂等键 {idempotency_key!r} 已绑定不同载荷",
                        code=RejectCode.IDEMPOTENCY_CONFLICT.value)
            raise GovernanceError(409, RejectCode.IDEMPOTENCY_CONFLICT,
                                  "同一 Idempotency-Key 提交了不同的请求内容")
        if decision.is_replay:
            self._audit(tenant.tenant_id, action, "replay",
                        f"幂等键 {idempotency_key!r} 重放首次结果")
            return 201, decision.stored_response

        try:
            self._consume_quota(tenant.tenant_id, tenant.quota_limit, action)
            doc_id = uuid.uuid4().hex
            body = self.store.create_document(
                tenant.tenant_id, doc_id,
                title=str(payload.get("title", "")), body=str(payload.get("body", "")),
            )
        except Exception:
            # 首个请求失败：释放幂等键，让后续重试可重新执行
            idempotency.abort(self.store, tenant.tenant_id, idempotency_key)
            raise
        idempotency.finish(self.store, tenant.tenant_id, idempotency_key, body)
        self._audit(tenant.tenant_id, action, "allow", f"创建文档 {doc_id}")
        return 201, body

    # ---- 读操作：配额 + 租户隔离 ----
    def get_document(self, api_key: str | None, doc_id: str) -> dict:
        action = "get_document"
        tenant = self._authenticate(api_key, action)
        self._consume_quota(tenant.tenant_id, tenant.quota_limit, action)
        doc = self.store.get_document(tenant.tenant_id, doc_id)
        if doc is None:
            self._audit(tenant.tenant_id, action, "reject",
                        f"文档 {doc_id!r} 不存在或属于其他租户（按不存在处理，不泄露归属）",
                        code=RejectCode.RESOURCE_NOT_FOUND.value)
            raise GovernanceError(404, RejectCode.RESOURCE_NOT_FOUND, "资源不存在")
        self._audit(tenant.tenant_id, action, "allow", f"读取文档 {doc_id}")
        return doc
