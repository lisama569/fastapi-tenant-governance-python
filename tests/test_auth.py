"""租户识别：凭据缺失/失效/伪造三类原因必须可区分。"""

from __future__ import annotations

from tests.conftest import KEY_A, KEY_DISABLED, print_decision


def test_missing_api_key_rejected(client):
    r = client.get("/accounts/whatever")
    print_decision("认证-缺失", {"headers": {"X-API-Key": None}}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_CREDENTIALS"


def test_malformed_key_is_forged(client):
    r = client.get("/accounts/x", headers={"X-API-Key": "not-a-valid-key"})
    print_decision("认证-格式伪造", {"X-API-Key": "not-a-valid-key"}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "FORGED_CREDENTIAL"


def test_wrong_secret_is_forged(client):
    r = client.get(
        "/accounts/x",
        headers={"X-API-Key": "tk_tenant-a.WRONGsecret00000000000000"},
    )
    print_decision("认证-密钥伪造", {"X-API-Key": "tk_tenant-a.<wrong>"}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "FORGED_CREDENTIAL"


def test_unknown_tenant_is_forged(client):
    r = client.get(
        "/accounts/x",
        headers={"X-API-Key": "tk_ghost.S2u9Vq7XmK1pR4wL"},
    )
    print_decision("认证-租户不存在", {"X-API-Key": "tk_ghost.<k>"}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "FORGED_CREDENTIAL"


def test_disabled_tenant_key_is_invalid_not_forged(client):
    # 凭据本身格式/密钥正确，但租户停用 -> 失效，而非伪造
    r = client.get("/accounts/x", headers={"X-API-Key": KEY_DISABLED})
    print_decision("认证-租户停用", {"X-API-Key": "<disabled tenant key>"}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_expired_key_is_invalid(client, clock):
    r1 = client.post(
        "/accounts",
        json={"account_id": "exp-acc"},
        headers={
            "X-API-Key": "tk_tenant-expired.P9qL1mW4xR7sT2vY",
            "Idempotency-Key": "exp-1",
        },
    )
    print_decision("认证-未过期可用", {"tenant": "tenant-expired", "clock": clock.now}, r1)
    assert r1.status_code == 201

    clock.advance(60)  # 越过 expires_at
    r2 = client.get(
        "/accounts/exp-acc",
        headers={"X-API-Key": "tk_tenant-expired.P9qL1mW4xR7sT2vY"},
    )
    print_decision("认证-过期失效", {"tenant": "tenant-expired", "clock": clock.now}, r2)
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_valid_key_passes(client):
    r = client.post(
        "/accounts",
        json={"account_id": "acc-ok"},
        headers={"X-API-Key": KEY_A, "Idempotency-Key": "ok-1"},
    )
    print_decision("认证-合法", {"X-API-Key": "<tenant-a key>"}, r)
    assert r.status_code == 201
