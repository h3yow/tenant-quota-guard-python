"""按租户配置：启动时校验、覆盖与默认值的合并规则。"""

import pytest

from app.config import DegradationPolicy, QuotaSettings, TenantQuotaRule


def test_defaults_apply_when_no_override():
    settings = QuotaSettings(default_limit=100, default_window_seconds=60.0)
    rule = settings.rule_for("tenant-a")
    print(f"\n[config] no override -> {rule}")
    assert rule.limit == 100
    assert rule.window_seconds == 60.0
    assert rule.policy is DegradationPolicy.FAIL_CLOSED


def test_override_replaces_limit_and_window():
    settings = QuotaSettings(
        default_limit=100,
        default_window_seconds=60.0,
        default_policy=DegradationPolicy.FAIL_CLOSED,
        tenant_overrides={
            "tenant-vip": TenantQuotaRule(limit=1000, window_seconds=10.0,
                                          policy=DegradationPolicy.FAIL_OPEN),
        },
    )
    vip = settings.rule_for("tenant-vip")
    plain = settings.rule_for("tenant-a")
    print(f"\n[config] tenant-vip -> {vip}")
    print(f"[config] tenant-a   -> {plain}")
    assert (vip.limit, vip.window_seconds, vip.policy) == (1000, 10.0, DegradationPolicy.FAIL_OPEN)
    # 未覆盖的租户完全沿用默认值。
    assert (plain.limit, plain.window_seconds, plain.policy) == (100, 60.0, DegradationPolicy.FAIL_CLOSED)


def test_override_without_policy_inherits_default_policy():
    settings = QuotaSettings(
        default_limit=100,
        default_window_seconds=60.0,
        default_policy=DegradationPolicy.FAIL_OPEN,
        tenant_overrides={"tenant-vip": TenantQuotaRule(limit=5, window_seconds=10.0)},
    )
    rule = settings.rule_for("tenant-vip")
    print(f"\n[config] override w/o policy -> {rule}")
    assert rule.policy is DegradationPolicy.FAIL_OPEN


@pytest.mark.parametrize("kwargs", [
    {"default_limit": 0, "default_window_seconds": 60.0},
    {"default_limit": -1, "default_window_seconds": 60.0},
    {"default_limit": 100, "default_window_seconds": 0.0},
    {"default_limit": 100, "default_window_seconds": -10.0},
])
def test_invalid_defaults_rejected_at_startup(kwargs):
    with pytest.raises(ValueError) as exc_info:
        QuotaSettings(**kwargs)
    print(f"\n[config] invalid defaults {kwargs} -> ValueError: {exc_info.value}")


@pytest.mark.parametrize("overrides", [
    {"tenant-vip": TenantQuotaRule(limit=0, window_seconds=10.0)},
    {"tenant-vip": TenantQuotaRule(limit=5, window_seconds=-1.0)},
    {"BAD_ID": TenantQuotaRule(limit=5, window_seconds=10.0)},
    {"x": TenantQuotaRule(limit=5, window_seconds=10.0)},
])
def test_invalid_overrides_rejected_at_startup(overrides):
    with pytest.raises(ValueError) as exc_info:
        QuotaSettings(default_limit=100, default_window_seconds=60.0, tenant_overrides=overrides)
    print(f"\n[config] invalid overrides {list(overrides)} -> ValueError: {exc_info.value}")
