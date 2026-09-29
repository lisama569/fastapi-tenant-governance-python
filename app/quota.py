"""按租户的固定时间窗配额/限流。"""

from __future__ import annotations

from app.audit import audit_log
from app.errors import QuotaExceededError
from app.store import Store


def check_and_consume(
    tenant_id: str,
    limit: int,
    store: Store,
    window_seconds: int,
) -> None:
    """尝试占用一个配额槽位；超限抛 QuotaExceededError。

    计数器在 SQLite 单事务内读改写并由进程锁保护，因此一批并发请求
    不会把同一窗口的计数加多；窗口结束后 window_start 变化即自动恢复。
    """
    allowed, count, retry_after = store.consume_quota(
        tenant_id, limit, window_seconds
    )
    if not allowed:
        audit_log(
            tenant_id,
            "quota",
            "DENY",
            "quota_exceeded",
            limit=limit,
            window_seconds=window_seconds,
            window_count=count,
            retry_after_seconds=retry_after,
        )
        raise QuotaExceededError(
            f"租户 {tenant_id} 在当前 {window_seconds} 秒窗口内已用满 {limit} 次配额",
            detail={
                "tenant_id": tenant_id,
                "limit": limit,
                "window_seconds": window_seconds,
                "window_count": count,
                "retry_after_seconds": retry_after,
            },
        )
    audit_log(
        tenant_id,
        "quota",
        "ALLOW",
        "quota_consumed",
        limit=limit,
        window_seconds=window_seconds,
        window_count=count,
    )
