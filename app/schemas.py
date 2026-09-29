"""请求/响应 Pydantic 模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateAccountRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)


class DepositRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0)


class AccountResponse(BaseModel):
    tenant_id: str
    account_id: str
    balance: int


class ErrorEnvelope(BaseModel):
    code: str
    status_code: int
    message: str
    detail: dict
