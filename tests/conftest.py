import logging

import pytest
from fastapi.testclient import TestClient

from main import create_app
from tenant_governance.models import Tenant
from tenant_governance.service import GovernanceService
from tenant_governance.store import Store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

TENANT_A = Tenant(tenant_id="tenant-a", api_key="key-a", quota_limit=5)
TENANT_B = Tenant(tenant_id="tenant-b", api_key="key-b", quota_limit=5)


class Env:
    def __init__(self, store: Store, service: GovernanceService, client: TestClient):
        self.store = store
        self.service = service
        self.client = client

    def headers(self, tenant: Tenant, idem_key: str | None = None) -> dict:
        h = {"X-Api-Key": tenant.api_key}
        if idem_key is not None:
            h["Idempotency-Key"] = idem_key
        return h

    def dump_audit(self, label: str) -> None:
        """打印判定依据，满足“单测日志需打印请求输入与判定依据”的要求。"""
        print(f"\n===== 审计日志 [{label}] =====")
        for e in self.service.audit_log:
            print(f"tenant={e.tenant_id} action={e.action} decision={e.decision} "
                  f"reason={e.reason} extra={e.extra}")


@pytest.fixture
def env():
    store = Store()
    store.upsert_tenant(TENANT_A)
    store.upsert_tenant(TENANT_B)
    service = GovernanceService(store)
    client = TestClient(create_app(service))
    yield Env(store, service, client)
    store.close()
