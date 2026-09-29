"""跨租户隔离：即使拿到对方的 account_id，也不能读、不能写、不能重复建。"""

from __future__ import annotations

from tests.conftest import KEY_A, KEY_B, print_decision


def _setup_account(client, account_id: str, idem: str) -> None:
    r = client.post(
        "/accounts",
        json={"account_id": account_id},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": idem},
    )
    assert r.status_code == 201, r.text
    client.post(
        "/accounts/deposit",
        json={"account_id": account_id, "amount": 100},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": idem + "-dep"},
    )


def test_cannot_read_other_tenant_account(client):
    _setup_account(client, "secret-acc", "iso-setup")
    r = client.get("/accounts/secret-acc", headers={"X-API-Key": KEY_B})
    print_decision(
        "隔离-越权读",
        {"tenant": "tenant-b", "资源": "secret-acc(属 tenant-a)"},
        r,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CROSS_TENANT_DENIED"
    assert r.json()["error"]["detail"]["owner"] == "tenant-a"


def test_cannot_deposit_to_other_tenant_account(client):
    _setup_account(client, "secret-acc2", "iso2-setup")
    r = client.post(
        "/accounts/deposit",
        json={"account_id": "secret-acc2", "amount": 999999},
        headers={"X-API-Key": KEY_B, "Idempotency-Key": "iso2-attack"},
    )
    print_decision(
        "隔离-越权写",
        {"tenant": "tenant-b", "body": {"account_id": "secret-acc2", "amount": 999999}},
        r,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CROSS_TENANT_DENIED"

    # owner 视角余额仍是 100，攻击金额未入账
    owner_view = client.get("/accounts/secret-acc2", headers={"X-API-Key": KEY_A})
    print_decision("隔离-owner核对", {"tenant": "tenant-a"}, owner_view)
    assert owner_view.json()["balance"] == 100


def test_cannot_recreate_account_id_owned_by_other(client):
    _setup_account(client, "shared-name", "iso3-setup")
    r = client.post(
        "/accounts",
        json={"account_id": "shared-name"},
        headers={"X-API-Key": KEY_B, "Idempotency-Key": "iso3-grab"},
    )
    print_decision(
        "隔离-抢占他人账户标识",
        {"tenant": "tenant-b", "account_id": "shared-name"},
        r,
    )
    # 同名资源属于别的租户：按越权拒绝，而不是泄露“已存在”
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CROSS_TENANT_DENIED"


def test_owner_access_allowed_but_stranger_denied(client):
    _setup_account(client, "iso4", "iso4-setup")
    ok = client.get("/accounts/iso4", headers={"X-API-Key": KEY_A})
    no = client.get("/accounts/iso4", headers={"X-API-Key": KEY_B})
    print_decision("隔离-owner可读", {"tenant": "tenant-a"}, ok)
    print_decision("隔离-他租户被拒", {"tenant": "tenant-b"}, no)
    assert ok.status_code == 200
    assert no.status_code == 403


def test_nonexistent_account_is_404_not_403(client):
    r = client.get("/accounts/never-exists", headers={"X-API-Key": KEY_A})
    print_decision("隔离-不存在资源", {"tenant": "tenant-a"}, r)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
