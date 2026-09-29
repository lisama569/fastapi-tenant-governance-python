import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from tenant_governance.service import GovernanceError, GovernanceService
from tenant_governance.store import Store


def create_app(service: GovernanceService) -> FastAPI:
    app = FastAPI(title="Tenant Governance Service")

    @app.exception_handler(GovernanceError)
    def governance_error_handler(_request: Request, exc: GovernanceError) -> JSONResponse:
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "detail": exc.detail}},
            headers=headers,
        )

    @app.get("/")
    def read_root():
        return {"Hello": "World"}

    @app.post("/documents", status_code=201)
    def create_document(payload: dict, request: Request):
        headers: Headers = request.headers
        status, body = service.create_document(
            headers.get("x-api-key"), headers.get("idempotency-key"), payload
        )
        return JSONResponse(status_code=status, content=body)

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: str, request: Request):
        return service.get_document(request.headers.get("x-api-key"), doc_id)

    return app


store = Store()
service = GovernanceService(store)
app = create_app(service)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
