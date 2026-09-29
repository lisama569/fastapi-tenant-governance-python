"""固定窗口配额判定。骨架阶段为空实现。"""
from __future__ import annotations

from .models import QuotaDecision
from .store import Store

WINDOW_SECONDS = 60


def check_and_consume(store: Store, tenant_id: str, limit: int, now: float | None = None) -> QuotaDecision:
    raise NotImplementedError
