"""固定时间窗配额：边界放行/拒绝、并发计数准确、窗口滚动自动恢复。"""

from __future__ import annotations

import threading

from tests.conftest import KEY_A, KEY_B, print_decision


def _deposit(client, account_id, amount, idem, key=KEY_A):
    return client.post(
        "/accounts/deposit",
        json={"account_id": account_id, "amount": amount},
        headers={"X-API-Key": key, "Idempotency-Key": idem},
    )


def test_quota_boundary_and_recovery(client, clock):
    # 开户用 1 次配额，之后 tenant-a 限额 5
    r0 = client.post(
        "/accounts",
        json={"account_id": "acc-q"},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "q0"},
    )
    assert r0.status_code == 201

    # 第 2..5 次：恰好再放 4 个，共 5 个
    for i in range(4):
        r = _deposit(client, "acc-q", 1, f"qd{i}")
        print_decision("配额-窗口内", {"第": i + 2, "次": "请求"}, r)
        assert r.status_code == 200, r.text

    # 第 6 次：超限 429，必须带恢复等待秒数
    r_limited = _deposit(client, "acc-q", 1, "q-over")
    print_decision("配额-超限", {"第": 6, "次": "请求", "窗口": 60}, r_limited)
    assert r_limited.status_code == 429
    err = r_limited.json()["error"]
    assert err["code"] == "QUOTA_EXCEEDED"
    assert err["detail"]["retry_after_seconds"] >= 1
    assert err["detail"]["window_count"] == 5

    # 余额未被超限请求改动（前面只成功 4 笔 1 元 + 初始 0）
    bal = client.get("/accounts/acc-q", headers={"X-API-Key": KEY_A})
    assert bal.json()["balance"] == 4

    # 窗口结束：自动恢复
    clock.advance(61)
    r_recover = _deposit(client, "acc-q", 1, "q-recovered")
    print_decision("配额-窗口滚动恢复", {"clock": clock.now}, r_recover)
    assert r_recover.status_code == 200
    assert r_recover.json()["balance"] == 5


def test_concurrent_quota_does_not_oversell(client):
    client.post(
        "/accounts",
        json={"account_id": "acc-cq"},
        headers={"X-API-Key": KEY_B, "Idempotency-Key": "cq0"},
    )
    # tenant-b 限额 3：已用 1，本窗口只剩 2
    outcomes: list[int] = []
    barrier = threading.Barrier(10)

    def worker(i: int) -> None:
        barrier.wait()
        r = _deposit(client, "acc-cq", 1, f"cq-{i}", key=KEY_B)
        outcomes.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = outcomes.count(200)
    denied = outcomes.count(429)
    print(f"[配额-并发] 10 并发：放行 {allowed}，429 拒绝 {denied}（期望 2 / 8）")
    print_decision(
        "配额-并发结论",
        {"并发": 10, "剩余额度": 2},
        type("R", (), {"status_code": allowed, "json": lambda self: {"放行": allowed, "拒绝": denied}})(),
    )
    assert allowed == 2
    assert denied == 8


def test_overlimit_429_distinct_from_cross_tenant_403(client):
    # tenant-a 用满 5 次
    client.post(
        "/accounts",
        json={"account_id": "acc-x"},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "x0"},
    )
    for i in range(4):
        assert _deposit(client, "acc-x", 1, f"xd{i+1}").status_code == 200
    over = _deposit(client, "acc-x", 1, "x-over")
    cross = _deposit(client, "acc-x", 1, "b-to-a", key=KEY_B)

    print_decision("结论区分-超限", {"tenant": "tenant-a"}, over)
    print_decision("结论区分-越权", {"tenant": "tenant-b 访问 tenant-a 资源"}, cross)
    assert over.status_code == 429
    assert over.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert cross.status_code == 403
    assert cross.json()["error"]["code"] == "CROSS_TENANT_DENIED"


def test_replay_does_not_consume_quota(client):
    client.post(
        "/accounts",
        json={"account_id": "acc-r"},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "r0"},
    )
    # 用 4 次（共 5 次用满）
    assert _deposit(client, "acc-r", 1, "shared-key-1").status_code == 200
    for i in range(3):
        assert _deposit(client, "acc-r", 1, f"r{i+2}").status_code == 200
    # 已用满；但同键重放不耗配额，应仍返回首次结果而非 429
    replay = _deposit(client, "acc-r", 1, "shared-key-1")
    print_decision("配额-重放不耗额", {"Idempotency-Key": "shared-key-1"}, replay)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    fresh = _deposit(client, "acc-r", 1, "fresh-after-full")
    print_decision("配额-新请求仍超限", {"Idempotency-Key": "fresh-after-full"}, fresh)
    assert fresh.status_code == 429
