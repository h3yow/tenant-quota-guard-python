"""FastAPI 接入层：把 QuotaGuard 的判定映射为可区分、不泄露内部信息的 HTTP 响应。"""

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from quota_guard import InMemoryQuotaStore, QuotaGuard
from quota_guard.audit import InMemoryAuditSink
from quota_guard.guard import Decision, DegradePolicy

QUOTA_LIMIT = 100
WINDOW_SECONDS = 60.0

audit_sink = InMemoryAuditSink()
guard = QuotaGuard(
    store=InMemoryQuotaStore(),
    sink=audit_sink,
    limit=QUOTA_LIMIT,
    window_seconds=WINDOW_SECONDS,
    policy=DegradePolicy.FAIL_CLOSED,
)

app = FastAPI(title="tenant-quota-guard")

# 判定 reason -> HTTP 状态码：超限 429，身份问题 400，降级拒绝 503。
_STATUS_BY_REASON = {
    "quota_exceeded": 429,
    "missing_identity": 400,
    "invalid_identity": 400,
    "conflicting_identity": 400,
    "degraded_fail_closed": 503,
}


@app.middleware("http")
async def quota_middleware(request: Request, call_next) -> Response:
    if request.url.path == "/healthz":
        return await call_next(request)

    headers = {k.lower(): v for k, v in request.headers.items()}
    query = dict(request.query_params)
    decision: Decision = guard.check(headers, query)

    if not decision.allowed:
        status = _STATUS_BY_REASON.get(decision.reason, 500)
        # 响应只含稳定原因码与重试提示，不含堆栈或跨租户信息。
        body = {"error": decision.reason}
        response = JSONResponse(status_code=status, content=body)
        if decision.reason == "quota_exceeded":
            response.headers["Retry-After"] = f"{decision.retry_after:.0f}"
        return response

    response = await call_next(request)
    if decision.degraded:
        response.headers["X-Quota-Degraded"] = "true"
    return response


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
