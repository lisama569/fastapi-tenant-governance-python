"""场景一：并发重复提交 —— 同一幂等键并发重放只能生效一次。"""
from concurrent.futures import ThreadPoolExecutor

from conftest import TENANT_A

WORKERS = 16


def test_concurrent_same_key_same_payload_effects_once(env):
    payload = {"title": "并发文档", "body": "hello"}
    headers = env.headers(TENANT_A, idem_key="concurrent-key")
    print(f"\n请求输入: POST /documents payload={payload} headers={headers} 并发度={WORKERS}")

    def submit(_):
        r = env.client.post("/documents", json=payload, headers=headers)
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(submit, range(WORKERS)))

    statuses = [s for s, _ in results]
    bodies = [b for _, b in results]
    print(f"判定结果: statuses={statuses}")
    assert all(s == 201 for s in statuses), "同键同载荷的并发重放都应拿到首次结果"
    assert len({b["doc_id"] for b in bodies}) == 1, "所有并发请求必须落到同一个文档"

    # 只落账一次：文档数与配额消耗都为 1
    rows = env.store._conn.execute("SELECT COUNT(*) c FROM documents WHERE tenant_id='tenant-a'").fetchone()
    assert rows["c"] == 1, "并发重放不得重复落账"
    quota_rows = env.store._conn.execute(
        "SELECT used FROM quota_windows WHERE tenant_id='tenant-a'").fetchall()
    assert sum(r["used"] for r in quota_rows) == 1, "重放不得重复消耗配额"
    env.dump_audit("并发同键同载荷")


def test_concurrent_same_key_different_payload_single_winner(env):
    headers = env.headers(TENANT_A, idem_key="race-key")
    print(f"\n请求输入: POST /documents 同一 Idempotency-Key 不同载荷 并发度={WORKERS}")

    def submit(i):
        r = env.client.post("/documents", json={"title": f"payload-{i}"}, headers=headers)
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(submit, range(WORKERS)))

    created = [b for s, b in results if s == 201]
    conflicts = [b for s, b in results if s == 409]
    print(f"判定结果: 201x{len(created)} 409x{len(conflicts)}")
    assert len(created) == 1, "并发冲突下只能有一个请求生效"
    assert len(created) + len(conflicts) == WORKERS
    assert all(b["error"]["code"] == "IDEMPOTENCY_CONFLICT" for b in conflicts)

    rows = env.store._conn.execute("SELECT COUNT(*) c FROM documents WHERE tenant_id='tenant-a'").fetchone()
    assert rows["c"] == 1, "冲突竞争不得产生重复落账"
    env.dump_audit("并发同键不同载荷")


def test_sequential_replay_returns_identical_result(env):
    payload = {"title": "顺序重放", "body": "x"}
    headers = env.headers(TENANT_A, idem_key="seq-key")
    print(f"\n请求输入: 两次顺序 POST /documents payload={payload}")
    r1 = env.client.post("/documents", json=payload, headers=headers)
    r2 = env.client.post("/documents", json=payload, headers=headers)
    print(f"判定结果: first={r1.status_code} replay={r2.status_code} body_equal={r1.json() == r2.json()}")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json(), "重复提交必须返回与首次一致的结果"
    env.dump_audit("顺序重放")
