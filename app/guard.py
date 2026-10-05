"""配额治理编排层：身份校验 -> 配额判定 -> 审计，含异常降级策略。

降级策略（显式、按租户生效）：
- 计数后端以任意方式失败（连接失败、超时、未预料的运行错误……）：
  一律按该租户生效的 DegradationPolicy 处理——fail_open 放行 / fail_closed 拒绝；
  未单独配置的租户沿用默认策略。判定结果、审计记录、对外响应三者语义一致。
- 审计写入失败：不影响判定结果，但标记 degraded 并记录日志，绝不静默吞异常。
- 身份校验未通过的拒绝同样写入审计，可追溯；审计按键隔离，租户间不可互查。
- 任何内部异常都不向外暴露堆栈或跨租户信息。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping

from .audit import AuditRecorder, AuditRecord
from .config import DegradationPolicy, QuotaSettings
from .errors import QuotaGuardError, RejectReason
from .identity import TenantIdentity, parse_tenant_identity
from .quota import QuotaCounter

# 兼容既有引用：DegradationPolicy 现定义于 app.config，此处再导出。
__all__ = ["DegradationPolicy", "GuardDecision", "QuotaGuard"]

logger = logging.getLogger("quota_guard")

# 身份缺失时的审计归属键（无法归属到具体租户，单独成桶，不与其他租户混淆）。
UNKNOWN_TENANT_KEY = "<unknown>"


@dataclass(frozen=True)
class GuardDecision:
    """治理层最终判定。"""

    allowed: bool
    tenant_id: str
    reason: str
    degraded: bool
    retry_after: float | None


class QuotaGuard:
    """编排身份、配额与审计的治理入口。"""

    def __init__(
        self,
        counter: QuotaCounter,
        recorder: AuditRecorder,
        policy: DegradationPolicy = DegradationPolicy.FAIL_CLOSED,
        settings: QuotaSettings | None = None,
    ) -> None:
        self._counter = counter
        self._recorder = recorder
        # settings 提供按租户的降级策略；未提供时所有租户使用同一个默认策略。
        self._settings = settings
        self._default_policy = policy

    def _policy_for(self, tenant_id: str) -> DegradationPolicy:
        if self._settings is not None:
            return self._settings.rule_for(tenant_id).policy
        return self._default_policy

    def _retry_hint_for(self, tenant_id: str) -> float | None:
        """fail-closed 拒绝时的重试提示：一个生效窗口的长度。"""
        if self._settings is not None:
            return self._settings.rule_for(tenant_id).window_seconds
        return None

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

    # ---- 身份校验 ----

    @staticmethod
    def _identity_failure_key(headers: Mapping[str, str], query_params: Mapping[str, str]) -> str:
        """身份校验失败时审计记录的归属键：尽量用请求里给出的原始值。"""
        lowered = {k.lower(): v for k, v in headers.items()}
        raw = lowered.get("x-tenant-id") or query_params.get("tenant_id")
        if raw and raw.strip():
            return raw.strip()
        return UNKNOWN_TENANT_KEY

    def check_request(
        self,
        headers: dict[str, str],
        query_params: dict[str, str],
        now: float,
    ) -> GuardDecision:
        """完整治理入口：身份校验 -> 配额判定。身份被拒同样留审计依据。"""
        try:
            identity = parse_tenant_identity(headers, query_params)
        except QuotaGuardError as exc:
            key = self._identity_failure_key(headers, query_params)
            self._audit(AuditRecord(
                tenant_id=key,
                decision="rejected",
                reason=exc.reason.value,
                detail=f"identity check failed: {exc.message}",
            ))
            return GuardDecision(
                allowed=False,
                tenant_id=key,
                reason=exc.reason.value,
                degraded=False,
                retry_after=None,
            )
        return self.check(identity, now)

    # ---- 配额判定 ----

    def check(self, identity: TenantIdentity, now: float) -> GuardDecision:
        tenant_id = identity.tenant_id

        try:
            quota = self._counter.try_acquire(tenant_id, now)
        except Exception as exc:
            # 计数后端以任意方式失败（连接失败 / 超时 / 未预料的运行错误）：
            # 统一按该租户生效的策略降级，判定、审计、响应保持一致。
            policy = self._policy_for(tenant_id)
            logger.warning(
                "quota store failed (%s: %s), degrading with policy=%s",
                type(exc).__name__, exc, policy.value,
                extra={"tenant_id": tenant_id},
            )
            if policy is DegradationPolicy.FAIL_OPEN:
                self._audit(AuditRecord(
                    tenant_id=tenant_id,
                    decision="allowed",
                    reason="degraded_fail_open",
                    detail=f"quota store failed ({type(exc).__name__}); allowed by fail-open policy",
                ))
                return GuardDecision(
                    allowed=True,
                    tenant_id=tenant_id,
                    reason="degraded_fail_open",
                    degraded=True,
                    retry_after=None,
                )
            retry_hint = self._retry_hint_for(tenant_id)
            self._audit(AuditRecord(
                tenant_id=tenant_id,
                decision="rejected",
                reason=RejectReason.QUOTA_UNAVAILABLE.value,
                detail=f"quota store failed ({type(exc).__name__}); rejected by fail-closed policy",
            ))
            return GuardDecision(
                allowed=False,
                tenant_id=tenant_id,
                reason=RejectReason.QUOTA_UNAVAILABLE.value,
                degraded=True,
                retry_after=retry_hint,
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

    # ---- 状态回收 ----

    def reclaim_expired(self, now: float) -> list[str]:
        """清理窗口已完整过去的租户计数状态（与并发判定结果一致）。"""
        reclaimed = self._counter.reclaim_expired(now)
        if reclaimed:
            logger.info("reclaimed expired tenant state: %s", reclaimed)
        return reclaimed

    def reclaim_tenants(self, tenant_ids: Iterable[str]) -> list[str]:
        """显式清理已确认不再活跃的租户状态。"""
        reclaimed = self._counter.reclaim_tenants(tenant_ids)
        if reclaimed:
            logger.info("reclaimed inactive tenants: %s", reclaimed)
        return reclaimed
