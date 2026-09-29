"""写操作幂等。

规则：
- 同租户 + 同幂等键 + 同请求体：只有首个请求真正落账，其余（含并发）重放首次响应；
- 同键不同体：IDEMPOTENCY_CONFLICT (409)；
- 首任者的业务若失败，IN_FLIGHT 登记被删除，同键可安全重试（错误不落账、不缓存）；
- 重放不重复消耗配额（只有首任者占一个配额槽位）。

并发模型：``idempotency`` 表的主键保证同一 (tenant,key) 只有一次 INSERT 成功，
即只有一个“首任者”；后来者用进程内 per-key Condition 等待首任完成后读同一结果。
跨进程的后来者则在 DB 上轮询重读。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from app.audit import audit_log
from app.errors import IdempotencyConflictError, MissingIdempotencyKeyError
from app.store import Store

#: (tenant_id, key) -> Condition，保护同键并发者之间的等待/通知
_conditions: dict[tuple[str, str], threading.Condition] = {}
_conditions_guard = threading.Lock()

#: 同键等待的轮询间隔与最长等待（跨进程/极端情况兜底）
_POLL_SECONDS = 0.02
_MAX_WAIT_SECONDS = 10.0


def _condition_for(tenant_id: str, key: str) -> threading.Condition:
    token = (tenant_id, key)
    with _conditions_guard:
        cond = _conditions.get(token)
        if cond is None:
            cond = threading.Condition()
            _conditions[token] = cond
        return cond


def run_idempotent(
    tenant_id: str,
    key: str | None,
    request_hash: str,
    store: Store,
    business: Callable[[], tuple[dict[str, Any], int]],
) -> tuple[dict[str, Any], int, bool]:
    """执行一个幂等写操作。

    business 仅在首次请求时调用，返回 (响应体, HTTP 状态码)。
    本函数返回 (响应体, HTTP 状态码, is_replay)。
    """
    if not key:
        raise MissingIdempotencyKeyError(
            "写操作必须提供 Idempotency-Key 请求头"
        )

    cond = _condition_for(tenant_id, key)
    waited = 0.0

    while True:
        status, stored_hash, body, code = store.idempotency_begin(
            tenant_id, key, request_hash
        )

        if status == "CONFLICT":
            audit_log(tenant_id, "idempotency", "DENY",
                      "same_key_different_body", idem_key=key)
            raise IdempotencyConflictError(
                "同一个 Idempotency-Key 已用于另一个不同内容的请求",
                detail={"idem_key": key},
            )

        if status == "REPLAY":
            audit_log(tenant_id, "idempotency", "REPLAY",
                      "replayed_first_response", idem_key=key)
            return json.loads(body or "{}"), int(code or 200), True

        if status == "IN_FLIGHT":
            # 同键同体正由另一请求处理：等它完成，然后重读
            with cond:
                cond.wait(timeout=_POLL_SECONDS)
            waited += _POLL_SECONDS
            row = store.idempotency_get(tenant_id, key)
            if row is not None and row[0] == "COMPLETED":
                audit_log(tenant_id, "idempotency", "REPLAY",
                          "replayed_after_concurrent_wait", idem_key=key)
                return json.loads(row[1] or "{}"), int(row[2] or 200), True
            if waited >= _MAX_WAIT_SECONDS:
                # 兜底：首任者长时间未完成（如进程被杀，重启时已复位），重新尝试登记
                waited = 0.0
            continue

        # status == NEW：本请求是首任者
        try:
            response, status_code = business()
            store.idempotency_finish(
                tenant_id, key, json.dumps(response, ensure_ascii=False), status_code
            )
            audit_log(tenant_id, "idempotency", "ALLOW",
                      "first_request_completed", idem_key=key,
                      status_code=status_code)
            return response, status_code, False
        except Exception:
            # 业务失败：未落账，清除登记并唤醒等待者重试
            store.idempotency_reset(tenant_id, key)
            with cond:
                cond.notify_all()
            raise
        finally:
            with cond:
                cond.notify_all()
