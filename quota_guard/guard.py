"""配额判定编排：身份 -> 计数 -> 审计，含异常降级策略。

降级策略（DegradePolicy）——治理组件内部失败（计数后端异常、审计写入
失败）时如何给出最终判定：
- FAIL_CLOSED：拒绝请求，reason 为 "degraded_fail_closed"；
- FAIL_OPEN：放行请求，reason 为 "degraded_fail_open"。

内部异常只进入日志与审计 detail，绝不把堆栈或跨租户信息放进返回给
调用方的 Decision；所有异常都会被记录，不会静默吞掉。
"""

import logging
from dataclasses import dataclass, replace
from enum import Enum

from quota_guard.audit import AuditRecord, AuditSink
from quota_guard.errors import IdentityError
from quota_guard.identity import parse_identity
from quota_guard.quota import QuotaStore

logger = logging.getLogger("quota_guard")

UNIDENTIFIED_TENANT = "<unidentified>"


class DegradePolicy(Enum):
    """治理组件内部失败时的降级策略。"""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class Decision:
    """一次请求的判定结果，含可解释的判定依据。

    reason 取值：
    - "allowed"：配额内放行；
    - "quota_exceeded"：超限拦截（retry_after 给出重试提示）；
    - "missing_identity" / "invalid_identity" / "conflicting_identity"：身份问题；
    - "degraded_fail_open" / "degraded_fail_closed"：治理组件异常后的降级判定。
    """

    allowed: bool
    reason: str
    tenant_id: str | None = None
    retry_after: float = 0.0
    degraded: bool = False


class QuotaGuard:
    """编排身份解析、配额计数与审计的判定入口。"""

    def __init__(
        self,
        store: QuotaStore,
        sink: AuditSink,
        limit: int,
        window_seconds: float,
        policy: DegradePolicy = DegradePolicy.FAIL_CLOSED,
    ) -> None:
        self._store = store
        self._sink = sink
        self._limit = limit
        self._window_seconds = window_seconds
        self._policy = policy

    def check(self, headers: dict[str, str], query: dict[str, str]) -> Decision:
        """对一次请求做出放行/拒绝判定，并留下审计记录。"""
        # 1. 身份解析：身份错误直接拒绝，原因可区分，不消耗任何配额。
        try:
            identity = parse_identity(headers, query)
        except IdentityError as exc:
            decision = Decision(allowed=False, reason=exc.reason, tenant_id=None)
            return self._finalize(UNIDENTIFIED_TENANT, decision, exc.message)

        tenant_id = identity.tenant_id

        # 2. 配额计数：后端异常时按既定策略降级，不静默吞异常。
        try:
            state = self._store.hit(tenant_id, self._limit, self._window_seconds)
        except Exception as exc:  # noqa: BLE001 - 治理组件内部失败统一走降级
            logger.exception("quota store failed for tenant %s", tenant_id)
            decision = self._degraded_decision(tenant_id)
            return self._finalize(
                tenant_id, decision, f"quota store failure: {type(exc).__name__}"
            )

        if not state.allowed:
            decision = Decision(
                allowed=False,
                reason="quota_exceeded",
                tenant_id=tenant_id,
                retry_after=state.retry_after,
            )
            detail = (
                f"count {state.count}/{state.limit} in window, "
                f"retry after {state.retry_after:.3f}s"
            )
        else:
            decision = Decision(allowed=True, reason="allowed", tenant_id=tenant_id)
            detail = f"count {state.count}/{state.limit} in window"

        return self._finalize(tenant_id, decision, detail)

    def _degraded_decision(self, tenant_id: str | None) -> Decision:
        """按既定策略生成降级判定。"""
        allowed = self._policy is DegradePolicy.FAIL_OPEN
        return Decision(
            allowed=allowed,
            reason="degraded_fail_open" if allowed else "degraded_fail_closed",
            tenant_id=tenant_id,
            degraded=True,
        )

    def _finalize(self, tenant_id: str, decision: Decision, detail: str) -> Decision:
        """写入审计记录并返回最终判定。

        审计写入失败同样属于治理组件内部失败：fail-closed 时把判定改为
        降级拒绝，fail-open 时保留原判定但标记 degraded；异常始终进日志。
        """
        record = AuditRecord(
            tenant_id=tenant_id,
            allowed=decision.allowed,
            reason=decision.reason,
            detail=detail,
        )
        try:
            self._sink.write(record)
        except Exception:  # noqa: BLE001 - 审计失败不静默吞掉，进日志并按策略降级
            logger.exception(
                "audit write failed for tenant %s (reason=%s)",
                tenant_id,
                decision.reason,
            )
            if self._policy is DegradePolicy.FAIL_CLOSED:
                return self._degraded_decision(decision.tenant_id)
            return replace(decision, degraded=True)
        return decision
