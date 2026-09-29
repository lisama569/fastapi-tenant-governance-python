"""场景二：配额边界 —— 窗口内计数并发准确，超限可区分、可恢复，重启后状态可预期。"""
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from conftest import TENANT_A, TENANT_B
from main import create_app
from tenant_governance import quota
from tenant_governance.models import Tenant
from tenant_governance.service import GovernanceService
from tenant_governance.store import Store

LIMIT = TENANT_A.quota_limit


def _create(env, tenant, key):
    return env.client.post("/documents", json={"title": key}, headers=env.headers(tenant, idem_key=key))


def test_quota_boundary_exact_limit(env):
    print(f"\n请求输入: 连续 POST /documents x{LIMIT + 1}，租户限额={LIMIT}")
    for i in range(LIMIT):
        r = _create(env, TENANT_A, f"q-{i}")
        assert r.status_code == 201, f"第 {i + 1} 次请求应在额度内"
    over = _create(env, TENANT_A, "q-over")
    print(f"判定结果: 第 {LIMIT + 1} 次 status={over.status_code} body={over.json()} "
          f"Retry-After={over.headers.get('Retry-After')}")
    assert over.status_code == 429
    assert over.json()["error"]["code"] == "QUOTA_EXCEEDED"
    retry_after = int(over.headers["Retry-After"])
    assert 1 <= retry_after <= quota.WINDOW_SECONDS, "必须给出明确的恢复等待时间"
    env.dump_audit("配额边界")


def test_quota_concurrent_burst_no_overcommit(env):
    burst = LIMIT * 4
    print(f"\n请求输入: 并发 POST /documents x{burst}，租户限额={LIMIT}")

    def submit(i):
        return _create(env, TENANT_A, f"burst-{i}").status_code

    with ThreadPoolExecutor(max_workers=burst) as pool:
        statuses = list(pool.map(submit, range(burst)))

    allowed = statuses.count(201)
    rejected = statuses.count(429)
    print(f"判定结果: 201x{allowed} 429x{rejected}")
    assert allowed == LIMIT, "并发突发下放行数必须恰好等于限额，不得多放"
    assert rejected == burst - LIMIT
    env.dump_audit("并发突发配额")


def test_quota_recovers_after_window(env, monkeypatch):
    real_time = time.time()
    monkeypatch.setattr(quota.time, "time", lambda: real_time)
    print(f"\n请求输入: 窗口内打满 {LIMIT} 次后，时间推进到下一窗口再请求")
    for i in range(LIMIT):
        assert _create(env, TENANT_A, f"w-{i}").status_code == 201
    assert _create(env, TENANT_A, "w-over").status_code == 429

    monkeypatch.setattr(quota.time, "time", lambda: real_time + quota.WINDOW_SECONDS + 1)
    r = _create(env, TENANT_A, "w-next-window")
    print(f"判定结果: 窗口结束后 status={r.status_code}")
    assert r.status_code == 201, "窗口结束后应自动恢复"
    env.dump_audit("窗口自动恢复")


def test_quota_isolated_between_tenants(env):
    print(f"\n请求输入: 租户A打满额度后，租户B继续请求")
    for i in range(LIMIT):
        assert _create(env, TENANT_A, f"iso-{i}").status_code == 201
    assert _create(env, TENANT_A, "iso-over").status_code == 429
    r = _create(env, TENANT_B, "iso-b")
    print(f"判定结果: 租户B status={r.status_code}")
    assert r.status_code == 201, "租户额度互相独立"
    env.dump_audit("租户额度隔离")


def test_state_survives_restart(tmp_path):
    """重启（新建 Store 打开同一文件）后：配额计数保留、幂等记录可重放。"""
    db = str(tmp_path / "gov.db")
    tenant = Tenant("tenant-r", "key-r", 2)

    store1 = Store(db)
    store1.upsert_tenant(tenant)
    service1 = GovernanceService(store1)
    client1 = TestClient(create_app(service1))
    h = {"X-Api-Key": "key-r", "Idempotency-Key": "persist-key"}
    payload = {"title": "重启前"}
    r1 = client1.post("/documents", json=payload, headers=h)
    assert r1.status_code == 201
    print(f"\n请求输入: 重启前创建 payload={payload} -> {r1.status_code} doc={r1.json()['doc_id']}")
    store1.close()

    store2 = Store(db)  # 模拟服务重启
    service2 = GovernanceService(store2)
    client2 = TestClient(create_app(service2))

    replay = client2.post("/documents", json=payload, headers=h)
    print(f"判定结果: 重启后重放 status={replay.status_code} body_equal={replay.json() == r1.json()}")
    assert replay.status_code == 201 and replay.json() == r1.json(), "重启后同键同载荷必须重放首次结果"

    # 重启前已用 1/2，重启后再用 1 次即打满，第 3 次超限
    assert client2.post("/documents", json={"title": "2"}, headers={"X-Api-Key": "key-r", "Idempotency-Key": "k2"}).status_code == 201
    over = client2.post("/documents", json={"title": "3"}, headers={"X-Api-Key": "key-r", "Idempotency-Key": "k3"})
    print(f"判定结果: 重启后配额计数延续，第 3 次 status={over.status_code}")
    assert over.status_code == 429, "重启后配额窗口计数必须保留"
    store2.close()
