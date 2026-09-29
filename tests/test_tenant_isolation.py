"""场景三：跨租户越权与凭据治理 —— 失败原因可区分，越权不泄露资源归属。"""
from conftest import TENANT_A, TENANT_B
from tenant_governance.models import Tenant


def test_credential_failures_are_distinguishable(env):
    print("\n请求输入: 分别使用 缺失/伪造/已吊销 凭据访问")
    env.store.upsert_tenant(Tenant("tenant-c", "key-c", 5, revoked=True))

    missing = env.client.get("/documents/whatever")
    forged = env.client.get("/documents/whatever", headers={"X-Api-Key": "forged-key"})
    revoked = env.client.get("/documents/whatever", headers={"X-Api-Key": "key-c"})

    codes = (
        missing.json()["error"]["code"],
        forged.json()["error"]["code"],
        revoked.json()["error"]["code"],
    )
    print(f"判定结果: missing={missing.status_code}/{codes[0]} "
          f"forged={forged.status_code}/{codes[1]} revoked={revoked.status_code}/{codes[2]}")
    assert codes == ("TENANT_CREDENTIAL_MISSING", "TENANT_CREDENTIAL_INVALID", "TENANT_CREDENTIAL_REVOKED")
    assert len(set(codes)) == 3, "三种凭据失败原因必须可区分"
    assert missing.status_code == forged.status_code == revoked.status_code == 401
    env.dump_audit("凭据失败区分")


def test_cross_tenant_read_is_denied_as_not_found(env):
    create = env.client.post("/documents", json={"title": "A的机密"},
                             headers=env.headers(TENANT_A, idem_key="secret-doc"))
    doc_id = create.json()["doc_id"]
    print(f"\n请求输入: 租户B持租户A的资源标识 GET /documents/{doc_id}")

    cross = env.client.get(f"/documents/{doc_id}", headers=env.headers(TENANT_B))
    print(f"判定结果: status={cross.status_code} body={cross.json()}")
    assert cross.status_code == 404
    assert cross.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "tenant" not in cross.text.lower() or "tenant-a" not in cross.text, "不得泄露资源归属"

    own = env.client.get(f"/documents/{doc_id}", headers=env.headers(TENANT_A))
    assert own.status_code == 200 and own.json()["doc_id"] == doc_id
    env.dump_audit("跨租户读取")


def test_unauthorized_vs_quota_exceeded_are_distinguishable(env):
    """越权(404/401)与超限(429)的结论必须可区分。"""
    limit = TENANT_A.quota_limit
    for i in range(limit):
        r = env.client.post("/documents", json={"title": f"d{i}"},
                            headers=env.headers(TENANT_A, idem_key=f"dq-{i}"))
        assert r.status_code == 201
    doc_id = r.json()["doc_id"]

    over_quota = env.client.post("/documents", json={"title": "x"},
                                 headers=env.headers(TENANT_A, idem_key="dq-over"))
    cross_tenant = env.client.get(f"/documents/{doc_id}", headers=env.headers(TENANT_B))
    no_cred = env.client.get(f"/documents/{doc_id}")

    verdicts = {
        "over_quota": over_quota.json()["error"]["code"],
        "cross_tenant": cross_tenant.json()["error"]["code"],
        "no_credential": no_cred.json()["error"]["code"],
    }
    print(f"\n判定结果: {verdicts}")
    assert verdicts["over_quota"] == "QUOTA_EXCEEDED"
    assert verdicts["cross_tenant"] == "RESOURCE_NOT_FOUND"
    assert verdicts["no_credential"] == "TENANT_CREDENTIAL_MISSING"
    assert len(set(verdicts.values())) == 3, "越权、超限、无凭据的结论必须两两可区分"
    env.dump_audit("越权与超限区分")


def test_idempotency_keys_are_tenant_scoped(env):
    """同一幂等键在不同租户下互不影响，不能借键串数据。"""
    print("\n请求输入: 租户A与租户B使用相同 Idempotency-Key 提交不同内容")
    r_a = env.client.post("/documents", json={"title": "A的内容"},
                          headers=env.headers(TENANT_A, idem_key="shared-key"))
    r_b = env.client.post("/documents", json={"title": "B的内容"},
                          headers=env.headers(TENANT_B, idem_key="shared-key"))
    print(f"判定结果: A={r_a.status_code} B={r_b.status_code} "
          f"doc_id不同={r_a.json()['doc_id'] != r_b.json()['doc_id']}")
    assert r_a.status_code == r_b.status_code == 201
    assert r_a.json()["doc_id"] != r_b.json()["doc_id"], "幂等键按租户隔离，互不重放对方结果"
    env.dump_audit("幂等键租户隔离")
