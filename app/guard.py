"""配额治理编排层：配额判定 -> 审计，含异常降级策略。

降级策略（显式、可配置）：
- 计数后端不可用：按 policy 放行（fail_open）或拒绝（fail_closed），默认 fail_closed。
- 审计写入失败：不影响判定结果，但标记 degraded 并记录日志，绝不静默吞异常。
- 任何内部异常都不向外暴露堆栈或跨租户信息。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .audit import AuditRecorder, AuditRecord
from .errors import RejectReason
from .identity import TenantIdentity
from .quota import QuotaCounter, QuotaStoreUnavailable

logger = logging.getLogger("quota_guard")


class DegradationPolicy(str, Enum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class GuardDecision:
    """治理层最终判定。"""

    allowed: bool
    tenant_id: str
    reason: str
    degraded: bool
    retry_after: float | None


class QuotaGuard:
    """编排配额与审计的治理入口。"""

    def __init__(
        self,
        counter: QuotaCounter,
        recorder: AuditRecorder,
        policy: DegradationPolicy = DegradationPolicy.FAIL_CLOSED,
    ) -> None:
        self._counter = counter
        self._recorder = recorder
        self._policy = policy

    def _audit(self, record: AuditRecord) -> bool:
        """写入审计；失败时记录日志并返回 False（degraded），不吞异常也不影响判定。"""
        try:
            self._recorder.record(record)
            return True
        except Exception:
            logger.exception(
                "audit write failed, decision unaffected",
                extra={"tenant_id": record.tenant_id, "reason": record.reason},
            )
            return False

    def check(self, identity: TenantIdentity, now: float) -> GuardDecision:
        tenant_id = identity.tenant_id

        try:
            quota = self._counter.try_acquire(tenant_id, now)
        except QuotaStoreUnavailable:
            # 计数后端不可用：按既定策略降级，并保留可追溯依据。
            logger.warning(
                "quota store unavailable, degrading with policy=%s",
                self._policy.value,
                extra={"tenant_id": tenant_id},
            )
            if self._policy is DegradationPolicy.FAIL_OPEN:
                self._audit(AuditRecord(
                    tenant_id=tenant_id,
                    decision="allowed",
                    reason="degraded_fail_open",
                    detail="quota store unavailable; allowed by fail-open policy",
                ))
                return GuardDecision(
                    allowed=True,
                    tenant_id=tenant_id,
                    reason="degraded_fail_open",
                    degraded=True,
                    retry_after=None,
                )
            self._audit(AuditRecord(
                tenant_id=tenant_id,
                decision="rejected",
                reason=RejectReason.QUOTA_UNAVAILABLE.value,
                detail="quota store unavailable; rejected by fail-closed policy",
            ))
            return GuardDecision(
                allowed=False,
                tenant_id=tenant_id,
                reason=RejectReason.QUOTA_UNAVAILABLE.value,
                degraded=True,
                retry_after=None,
            )

        if quota.allowed:
            audit_ok = self._audit(AuditRecord(
                tenant_id=tenant_id,
                decision="allowed",
                reason="within_quota",
                detail=f"remaining={quota.remaining} window_start={quota.window_start}",
            ))
            return GuardDecision(
                allowed=True,
                tenant_id=tenant_id,
                reason="within_quota",
                degraded=not audit_ok,
                retry_after=None,
            )

        audit_ok = self._audit(AuditRecord(
            tenant_id=tenant_id,
            decision="rejected",
            reason=RejectReason.QUOTA_EXCEEDED.value,
            detail=f"retry_after={quota.retry_after:.3f} window_start={quota.window_start}",
        ))
        return GuardDecision(
            allowed=False,
            tenant_id=tenant_id,
            reason=RejectReason.QUOTA_EXCEEDED.value,
            degraded=not audit_ok,
            retry_after=quota.retry_after,
        )
