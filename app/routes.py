"""业务路由：开户、存款（幂等写）、查询（租户隔离）。

每个写请求必须带 ``Idempotency-Key`` 头；处理顺序为
认证 -> 幂等判定（重放直接返回，不耗配额）-> 配额占用 -> 租户隔离校验 -> 落账。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Request

from app.audit import audit_log
from app.deps import Settings, get_settings, require_tenant
from app.errors import (
    CrossTenantDeniedError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from app.idempotency import run_idempotent
from app.quota import check_and_consume
from app.schemas import CreateAccountRequest, DepositRequest

router = APIRouter()


async def _read_payload(request: Request) -> tuple[bytes, dict]:
    raw = await request.body()
    try:
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError:
        data = {}
    return raw, data


def _request_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.post("/accounts", status_code=201)
async def create_account(
    request: Request,
    tenant_id: str = Depends(require_tenant),
    settings: Settings = Depends(get_settings),
) -> dict:
    raw, data = await _read_payload(request)
    payload = CreateAccountRequest(**data)
    idem_key = request.headers.get("Idempotency-Key")
    audit_log(
        tenant_id, "write", "-", "request_received",
        endpoint="POST /accounts",
        idem_key=idem_key,
        input={"account_id": payload.account_id},
    )

    def business() -> tuple[dict, int]:
        # 新请求才占用配额；重放在 run_idempotent 内直接返回
        limit = settings.quota_limit_for(tenant_id)
        check_and_consume(
            tenant_id, limit, settings.store, settings.window_seconds
        )
        try:
            settings.store.create_account(tenant_id, payload.account_id)
        except Exception:
            # 同租户已建过：幂等意义上的“已存在”
            owner = settings.store.find_account_owner(payload.account_id)
            if owner == tenant_id:
                audit_log(tenant_id, "access", "DENY",
                          "account_exists_same_tenant",
                          account_id=payload.account_id)
                raise ResourceExistsError(
                    f"账户 {payload.account_id} 已存在",
                    detail={"account_id": payload.account_id},
                )
            # 同名账户属于别的租户：不能让对方猜到/占用，按越权拒绝
            audit_log(tenant_id, "access", "DENY",
                      "account_id_owned_by_other_tenant",
                      account_id=payload.account_id, owner=owner)
            raise CrossTenantDeniedError(
                "不能在其他租户的账户标识上执行操作",
                detail={"account_id": payload.account_id},
            )
        return (
            {
                "tenant_id": tenant_id,
                "account_id": payload.account_id,
                "balance": 0,
            },
            201,
        )

    body, status_code, is_replay = run_idempotent(
        tenant_id, idem_key, _request_hash(raw), settings.store, business
    )
    body["replayed"] = is_replay
    return _with_status(body, status_code)


@router.post("/accounts/deposit")
async def deposit(
    request: Request,
    tenant_id: str = Depends(require_tenant),
    settings: Settings = Depends(get_settings),
) -> dict:
    raw, data = await _read_payload(request)
    payload = DepositRequest(**data)
    idem_key = request.headers.get("Idempotency-Key")
    audit_log(
        tenant_id, "write", "-", "request_received",
        endpoint="POST /accounts/deposit",
        idem_key=idem_key,
        input={"account_id": payload.account_id, "amount": payload.amount},
    )

    def business() -> tuple[dict, int]:
        limit = settings.quota_limit_for(tenant_id)
        check_and_consume(
            tenant_id, limit, settings.store, settings.window_seconds
        )
        owner = settings.store.find_account_owner(payload.account_id)
        if owner is None:
            audit_log(tenant_id, "access", "DENY", "account_not_found",
                      account_id=payload.account_id)
            raise ResourceNotFoundError(
                f"账户 {payload.account_id} 不存在",
                detail={"account_id": payload.account_id},
            )
        if owner != tenant_id:
            audit_log(tenant_id, "access", "DENY",
                      "cross_tenant_write_denied",
                      account_id=payload.account_id, owner=owner)
            raise CrossTenantDeniedError(
                "不能向其他租户的账户存款",
                detail={"account_id": payload.account_id, "owner": owner},
            )
        balance = settings.store.deposit(
            tenant_id, payload.account_id, payload.amount
        )
        return (
            {
                "tenant_id": tenant_id,
                "account_id": payload.account_id,
                "balance": balance,
            },
            200,
        )

    body, status_code, is_replay = run_idempotent(
        tenant_id, idem_key, _request_hash(raw), settings.store, business
    )
    body["replayed"] = is_replay
    return _with_status(body, status_code)


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    tenant_id: str = Depends(require_tenant),
    settings: Settings = Depends(get_settings),
) -> dict:
    owner = settings.store.find_account_owner(account_id)
    if owner is None:
        audit_log(tenant_id, "access", "DENY", "account_not_found",
                  account_id=account_id)
        raise ResourceNotFoundError(
            f"账户 {account_id} 不存在", detail={"account_id": account_id}
        )
    if owner != tenant_id:
        audit_log(tenant_id, "access", "DENY", "cross_tenant_read_denied",
                  account_id=account_id, owner=owner)
        raise CrossTenantDeniedError(
            "不能读取其他租户的账户",
            detail={"account_id": account_id, "owner": owner},
        )
    balance = settings.store.get_balance(tenant_id, account_id)
    audit_log(tenant_id, "access", "ALLOW", "owner_match",
              account_id=account_id)
    return {"tenant_id": tenant_id, "account_id": account_id,
            "balance": balance}


def _with_status(body: dict, status_code: int):
    """让 FastAPI 返回与首次一致的状态码（重放 201 仍为 201）。"""
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=body)
