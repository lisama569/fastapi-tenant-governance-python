"""幂等键判定。骨架阶段为空实现。"""
from __future__ import annotations

import hashlib
import json

from .models import IdempotencyDecision
from .store import Store


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def begin(store: Store, tenant_id: str, key: str, payload: dict) -> IdempotencyDecision:
    raise NotImplementedError


def finish(store: Store, tenant_id: str, key: str, response: dict) -> None:
    raise NotImplementedError
