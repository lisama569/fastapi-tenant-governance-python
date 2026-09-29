"""pytest 公共夹具：临时 SQLite、可控时钟、测试客户端。"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from app.config import TenantConfig
from app.deps import Settings
from app.main import create_app

KEY_A = "tk_tenant-a.S2u9Vq7XmK1pR4wL"
KEY_B = "tk_tenant-b.H8nT3yB6eJ0cF5xA"
KEY_DISABLED = "tk_tenant-disabled.Q4zD7vN2bG9hW6kE"


class FakeClock:
    """可手动快进的时钟，供配额窗口滚动测试。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tenant_configs(clock: FakeClock) -> list[TenantConfig]:
    return [
        TenantConfig("tenant-a", KEY_A, quota_limit=5),
        TenantConfig("tenant-b", KEY_B, quota_limit=3),
        TenantConfig("tenant-disabled", KEY_DISABLED, quota_limit=2, enabled=False),
        TenantConfig(
            "tenant-expired",
            "tk_tenant-expired.P9qL1mW4xR7sT2vY",
            quota_limit=2,
            expires_at=clock() + 10,
        ),
    ]


@pytest.fixture
def app(tmp_path, clock: FakeClock, tenant_configs):
    db_path = str(tmp_path / "test_tenant_gov.db")
    application = create_app(db_path)
    # 替换为可控时钟的 settings（先关闭初始连接）
    application.state.settings.store.close()
    application.state.settings = Settings.create(
        db_path,
        time_func=clock,
        window_seconds=60,
        tenants=tenant_configs,
    )
    yield application
    application.state.settings.store.close()


@pytest.fixture
def client(app):
    return TestClient(app)


def print_decision(title: str, request_desc: dict, response) -> None:
    """单测日志：打印请求输入与服务端判定结果。"""
    print(
        f"\n[{title}] 请求输入={request_desc} -> "
        f"HTTP {response.status_code} 判定={response.json()}"
    )
