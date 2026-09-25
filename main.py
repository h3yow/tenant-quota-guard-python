"""服务入口：将配额治理层挂载到 FastAPI。

- 每个请求先经治理依赖：解析租户身份 -> 配额判定 -> 审计。
- 拒绝响应使用可区分的状态码与 reason，超限携带 Retry-After。
- 内部异常一律转为通用 500，不暴露堆栈或跨租户信息。
"""

import logging
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.audit import AuditRecorder
from app.errors import QuotaGuardError, RejectReason
from app.guard import DegradationPolicy, QuotaGuard
from app.identity import parse_tenant_identity
from app.quota import QuotaCounter

logging.basicConfig(level=logging.INFO)

# 每租户每 60 秒窗口最多 100 次请求；计数后端故障时默认拒绝（fail-closed）。
quota_counter = QuotaCounter(limit=100, window_seconds=60.0)
audit_recorder = AuditRecorder()
quota_guard = QuotaGuard(quota_counter, audit_recorder, policy=DegradationPolicy.FAIL_CLOSED)

app = FastAPI()

_REASON_STATUS = {
    RejectReason.TENANT_MISSING: 401,
    RejectReason.TENANT_INVALID: 400,
    RejectReason.TENANT_CONFLICT: 409,
    RejectReason.QUOTA_EXCEEDED: 429,
    RejectReason.QUOTA_UNAVAILABLE: 503,
}


def _reject(status: int, reason: str, retry_after: float | None = None) -> JSONResponse:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
    return JSONResponse(
        status_code=status,
        content={"error": {"reason": reason, "retry_after": retry_after}},
        headers=headers,
    )


@app.middleware("http")
async def quota_guard_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    try:
        identity = parse_tenant_identity(dict(request.headers), dict(request.query_params))
    except QuotaGuardError as exc:
        return _reject(_REASON_STATUS[exc.reason], exc.reason.value)

    try:
        decision = quota_guard.check(identity, now=time.time())
    except Exception:
        # 兜底：内部异常不外泄，按 fail-closed 拒绝。
        logging.getLogger("quota_guard").exception("unexpected guard failure")
        return _reject(503, RejectReason.QUOTA_UNAVAILABLE.value)

    if not decision.allowed:
        status = _REASON_STATUS.get(RejectReason(decision.reason), 503)
        return _reject(status, decision.reason, decision.retry_after)

    response = await call_next(request)
    if decision.degraded:
        response.headers["X-Quota-Degraded"] = "true"
    return response


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
