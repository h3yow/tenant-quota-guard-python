"""服务入口：将配额治理层挂载到 FastAPI。

- 启动时从环境变量加载配置并立即校验（非法配置直接启动失败）：
  - QUOTA_DEFAULT_LIMIT / QUOTA_DEFAULT_WINDOW / QUOTA_DEFAULT_POLICY
  - QUOTA_TENANT_OVERRIDES：JSON，如
    {"tenant-vip": {"limit": 1000, "window_seconds": 60, "policy": "fail_open"}}
- 每个请求先经治理层：身份校验 -> 配额判定 -> 审计（身份被拒也留审计）。
- 拒绝响应使用可区分的状态码与 reason，超限/后端不可用携带 Retry-After。
- 内部异常一律转为通用 503，不暴露堆栈或跨租户信息。
"""

import json
import logging
import os
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.audit import AuditRecorder
from app.config import DegradationPolicy, QuotaSettings, TenantQuotaRule
from app.errors import RejectReason
from app.guard import QuotaGuard
from app.quota import QuotaCounter

logging.basicConfig(level=logging.INFO)


def load_settings() -> QuotaSettings:
    """从环境变量加载配置；任何非法值在此直接抛错，服务启动即失败。"""
    overrides_raw = os.environ.get("QUOTA_TENANT_OVERRIDES", "{}")
    overrides = {
        tenant_id: TenantQuotaRule(
            limit=int(rule["limit"]),
            window_seconds=float(rule["window_seconds"]),
            policy=DegradationPolicy(rule["policy"]) if rule.get("policy") else None,
        )
        for tenant_id, rule in json.loads(overrides_raw).items()
    }
    return QuotaSettings(
        default_limit=int(os.environ.get("QUOTA_DEFAULT_LIMIT", "100")),
        default_window_seconds=float(os.environ.get("QUOTA_DEFAULT_WINDOW", "60")),
        default_policy=DegradationPolicy(os.environ.get("QUOTA_DEFAULT_POLICY", "fail_closed")),
        tenant_overrides=overrides,
    )


# 默认每租户每 60 秒 100 次请求；计数后端故障时默认拒绝（fail-closed）。
quota_settings = load_settings()
quota_counter = QuotaCounter(settings=quota_settings)
audit_recorder = AuditRecorder()
quota_guard = QuotaGuard(quota_counter, audit_recorder, settings=quota_settings)

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
        decision = quota_guard.check_request(
            dict(request.headers), dict(request.query_params), now=time.time()
        )
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
