"""并发重复提交：同键同体只生效一次；同键异体必须冲突。"""

from __future__ import annotations

import threading

from tests.conftest import KEY_A, KEY_B, print_decision

DEPOSIT_URL = "/accounts/deposit"


def _create_account(client, account_id: str, key: str) -> None:
    r = client.post(
        "/accounts",
        json={"account_id": account_id},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": key},
    )
    assert r.status_code == 201, r.text


def test_concurrent_same_key_applies_once(client):
    _create_account(client, "acc-conc", "setup-conc")
    body = {"account_id": "acc-conc", "amount": 50}
    headers = {"X-API-Key": KEY_A, "Idempotency-Key": "dep-conc-1"}

    print_decision(
        "幂等-并发输入",
        {"threads": 20, "body": body, "Idempotency-Key": "dep-conc-1"},
        type("R", (), {"status_code": "-", "json": lambda self: {"判定依据": "同键同体，只允许首任者落账，其余等待后重放"}})(),
    )

    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()  # 尽量让 20 个请求同时进入
        r = client.post(DEPOSIT_URL, json=body, headers=headers)
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 全部 200，且余额都应是首次结果 50（不是 50*N）
    assert len(results) == 20
    statuses = {s for s, _ in results}
    balances = {b["balance"] for _, b in results}
    replayed = [b.get("replayed") for _, b in results]
    print("并发返回状态集合:", statuses, "余额集合:", balances,
          "重放标记:", replayed)
    assert statuses == {200}
    assert balances == {50}
    assert replayed.count(False) == 1   # 恰好一个首任者
    assert replayed.count(True) == 19   # 其余 19 个重放首次结果

    # 最终账户余额只能被加一次
    final = client.get("/accounts/acc-conc", headers={"X-API-Key": KEY_A})
    assert final.json()["balance"] == 50


def test_same_key_different_body_conflicts(client):
    _create_account(client, "acc-diff", "setup-diff")
    h = {"X-API-Key": KEY_A, "Idempotency-Key": "dep-diff"}

    r1 = client.post(DEPOSIT_URL, json={"account_id": "acc-diff", "amount": 10}, headers=h)
    print_decision("幂等-首次", {"body": {"amount": 10}, "key": "dep-diff"}, r1)
    assert r1.status_code == 200

    # 同一键、不同金额 -> 冲突，且不得落第二笔账
    r2 = client.post(DEPOSIT_URL, json={"account_id": "acc-diff", "amount": 999}, headers=h)
    print_decision("幂等-异体冲突", {"body": {"amount": 999}, "key": "dep-diff"}, r2)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    final = client.get("/accounts/acc-diff", headers={"X-API-Key": KEY_A})
    assert final.json()["balance"] == 10  # 999 绝不能入账


def test_concurrent_same_key_one_then_replay(client):
    _create_account(client, "acc-c2", "setup-c2")
    body = {"account_id": "acc-c2", "amount": 7}
    headers = {"X-API-Key": KEY_A, "Idempotency-Key": "dep-c2"}
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        r = client.post(DEPOSIT_URL, json=body, headers=headers)
        results.append((r.status_code, r.json()))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    balances = [b["balance"] for _, b in results]
    print("并发重放各请求拿到的余额:", balances)
    assert balances == [7] * 8
    assert client.get("/accounts/acc-c2", headers={"X-API-Key": KEY_B}).status_code == 403
