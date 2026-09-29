"""SQLite 持久化存储：租户、资源、配额窗口、幂等记录。

- 所有写操作在进程内锁 + SQLite 事务下完成，保证并发请求计数准确；
- 使用文件库时，重启后配额窗口计数与幂等记录保持不变，行为可预期；
- 幂等认领支持并发等待：同一键的并发重放会阻塞到首个请求完成，然后重放其结果。
"""
from __future__ import annotations

import json
import sqlite3
import threading

from .models import Tenant

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   TEXT PRIMARY KEY,
    api_key     TEXT NOT NULL UNIQUE,
    quota_limit INTEGER NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS documents (
    tenant_id TEXT NOT NULL,
    doc_id    TEXT NOT NULL,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, doc_id)
);
CREATE TABLE IF NOT EXISTS quota_windows (
    tenant_id    TEXT NOT NULL,
    window_epoch INTEGER NOT NULL,
    used         INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, window_epoch)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id    TEXT NOT NULL,
    key          TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,           -- in_progress | completed
    response     TEXT,                    -- JSON，completed 后写入
    PRIMARY KEY (tenant_id, key)
);
"""

_CLAIM_WAIT_SECONDS = 30.0


class Store:
    """进程内单例存储，SQLite 落盘以保证重启后行为可预期。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._cond = threading.Condition()
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._cond:
            self._conn.close()

    # ---- 租户 ----
    def upsert_tenant(self, tenant: Tenant) -> None:
        with self._cond, self._conn:
            self._conn.execute(
                "INSERT INTO tenants (tenant_id, api_key, quota_limit, revoked) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET api_key=excluded.api_key, "
                "quota_limit=excluded.quota_limit, revoked=excluded.revoked",
                (tenant.tenant_id, tenant.api_key, tenant.quota_limit, int(tenant.revoked)),
            )

    def get_tenant_by_key(self, api_key: str) -> Tenant | None:
        with self._cond:
            row = self._conn.execute(
                "SELECT tenant_id, api_key, quota_limit, revoked FROM tenants WHERE api_key = ?",
                (api_key,),
            ).fetchone()
        if row is None:
            return None
        return Tenant(
            tenant_id=row["tenant_id"],
            api_key=row["api_key"],
            quota_limit=row["quota_limit"],
            revoked=bool(row["revoked"]),
        )

    # ---- 资源（按 tenant_id 隔离） ----
    def create_document(self, tenant_id: str, doc_id: str, title: str, body: str) -> dict:
        with self._cond, self._conn:
            self._conn.execute(
                "INSERT INTO documents (tenant_id, doc_id, title, body) VALUES (?, ?, ?, ?)",
                (tenant_id, doc_id, title, body),
            )
        return {"doc_id": doc_id, "tenant_id": tenant_id, "title": title, "body": body}

    def get_document(self, tenant_id: str, doc_id: str) -> dict | None:
        """查询强制带 tenant_id，跨租户标识一律查不到。"""
        with self._cond:
            row = self._conn.execute(
                "SELECT doc_id, tenant_id, title, body FROM documents WHERE tenant_id = ? AND doc_id = ?",
                (tenant_id, doc_id),
            ).fetchone()
        if row is None:
            return None
        return {"doc_id": row["doc_id"], "tenant_id": row["tenant_id"], "title": row["title"], "body": row["body"]}

    # ---- 配额（固定窗口，锁内计数保证并发准确） ----
    def consume_quota(self, tenant_id: str, limit: int, window_epoch: int) -> tuple[bool, int]:
        """尝试占用 1 份额度，返回 (是否成功, 占用后用量)。"""
        with self._cond, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO quota_windows (tenant_id, window_epoch, used) VALUES (?, ?, 0)",
                (tenant_id, window_epoch),
            )
            row = self._conn.execute(
                "SELECT used FROM quota_windows WHERE tenant_id = ? AND window_epoch = ?",
                (tenant_id, window_epoch),
            ).fetchone()
            used = row["used"]
            if used >= limit:
                return False, used
            self._conn.execute(
                "UPDATE quota_windows SET used = used + 1 WHERE tenant_id = ? AND window_epoch = ?",
                (tenant_id, window_epoch),
            )
            return True, used + 1

    # ---- 幂等 ----
    def claim_idempotency(self, tenant_id: str, key: str, payload_hash: str) -> tuple[str, dict | None]:
        """认领幂等键。

        返回 (status, stored_response)：
        - claimed:  首次提交，调用方负责后续 complete/abort；
        - replay:   重复提交且载荷一致，直接重放 stored_response；
        - conflict: 键相同但载荷不同，必须拒绝。
        并发重放会等待首个请求完成后再判定。
        """
        with self._cond:
            while True:
                row = self._conn.execute(
                    "SELECT payload_hash, status, response FROM idempotency_keys WHERE tenant_id = ? AND key = ?",
                    (tenant_id, key),
                ).fetchone()
                if row is None:
                    with self._conn:
                        self._conn.execute(
                            "INSERT INTO idempotency_keys (tenant_id, key, payload_hash, status) VALUES (?, ?, ?, 'in_progress')",
                            (tenant_id, key, payload_hash),
                        )
                    return "claimed", None
                if row["payload_hash"] != payload_hash:
                    return "conflict", None
                if row["status"] == "completed":
                    return "replay", json.loads(row["response"])
                # in_progress：等待首个请求完成或被中止
                self._cond.wait(timeout=_CLAIM_WAIT_SECONDS)

    def complete_idempotency(self, tenant_id: str, key: str, response: dict) -> None:
        with self._cond, self._conn:
            self._conn.execute(
                "UPDATE idempotency_keys SET status = 'completed', response = ? WHERE tenant_id = ? AND key = ?",
                (json.dumps(response), tenant_id, key),
            )
            self._cond.notify_all()

    def abort_idempotency(self, tenant_id: str, key: str) -> None:
        """首个请求失败（如超限）时释放认领，让后续重试可以重新执行。"""
        with self._cond, self._conn:
            self._conn.execute(
                "DELETE FROM idempotency_keys WHERE tenant_id = ? AND key = ? AND status = 'in_progress'",
                (tenant_id, key),
            )
            self._cond.notify_all()
