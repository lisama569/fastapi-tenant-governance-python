"""SQLite 持久层：租户、账户、配额计数、幂等记录。

- 单连接（``check_same_thread=False``）+ 进程级 RLock 串行化写入；
- WAL 模式，提交即落盘，服务重启后额度与幂等状态仍在；
- 配额按“固定时间窗”计数，窗口起点 = floor(now/window)*window，窗口结束自然开新窗；
- 启动时把上次进程遗留的 IN_FLIGHT 幂等记录复位（那些请求从未完成，可安全重试）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id     TEXT PRIMARY KEY,
    api_key_hash  TEXT NOT NULL,
    quota_limit   INTEGER NOT NULL,
    enabled       INTEGER NOT NULL,
    expires_at    REAL
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    balance    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quota_counters (
    tenant_id    TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, window_start)
);
CREATE TABLE IF NOT EXISTS idempotency (
    tenant_id     TEXT NOT NULL,
    idem_key      TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status        TEXT NOT NULL,            -- IN_FLIGHT / COMPLETED
    response_body TEXT,
    status_code   INTEGER,
    created_at    REAL NOT NULL,
    PRIMARY KEY (tenant_id, idem_key)
);
"""


class Store:
    """线程安全的 SQLite 存储。"""

    def __init__(self, db_path: str, *, time_func: Callable[[], float] = time.time) -> None:
        self.db_path = db_path
        self._time = time_func
        self._lock = threading.RLock()
                # isolation_level=None：关闭驱动隐式事务，全部由手工 BEGIN/COMMIT 控制
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")

    # ---- 生命周期 ----
    def init_schema(self, tenants: list[Any] | None = None) -> None:
        """建表、复位上次进程残留的 IN_FLIGHT，并写入内置租户（已存在不覆盖）。"""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 上次进程崩溃时未完成的幂等请求从未生效：删除残留行，
            # 客户端可以用同一个键安全重试（行为等同首次提交）
            self._conn.execute(
                "DELETE FROM idempotency WHERE status='IN_FLIGHT'"
            )
            if tenants:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO tenants"
                    "(tenant_id, api_key_hash, quota_limit, enabled, expires_at) "
                    "VALUES (?,?,?,?,?)",
                    [
                        (
                            t.tenant_id,
                            t.api_key_hash,
                            t.quota_limit,
                            1 if t.enabled else 0,
                            t.expires_at,
                        )
                        for t in tenants
                    ],
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset_all(self) -> None:
        """测试辅助：清空业务表（保留租户配置）。"""
        with self._lock:
            self._conn.execute("DELETE FROM accounts")
            self._conn.execute("DELETE FROM quota_counters")
            self._conn.execute("DELETE FROM idempotency")
            self._conn.commit()

    # ---- 租户 ----
    def get_tenant_row(self, tenant_id: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,)
            )
            return cur.fetchone()

    # ---- 账户 ----
    def create_account(self, tenant_id: str, account_id: str) -> None:
        """在租户名下开户；已存在抛 sqlite3.IntegrityError。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO accounts(tenant_id, account_id, balance) VALUES (?,?,0)",
                    (tenant_id, account_id),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                raise

    def find_account_owner(self, account_id: str) -> str | None:
        """返回账户所属租户；不存在返回 None。用于区分 403 与 404。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT tenant_id FROM accounts WHERE account_id=?", (account_id,)
            )
            row = cur.fetchone()
            return row["tenant_id"] if row else None

    def get_balance(self, tenant_id: str, account_id: str) -> int | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT balance FROM accounts WHERE tenant_id=? AND account_id=?",
                (tenant_id, account_id),
            )
            row = cur.fetchone()
            return row["balance"] if row else None

    def deposit(self, tenant_id: str, account_id: str, amount: int) -> int:
        """原子加余额，返回新余额。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE accounts SET balance=balance+? WHERE tenant_id=? AND account_id=?",
                (amount, tenant_id, account_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise LookupError((tenant_id, account_id))
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT balance FROM accounts WHERE tenant_id=? AND account_id=?",
                (tenant_id, account_id),
            )
            return int(cur.fetchone()["balance"])

    # ---- 配额 ----
    def consume_quota(
        self, tenant_id: str, limit: int, window_seconds: int
    ) -> tuple[bool, int, int]:
        """在固定时间窗内原子占用一个槽位。

        返回 (allowed, window_count, retry_after_seconds)：
        allowed=False 时 retry_after 即本窗口还剩秒数（至少 1）。
        """
        with self._lock:
            now = int(self._time())
            window_start = (now // window_seconds) * window_seconds
            window_end = window_start + window_seconds
            retry_after = max(1, window_end - now)
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO quota_counters(tenant_id, window_start, count) "
                    "VALUES (?,?,0) ON CONFLICT(tenant_id, window_start) DO NOTHING",
                    (tenant_id, window_start),
                )
                cur = self._conn.execute(
                    "SELECT count FROM quota_counters "
                    "WHERE tenant_id=? AND window_start=?",
                    (tenant_id, window_start),
                )
                count = int(cur.fetchone()["count"])
                if count >= limit:
                    self._conn.rollback()
                    return False, count, retry_after
                self._conn.execute(
                    "UPDATE quota_counters SET count=count+1 "
                    "WHERE tenant_id=? AND window_start=?",
                    (tenant_id, window_start),
                )
                self._conn.commit()
                return True, count + 1, retry_after
            except Exception:
                self._conn.rollback()
                raise

    # ---- 幂等 ----
    def idempotency_begin(
        self, tenant_id: str, key: str, request_hash: str
    ) -> tuple[str, str | None, str | None, int | None]:
        """登记幂等键，返回 (status, stored_hash, response_body, status_code)。

        status:
        - NEW：新登记，当前调用负责执行业务
        - REPLAY：已完成且同体，附带首次响应
        - CONFLICT：同键异体
        - IN_FLIGHT：同键同体正由另一请求处理（调用方等待后重读）
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM idempotency WHERE tenant_id=? AND idem_key=?",
                (tenant_id, key),
            )
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO idempotency"
                    "(tenant_id, idem_key, request_hash, status, created_at) "
                    "VALUES (?,?,?, 'IN_FLIGHT', ?)",
                    (tenant_id, key, request_hash, self._time()),
                )
                self._conn.commit()
                return "NEW", request_hash, None, None

            stored_hash = row["request_hash"]
            if request_hash != stored_hash:
                return "CONFLICT", stored_hash, None, None
            if row["status"] == "COMPLETED":
                return "REPLAY", stored_hash, row["response_body"], row["status_code"]
            return "IN_FLIGHT", stored_hash, None, None

    def idempotency_get(
        self, tenant_id: str, key: str
    ) -> tuple[str, str | None, int | None] | None:
        """重读幂等记录：(status, response_body, status_code)。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT status, response_body, status_code FROM idempotency "
                "WHERE tenant_id=? AND idem_key=?",
                (tenant_id, key),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return row["status"], row["response_body"], row["status_code"]

    def idempotency_finish(
        self, tenant_id: str, key: str, response_body: str, status_code: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE idempotency SET status='COMPLETED', response_body=?, status_code=? "
                "WHERE tenant_id=? AND idem_key=?",
                (response_body, status_code, tenant_id, key),
            )
            self._conn.commit()

    def idempotency_reset(self, tenant_id: str, key: str) -> None:
        """业务失败：删除 IN_FLIGHT 登记，允许同键重试。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM idempotency WHERE tenant_id=? AND idem_key=? AND status='IN_FLIGHT'",
                (tenant_id, key),
            )
            self._conn.commit()
