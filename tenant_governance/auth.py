"""租户凭据识别：区分缺失 / 无效 / 已吊销。骨架阶段为空实现。"""
from __future__ import annotations

from .models import AuthResult
from .store import Store


def authenticate(store: Store, api_key: str | None) -> AuthResult:
    raise NotImplementedError
