"""服务入口：将配额治理层挂载到 FastAPI。

- 启动时即构建并校验 GuardConfig：非法配置直接抛错，服务起不来，
  不会等请求进入才暴露。
- 每个请求先经治理中间件：解析租户身份 -> 配额判定 -> 审计。
- 身份校验被拒的请求同样写入审计留痕。
- 拒绝响应使用可区分的状态码与 reason，超限/后端故障均携带 Retry-After。
- 内部异常一律按生效降级策略处理，不暴露堆栈或跨租户信息。
"""

import logging
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.audit import AuditRecorder
from app.config import DegradationPolicy, GuardConfig, TenantQuotaConfig
from app.errors import QuotaGuardError, RejectReason
from app.guard import QuotaGuard
from app.identity import parse_tenant_identity, tenant_candidates
from app.quota import QuotaCounter

logging.basicConfig(level=logging.INFO)

# 按租户的套餐覆盖：在此登记需要单独上限/窗口/降级策略的租户，
# 未登记的租户沿用默认值。配置在启动时校验（GuardConfig 构造即校验）。
TENANT_OVERRIDES: dict[str, TenantQuotaConfig] = {
    # 示例："tenant-vip": TenantQuotaConfig(limit=1000, window_seconds=60.0,
    #                                       degradation_policy=DegradationPolicy.FAIL_OPEN),
}

# 默认每租户每 60 秒 100 次请求；计数后端故障时默认拒绝（fail-closed），
# 拒绝时提示 1 秒后重试。
guard_config = GuardConfig(
    default_limit=100,
    default_window_seconds=60.0,
    default_policy=DegradationPolicy.FAIL_CLOSED,
    unavailable_retry_after=1.0,
    tenant_overrides=TENANT_OVERRIDES,
)

quota_counter = QuotaCounter(
    limit=guard_config.default_limit,
    window_seconds=guard_config.default_window_seconds,
    tenant_overrides=guard_config.tenant_overrides,
)
audit_recorder = AuditRecorder()
quota_guard = QuotaGuard(quota_counter, audit_recorder, config=guard_config)

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
        # 身份被拒同样留痕：按可解析的租户候选隔离记录，无法解析归入保留键。
        quota_guard.record_identity_rejection(
            tenant_candidates(dict(request.headers), dict(request.query_params)),
            exc.reason.value,
            exc.message,
        )
        return _reject(_REASON_STATUS[exc.reason], exc.reason.value)

    try:
        decision = quota_guard.check(identity, now=time.time())
    except Exception:
        # 兜底：治理层自身未预料异常，仍按该租户生效策略给出判定，语义不漂移。
        logging.getLogger("quota_guard").exception("unexpected guard failure")
        decision = quota_guard.fallback_decision(identity.tenant_id)

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
