"""配额治理配置：默认值 + 按租户覆盖，服务启动时一次性校验。

规则：
- 未单独配置的租户沿用默认上限 / 窗口 / 降级策略。
- 单独配置的租户可覆盖上限、窗口，也可覆盖降级策略（不覆盖则沿用默认策略）。
- 所有配置在构造 QuotaSettings 时即校验，非法配置直接抛 ValueError，
  保证"启动时就失败"，而不是等请求打进来才发现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .identity import TENANT_PATTERN


class DegradationPolicy(str, Enum):
    """计数后端故障时的降级策略。"""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class TenantQuotaRule:
    """单个租户的配额覆盖规则。policy 为 None 表示沿用默认策略。"""

    limit: int
    window_seconds: float
    policy: DegradationPolicy | None = None


@dataclass(frozen=True)
class EffectiveRule:
    """某个租户最终生效的规则（默认值与覆盖合并后的结果）。"""

    limit: int
    window_seconds: float
    policy: DegradationPolicy


def _validate_positive(name: str, limit: int, window_seconds: float) -> None:
    if limit <= 0:
        raise ValueError(f"{name}: limit must be positive, got {limit}")
    if window_seconds <= 0:
        raise ValueError(f"{name}: window_seconds must be positive, got {window_seconds}")


@dataclass(frozen=True)
class QuotaSettings:
    """治理层全部配置。构造即校验，校验不过直接抛 ValueError。"""

    default_limit: int
    default_window_seconds: float
    default_policy: DegradationPolicy = DegradationPolicy.FAIL_CLOSED
    tenant_overrides: Mapping[str, TenantQuotaRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_positive("default", self.default_limit, self.default_window_seconds)
        if not isinstance(self.default_policy, DegradationPolicy):
            object.__setattr__(self, "default_policy", DegradationPolicy(self.default_policy))
        for tenant_id, rule in self.tenant_overrides.items():
            if not TENANT_PATTERN.match(tenant_id):
                raise ValueError(f"tenant override id is invalid: {tenant_id!r}")
            _validate_positive(f"tenant {tenant_id}", rule.limit, rule.window_seconds)
            if rule.policy is not None and not isinstance(rule.policy, DegradationPolicy):
                raise ValueError(f"tenant {tenant_id}: unknown policy {rule.policy!r}")

    def rule_for(self, tenant_id: str) -> EffectiveRule:
        """返回租户最终生效的规则：有覆盖用覆盖，否则用默认值。"""
        override = self.tenant_overrides.get(tenant_id)
        if override is None:
            return EffectiveRule(
                limit=self.default_limit,
                window_seconds=self.default_window_seconds,
                policy=self.default_policy,
            )
        return EffectiveRule(
            limit=override.limit,
            window_seconds=override.window_seconds,
            policy=override.policy if override.policy is not None else self.default_policy,
        )
