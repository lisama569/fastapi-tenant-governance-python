"""审计日志：记录每次治理判定的输入与依据。

输出单行 JSON，字段固定，便于测试断言与事后排查“谁、因为什么被拦下”。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

logger = logging.getLogger("tenant_governance.audit")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def audit_log(
    tenant_id: str | None,
    action: str,
    decision: str,
    reason: str,
    **extra: Any,
) -> None:
    """写一条审计记录。

    tenant_id: 识别出的租户（未识别为 None）
    action:    动作，如 auth / quota / idempotency / access / write
    decision:  ALLOW / DENY / REPLAY
    reason:    机器可读的判定依据
    """
    record: dict[str, Any] = {
        "tenant_id": tenant_id,
        "action": action,
        "decision": decision,
        "reason": reason,
    }
    record.update(extra)
    logger.info("AUDIT %s", json.dumps(record, ensure_ascii=False, sort_keys=True))
