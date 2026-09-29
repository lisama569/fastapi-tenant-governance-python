"""SQLite 持久化存储：租户、资源、配额窗口、幂等记录。骨架阶段为空实现。"""
from __future__ import annotations

import threading

from .models import Tenant


class Store:
    """进程内单例存储，SQLite 落盘以保证重启后行为可预期。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = None  # TODO: 建立连接并建表

    def close(self) -> None:
        raise NotImplementedError

    # ---- 租户 ----
    def upsert_tenant(self, tenant: Tenant) -> None:
        raise NotImplementedError

    def get_tenant_by_key(self, api_key: str) -> Tenant | None:
        raise NotImplementedError

    # ---- 资源 ----
    def create_document(self, tenant_id: str, doc_id: str, title: str, body: str) -> dict:
        raise NotImplementedError

    def get_document(self, tenant_id: str, doc_id: str) -> dict | None:
        raise NotImplementedError

    # ---- 配额（固定窗口） ----
    def consume_quota(self, tenant_id: str, limit: int, window_seconds: int, now: float) -> tuple[int, int, int]:
        """返回 (used_after, limit, window_epoch)。骨架阶段未实现。"""
        raise NotImplementedError

    # ---- 幂等 ----
    def claim_idempotency(self, tenant_id: str, key: str, payload_hash: str) -> tuple[str, dict | None]:
        """返回 (status, stored_response)，status ∈ {claimed, replay, conflict}。"""
        raise NotImplementedError

    def complete_idempotency(self, tenant_id: str, key: str, response: dict) -> None:
        raise NotImplementedError
