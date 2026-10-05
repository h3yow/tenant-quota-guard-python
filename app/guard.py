"""配额治理编排层：配额判定 -> 审计，含异常降级策略。

降级策略（显式、可按租户覆盖）：
- 计数后端以任意方式失败（连接失败、超时、未预料的运行错误等，统一捕获
  Exception）：按该租户生效的 DegradationPolicy 放行（fail_open）或拒绝
  （fail_closed），未单独配置的租户用默认策略。判定与对外响应共用同一个
  GuardDecision，策略语义一致；fail-closed 拒绝携带重试提示。
- 审计写入失败：不影响判定结果，但标记 degraded 并记录日志，绝不静默吞异常。
- 身份校验被拒的请求同样写入审计（按可解析的租户候选隔离，无法解析时归入
  保留键 "<unidentified>"），不留无记录的拒绝。
- 任何内部异常都不向外暴露堆栈或跨租户信息。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from .audit import AuditRecorder, AuditRecord
from .config import DegradationPolicy, GuardConfig
from .errors import RejectReason
from .identity import TenantIdentity
from .quota import QuotaCounter

logger = logging.getLogger("quota_guard")

# 身份无法解析时审计记录归入的保留键；该值不是合法租户 ID，不会与真实租户冲突。
UNIDENTIFIED_TENANT = "<unidentified>"

__all__ = [
    "DegradationPolicy",
    "GuardDecision",
    "QuotaGuard",
    "UNIDENTIFIED_TENANT",
]


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
        config: GuardConfig | None = None,
    ) -> None:
        self._counter = counter
        self._recorder = recorder
        self._policy = policy
        self._config = config

    def _policy_for(self, tenant_id: str) -> DegradationPolicy:
        """该租户生效的降级策略：有配置按配置，否则用构造时给的默认策略。"""
        if self._config is not None:
            return self._config.policy_for(tenant_id)
        return self._policy

    def _unavailable_retry_after(self) -> float:
        if self._config is not None:
            return self._config.unavailable_retry_after
        return 1.0

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

    def _degraded_decision(self, tenant_id: str, exc: Exception) -> GuardDecision:
        """计数后端失败（任意异常类型）时，按该租户生效策略给出判定并留痕。"""
        policy = self._policy_for(tenant_id)
        failure = type(exc).__name__
        logger.warning(
            "quota store failed (%s), degrading with policy=%s",
            failure,
            policy.value,
            extra={"tenant_id": tenant_id},
        )
        if policy is DegradationPolicy.FAIL_OPEN:
            self._audit(AuditRecord(
                tenant_id=tenant_id,
                decision="allowed",
                reason="degraded_fail_open",
                detail=f"quota store failed ({failure}); allowed by fail-open policy",
            ))
            return GuardDecision(
                allowed=True,
                tenant_id=tenant_id,
                reason="degraded_fail_open",
                degraded=True,
                retry_after=None,
            )
        retry_after = self._unavailable_retry_after()
        self._audit(AuditRecord(
            tenant_id=tenant_id,
            decision="rejected",
            reason=RejectReason.QUOTA_UNAVAILABLE.value,
            detail=f"quota store failed ({failure}); rejected by fail-closed policy; "
                   f"retry_after={retry_after}",
        ))
        return GuardDecision(
            allowed=False,
            tenant_id=tenant_id,
            reason=RejectReason.QUOTA_UNAVAILABLE.value,
            degraded=True,
            retry_after=retry_after,
        )

    def fallback_decision(self, tenant_id: str | None) -> GuardDecision:
        """治理层自身出现未预料异常时的兜底判定，仍按生效策略给出，保证语义一致。"""
        tenant = tenant_id or UNIDENTIFIED_TENANT
        return self._degraded_decision(tenant, RuntimeError("unexpected guard failure"))

    def record_identity_rejection(
        self,
        tenant_candidates: Sequence[str],
        reason: str,
        detail: str,
    ) -> None:
        """为身份校验未通过的被拒请求留痕。

        按可解析出的租户候选分别记录（候选之间互不可见）；一个都解析不出时
        归入保留键 "<unidentified>"，保证任何拒绝都有可追溯依据。
        """
        targets = list(dict.fromkeys(tenant_candidates)) or [UNIDENTIFIED_TENANT]
        for tenant_id in targets:
            self._audit(AuditRecord(
                tenant_id=tenant_id,
                decision="rejected",
                reason=reason,
                detail=detail,
            ))

    def check(self, identity: TenantIdentity, now: float) -> GuardDecision:
        tenant_id = identity.tenant_id

        try:
            quota = self._counter.try_acquire(tenant_id, now)
        except Exception as exc:
            # 连接失败、超时、未预料的运行错误等任意后端故障，统一按生效策略降级。
            return self._degraded_decision(tenant_id, exc)

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
