"""FastAPI 依赖：运行期设置、存储与租户身份。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Depends, Request

from app.auth import authenticate
from app.config import QUOTA_WINDOW_SECONDS, default_tenants
from app.store import Store


@dataclass
class Settings:
    """一次服务运行的装配结果。"""

    store: Store
    window_seconds: int = QUOTA_WINDOW_SECONDS
    tenants: dict = field(default_factory=dict)
    time_func: Callable[[], float] = time.time

    @classmethod
    def create(
        cls,
        db_path: str,
        *,
        time_func: Callable[[], float] = time.time,
        window_seconds: int = QUOTA_WINDOW_SECONDS,
        tenants: list | None = None,
    ) -> "Settings":
        tenant_list = tenants if tenants is not None else default_tenants()
        tenants_map = {t.tenant_id: t for t in tenant_list}
        store = Store(db_path, time_func=time_func)
        store.init_schema(tenants=list(tenants_map.values()))
        return cls(
            store=store,
            window_seconds=window_seconds,
            tenants=tenants_map,
            time_func=time_func,
        )

    def quota_limit_for(self, tenant_id: str) -> int:
        row = self.store.get_tenant_row(tenant_id)
        return int(row["quota_limit"]) if row else 0


def get_settings(request: Request) -> Settings:
    """从 app.state 取装配结果（测试中每个夹具可独立构造）。"""
    return request.app.state.settings


def require_tenant(
    request: Request, settings: Settings = Depends(get_settings)
) -> str:
    """认证依赖：校验 X-API-Key，返回 tenant_id。"""
    return authenticate(request, settings.store, time_func=settings.time_func)
