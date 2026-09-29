"""FastAPI 应用入口：装配设置、注册异常处理与路由。"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import DB_PATH
from app.deps import Settings
from app.errors import GovernanceError
from app.routes import router


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="Multi-Tenant Governance Service")
    path = db_path or os.environ.get("TENANT_GOV_DB", DB_PATH)
    app.state.settings = Settings.create(path)

    @app.exception_handler(GovernanceError)
    async def _on_governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_response())

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "status_code": 422,
                    "message": "请求参数不合法",
                    "detail": {"errors": exc.errors()},
                }
            },
        )

    app.include_router(router)
    return app
