"""配置：启动时校验非法项、按租户覆盖解析、未覆盖项回落默认值。"""

import pytest

from app.config import DegradationPolicy, GuardConfig, TenantQuotaConfig


def _show(case: str, outcome: str) -> None:
    print(f"\n[config] case={case} -> {outcome}")


def test_defaults_apply_when_no_override():
    cfg = GuardConfig(default_limit=100, default_window_seconds=60.0)
    outcome = (f"limit={cfg.limit_for('tenant-a')} window={cfg.window_for('tenant-a')} "
               f"policy={cfg.policy_for('tenant-a').value}")
    _show("defaults_only", outcome)
    assert cfg.limit_for("tenant-a") == 100
    assert cfg.window_for("tenant-a") == 60.0
    assert cfg.policy_for("tenant-a") is DegradationPolicy.FAIL_CLOSED


def test_per_tenant_override_wins_and_others_keep_default():
    cfg = GuardConfig(
        default_limit=100,
        default_window_seconds=60.0,
        tenant_overrides={
            "tenant-vip": TenantQuotaConfig(
                limit=5, window_seconds=10.0,
                degradation_policy=DegradationPolicy.FAIL_OPEN,
            ),
        },
    )
    _show("override", f"vip limit={cfg.limit_for('tenant-vip')} "
                      f"window={cfg.window_for('tenant-vip')} "
                      f"policy={cfg.policy_for('tenant-vip').value}")
    _show("fallback", f"plain limit={cfg.limit_for('tenant-b')} "
                      f"window={cfg.window_for('tenant-b')} "
                      f"policy={cfg.policy_for('tenant-b').value}")
    assert cfg.limit_for("tenant-vip") == 5
    assert cfg.window_for("tenant-vip") == 10.0
    assert cfg.policy_for("tenant-vip") is DegradationPolicy.FAIL_OPEN
    # 未覆盖的租户沿用默认值。
    assert cfg.limit_for("tenant-b") == 100
    assert cfg.window_for("tenant-b") == 60.0
    assert cfg.policy_for("tenant-b") is DegradationPolicy.FAIL_CLOSED


def test_override_without_policy_inherits_default_policy():
    cfg = GuardConfig(
        default_limit=100,
        default_window_seconds=60.0,
        default_policy=DegradationPolicy.FAIL_OPEN,
        tenant_overrides={"tenant-vip": TenantQuotaConfig(limit=5, window_seconds=10.0)},
    )
    _show("policy_inherit", f"policy={cfg.policy_for('tenant-vip').value}")
    assert cfg.policy_for("tenant-vip") is DegradationPolicy.FAIL_OPEN


@pytest.mark.parametrize("kwargs", [
    {"default_limit": 0},
    {"default_limit": -1},
    {"default_limit": 1.5},
    {"default_window_seconds": 0},
    {"default_window_seconds": -3.0},
    {"unavailable_retry_after": 0},
    {"default_policy": "fail_open"},  # 字符串而非枚举，启动即拒绝
])
def test_invalid_defaults_rejected_at_startup(kwargs):
    base = {"default_limit": 100, "default_window_seconds": 60.0}
    base.update(kwargs)
    with pytest.raises(ValueError) as exc_info:
        GuardConfig(**base)
    _show("invalid_default", f"kwargs={kwargs} -> ValueError: {exc_info.value}")


@pytest.mark.parametrize("override", [
    TenantQuotaConfig(limit=5, window_seconds=10.0),  # 占位，键非法的用例在下方单独构造
])
def test_override_value_type_checked(override):
    with pytest.raises(ValueError):
        GuardConfig(default_limit=100, default_window_seconds=60.0,
                    tenant_overrides={"tenant-a": {"limit": 5}})
    _show("override_type", "dict instead of TenantQuotaConfig -> ValueError")


@pytest.mark.parametrize("bad_tenant", ["", "A", "1abc", "bad_id", "a" * 33])
def test_override_key_must_be_valid_tenant_id(bad_tenant):
    with pytest.raises(ValueError) as exc_info:
        GuardConfig(
            default_limit=100, default_window_seconds=60.0,
            tenant_overrides={bad_tenant: TenantQuotaConfig(limit=5, window_seconds=10.0)},
        )
    _show("bad_override_key", f"key={bad_tenant!r} -> ValueError: {exc_info.value}")


@pytest.mark.parametrize("kwargs", [
    {"limit": 0, "window_seconds": 10.0},
    {"limit": 5, "window_seconds": 0},
    {"limit": 5, "window_seconds": 10.0, "degradation_policy": "fail_open"},
])
def test_invalid_tenant_override_rejected(kwargs):
    with pytest.raises(ValueError) as exc_info:
        TenantQuotaConfig(**kwargs)
    _show("invalid_override", f"kwargs={kwargs} -> ValueError: {exc_info.value}")
