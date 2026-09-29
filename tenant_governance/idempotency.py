"""幂等键判定：同键同载荷重放首次结果，同键不同载荷判冲突。"""
from __future__ import annotations

import hashlib
import json

from .models import IdempotencyDecision
from .store import Store


def payload_hash(payload: dict) -> str:
    """对请求体做规范化哈希，作为“内容是否一致”的判据。"""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def begin(store: Store, tenant_id: str, key: str, payload: dict) -> IdempotencyDecision:
    status, stored = store.claim_idempotency(tenant_id, key, payload_hash(payload))
    if status == "conflict":
        return IdempotencyDecision(is_replay=False, conflict=True)
    if status == "replay":
        return IdempotencyDecision(is_replay=True, conflict=False, stored_response=stored)
    return IdempotencyDecision(is_replay=False, conflict=False)


def finish(store: Store, tenant_id: str, key: str, response: dict) -> None:
    store.complete_idempotency(tenant_id, key, response)


def abort(store: Store, tenant_id: str, key: str) -> None:
    """首个请求失败时释放键，允许后续重试重新执行。"""
    store.abort_idempotency(tenant_id, key)
