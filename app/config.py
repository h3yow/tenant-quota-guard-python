"""按租户的配额与降级配置。

- 默认值对所有租户生效；tenant_overrides 可为指定租户单独配置上上限、
  窗口与降级策略，未覆盖的项沿用默认值。
- 构造（服务启动）时即完成全部校验：非法配置直接抛 ValueError，
  不会等到请求进入才暴露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .identity import TENANT_ID_PATTERN


class DegradationPolicy(str, Enum):
    """计数后端故障时的降级策略。"""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


def _require_positive_int(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _require_positive_number(name: str, value: float) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive number, got {value!r}")


@dataclass(frozen=True)
class TenantQuotaConfig:
    """单个租户的覆盖配置；degradation_policy 为 None 表示沿用默认策略。"""

    limit: int
    window_seconds: float
    degradation_policy: DegradationPolicy | None = None

    def __post_init__(self) -> None:
        _require_positive_int("limit", self.limit)
        _require_positive_number("window_seconds", self.window_seconds)
        if self.degradation_policy is not None and not isinstance(
            self.degradation_policy, DegradationPolicy
        ):
            raise ValueError(
                f"degradation_policy must be a DegradationPolicy or None, "
                f"got {self.degradation_policy!r}"
            )


@dataclass(frozen=True)
class GuardConfig:
    """治理层整体配置：默认值 + 按租户覆盖。构造时即完成校验。"""

    default_limit: int
    default_window_seconds: float
    default_policy: DegradationPolicy = DegradationPolicy.FAIL_CLOSED
    # 计数后端故障且按 fail-closed 拒绝时，对外给出的重试提示（秒）。
    unavailable_retry_after: float = 1.0
    tenant_overrides: Mapping[str, TenantQuotaConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_positive_int("default_limit", self.default_limit)
        _require_positive_number("default_window_seconds", self.default_window_seconds)
        _require_positive_number("unavailable_retry_after", self.unavailable_retry_after)
        if not isinstance(self.default_policy, DegradationPolicy):
            raise ValueError(
                f"default_policy must be a DegradationPolicy, got {self.default_policy!r}"
            )
        for tenant_id, override in self.tenant_overrides.items():
            if not TENANT_ID_PATTERN.match(tenant_id):
                raise ValueError(
                    f"tenant override key {tenant_id!r} is not a valid tenant id"
                )
            if not isinstance(override, TenantQuotaConfig):
                raise ValueError(
                    f"tenant override for {tenant_id!r} must be a TenantQuotaConfig, "
                    f"got {override!r}"
                )

    def _override(self, tenant_id: str) -> TenantQuotaConfig | None:
        return self.tenant_overrides.get(tenant_id)

    def limit_for(self, tenant_id: str) -> int:
        override = self._override(tenant_id)
        return override.limit if override is not None else self.default_limit

    def window_for(self, tenant_id: str) -> float:
        override = self._override(tenant_id)
        return override.window_seconds if override is not None else self.default_window_seconds

    def policy_for(self, tenant_id: str) -> DegradationPolicy:
        override = self._override(tenant_id)
        if override is not None and override.degradation_policy is not None:
            return override.degradation_policy
        return self.default_policy
