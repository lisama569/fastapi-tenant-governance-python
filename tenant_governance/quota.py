"""固定窗口配额判定：窗口内计数并发安全，超限给出恢复等待时间。"""
from __future__ import annotations

import math
import time

from .models import QuotaDecision
from .store import Store

WINDOW_SECONDS = 60


def check_and_consume(store: Store, tenant_id: str, limit: int, now: float | None = None) -> QuotaDecision:
    """原子地检查并占用 1 份额度。

    窗口按 UNIX 时间整除对齐；窗口结束后计数自动归零（新窗口新行），无需清理任务。
    """
    now = time.time() if now is None else now
    window_epoch = int(now // WINDOW_SECONDS)
    allowed, used = store.consume_quota(tenant_id, limit, window_epoch)
    retry_after = 0
    if not allowed:
        retry_after = max(1, math.ceil((window_epoch + 1) * WINDOW_SECONDS - now))
    return QuotaDecision(
        allowed=allowed,
        used=used,
        limit=limit,
        window_epoch=window_epoch,
        retry_after_seconds=retry_after,
    )
