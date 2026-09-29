"""服务重启一致性：SQLite 落盘，额度计数延续、幂等结果可重放；
上次进程遗留的 IN_FLIGHT 在启动时复位，客户端可用同键安全重试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import TenantConfig
from app.deps import Settings
from app.main import create_app
from tests.conftest import KEY_A, KEY_B, print_decision


def _open_app(db_path: str):
    app = create_app(db_path)
    app.state.settings.store.close()
    # 重启后使用真实时钟
    app.state.settings = Settings.create(
        db_path,
        tenants=[
            TenantConfig("tenant-a", KEY_A, quota_limit=5),
            TenantConfig("tenant-b", KEY_B, quota_limit=3),
        ],
    )
    return TestClient(app), app


def test_quota_and_idempotency_survive_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    client, app = _open_app(db)

    client.post(
        "/accounts",
        json={"account_id": "acc-p"},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "p0"},
    )
    # 再成功 3 笔 -> 本窗口已用 4
    for i in range(3):
        assert client.post(
            "/accounts/deposit",
            json={"account_id": "acc-p", "amount": 10},
            headers={"X-API-Key": KEY_A, "Idempotency-Key": f"p{i+1}"},
        ).status_code == 200
    app.state.settings.store.close()

    # —— 重启 ——
    client2, app2 = _open_app(db)

    # 幂等：同键同体重放，返回首次结果且不重复入账
    replay = client2.post(
        "/accounts/deposit",
        json={"account_id": "acc-p", "amount": 10},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "p1"},
    )
    print_decision("重启-幂等重放", {"Idempotency-Key": "p1"}, replay)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["balance"] == 10  # 首次结果，非 40

    # 额度：本窗口已用 4（重放不耗额），新请求第 5 个放行、第 6 个仍超限
    ok = client2.post(
        "/accounts/deposit",
        json={"account_id": "acc-p", "amount": 5},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "p5"},
    )
    print_decision("重启-额度延续(第5次放行)", {"Idempotency-Key": "p5"}, ok)
    assert ok.status_code == 200

    over = client2.post(
        "/accounts/deposit",
        json={"account_id": "acc-p", "amount": 5},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "p6"},
    )
    print_decision("重启-额度延续(第6次超限)", {"Idempotency-Key": "p6"}, over)
    assert over.status_code == 429

    # 隔离关系也持久保留
    cross = client2.get("/accounts/acc-p", headers={"X-API-Key": KEY_B})
    assert cross.status_code == 403
    app2.state.settings.store.close()


def test_inflight_reset_after_crash_allows_retry(tmp_path):
    db = str(tmp_path / "crash.db")
    client, app = _open_app(db)
    client.post(
        "/accounts",
        json={"account_id": "acc-c"},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "c0"},
    )

    # 手工模拟：业务执行到一半时登记了 IN_FLIGHT，进程随即崩溃（未 finish）
    settings = app.state.settings
    status, *_ = settings.store.idempotency_begin(
        "tenant-a", "stuck-key", "deadbeef"
    )
    assert status == "NEW"
    settings.store.close()

    # 重启：IN_FLIGHT 残留已清除
    client2, app2 = _open_app(db)
    s2 = app2.state.settings
    row = s2.store.idempotency_get("tenant-a", "stuck-key")
    print_decision(
        "重启-崩溃残留",
        {"Idempotency-Key": "stuck-key"},
        type("R", (), {"status_code": "-",
                        "json": lambda self: {"db行是否存在": bool(row)}})(),
    )
    assert row is None

    # 同键可重新提交（注意用同一请求体哈希内容）
    r = client2.post(
        "/accounts/deposit",
        json={"account_id": "acc-c", "amount": 3},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "stuck-key"},
    )
    print_decision("重启-同键重试成功", {"Idempotency-Key": "stuck-key"}, r)
    assert r.status_code == 200
    assert r.json()["balance"] == 3
    app2.state.settings.store.close()
