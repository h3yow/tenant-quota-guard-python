"""后端任意故障统一按生效策略降级；身份被拒请求同样留痕且租户隔离。"""

import pytest

from app.audit import AuditRecorder
from app.config import DegradationPolicy, GuardConfig, TenantQuotaConfig
from app.errors import RejectReason
from app.guard import UNIDENTIFIED_TENANT, QuotaGuard
from app.identity import TenantIdentity
from app.quota import QuotaCounter, QuotaStoreUnavailable


class _FlakyCounter(QuotaCounter):
    """按指定异常类型失败的计数后端。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def try_acquire(self, tenant_id, now):
        raise self._exc


BACKEND_FAILURES = [
    QuotaStoreUnavailable("backend down"),   # 显式不可用
    ConnectionError("connection refused"),   # 连不上
    TimeoutError("request timed out"),       # 响应超时
    RuntimeError("unexpected bug"),          # 未预料的运行错误
]


def _show(case: str, tenant: str, decision) -> None:
    print(f"\n[degrade] case={case} tenant={tenant} -> allowed={decision.allowed} "
          f"reason={decision.reason} degraded={decision.degraded} "
          f"retry_after={decision.retry_after}")


@pytest.mark.parametrize("exc", BACKEND_FAILURES, ids=lambda e: type(e).__name__)
def test_any_backend_failure_fail_closed(exc):
    recorder = AuditRecorder()
    config = GuardConfig(default_limit=5, default_window_seconds=10.0,
                         unavailable_retry_after=2.5)
    guard = QuotaGuard(_FlakyCounter(exc), recorder, config=config)

    decision = guard.check(TenantIdentity("tenant-a"), now=0.0)
    _show(f"fail_closed/{type(exc).__name__}", "tenant-a", decision)

    # 不论后端以哪种方式失败，fail-closed 语义一致：拒绝 + 可区分原因 + 重试提示。
    assert not decision.allowed
    assert decision.reason == RejectReason.QUOTA_UNAVAILABLE.value
    assert decision.degraded
    assert decision.retry_after == 2.5

    records = recorder.records_for("tenant-a")
    for r in records:
        print(f"[audit] {r}")
    assert len(records) == 1
    assert records[0].decision == "rejected"
    assert records[0].reason == RejectReason.QUOTA_UNAVAILABLE.value
    assert type(exc).__name__ in records[0].detail  # 失败方式可追溯


@pytest.mark.parametrize("exc", BACKEND_FAILURES, ids=lambda e: type(e).__name__)
def test_any_backend_failure_fail_open(exc):
    recorder = AuditRecorder()
    config = GuardConfig(default_limit=5, default_window_seconds=10.0,
                         default_policy=DegradationPolicy.FAIL_OPEN)
    guard = QuotaGuard(_FlakyCounter(exc), recorder, config=config)

    decision = guard.check(TenantIdentity("tenant-a"), now=0.0)
    _show(f"fail_open/{type(exc).__name__}", "tenant-a", decision)

    assert decision.allowed
    assert decision.reason == "degraded_fail_open"
    assert decision.degraded
    assert recorder.records_for("tenant-a")[0].reason == "degraded_fail_open"


@pytest.mark.parametrize("exc", BACKEND_FAILURES, ids=lambda e: type(e).__name__)
def test_per_tenant_policy_override(exc):
    """同一后端故障：被覆盖的租户按 fail_open 放行，其他租户按默认 fail_closed 拒绝。"""
    recorder = AuditRecorder()
    config = GuardConfig(
        default_limit=5,
        default_window_seconds=10.0,
        default_policy=DegradationPolicy.FAIL_CLOSED,
        tenant_overrides={
            "tenant-vip": TenantQuotaConfig(
                limit=5, window_seconds=10.0,
                degradation_policy=DegradationPolicy.FAIL_OPEN,
            ),
        },
    )
    guard = QuotaGuard(_FlakyCounter(exc), recorder, config=config)

    vip = guard.check(TenantIdentity("tenant-vip"), now=0.0)
    plain = guard.check(TenantIdentity("tenant-b"), now=0.0)
    _show(f"per_tenant/{type(exc).__name__}", "tenant-vip", vip)
    _show(f"per_tenant/{type(exc).__name__}", "tenant-b", plain)

    # 判定结果与各租户生效策略一一对应，互不串扰。
    assert vip.allowed and vip.reason == "degraded_fail_open"
    assert not plain.allowed and plain.reason == RejectReason.QUOTA_UNAVAILABLE.value
    assert recorder.records_for("tenant-vip")[0].decision == "allowed"
    assert recorder.records_for("tenant-b")[0].decision == "rejected"


def test_identity_rejection_is_audited_per_candidate():
    recorder = AuditRecorder()
    guard = QuotaGuard(QuotaCounter(limit=5, window_seconds=10.0), recorder)

    # 头与查询参数冲突：两个可解析租户各自留下拒绝依据。
    guard.record_identity_rejection(
        ["tenant-a", "tenant-b"],
        RejectReason.TENANT_CONFLICT.value,
        "tenant identity conflict between header and query param",
    )
    # 完全无法解析：归入保留键。
    guard.record_identity_rejection(
        [], RejectReason.TENANT_MISSING.value, "tenant identity is missing",
    )

    for tenant in ("tenant-a", "tenant-b", UNIDENTIFIED_TENANT):
        for r in recorder.records_for(tenant):
            print(f"\n[audit] {r}")
    assert [r.reason for r in recorder.records_for("tenant-a")] == ["tenant_conflict"]
    assert [r.reason for r in recorder.records_for("tenant-b")] == ["tenant_conflict"]
    assert [r.reason for r in recorder.records_for(UNIDENTIFIED_TENANT)] == ["tenant_missing"]
    # 隔离：无关租户看不到这些记录。
    assert recorder.records_for("tenant-c") == []


def test_identity_rejection_records_do_not_leak_across_tenants():
    recorder = AuditRecorder()
    guard = QuotaGuard(QuotaCounter(limit=5, window_seconds=10.0), recorder)
    guard.record_identity_rejection(["tenant-a"], "tenant_invalid", "bad format")
    guard.check(TenantIdentity("tenant-a"), now=0.0)

    a_records = recorder.records_for("tenant-a")
    print(f"\n[audit] tenant-a -> {[ (r.decision, r.reason) for r in a_records ]}")
    assert [r.decision for r in a_records] == ["rejected", "allowed"]
    assert all(r.tenant_id == "tenant-a" for r in a_records)
    assert recorder.records_for("tenant-b") == []
