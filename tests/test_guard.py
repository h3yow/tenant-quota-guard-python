"""治理编排：超限拦截、组件异常降级、审计可追溯且租户隔离。"""

from app.audit import AuditRecorder, AuditStoreUnavailable
from app.errors import RejectReason
from app.guard import DegradationPolicy, QuotaGuard
from app.identity import TenantIdentity
from app.quota import QuotaCounter, QuotaStoreUnavailable


class _BrokenCounter(QuotaCounter):
    def __init__(self):
        pass  # 不初始化父类状态

    def try_acquire(self, tenant_id, now):
        raise QuotaStoreUnavailable("backend down")


class _BrokenRecorder(AuditRecorder):
    def record(self, record):
        raise AuditStoreUnavailable("audit backend down")


def _show(case: str, tenant: str, decision) -> None:
    print(f"\n[guard] case={case} tenant={tenant} -> allowed={decision.allowed} "
          f"reason={decision.reason} degraded={decision.degraded} "
          f"retry_after={decision.retry_after}")


def test_quota_exceeded_rejected_with_retry_hint():
    recorder = AuditRecorder()
    guard = QuotaGuard(QuotaCounter(limit=1, window_seconds=10.0), recorder)
    identity = TenantIdentity("tenant-a")

    first = guard.check(identity, now=0.0)
    second = guard.check(identity, now=1.0)
    _show("exceeded", "tenant-a", second)

    assert first.allowed
    assert not second.allowed
    assert second.reason == RejectReason.QUOTA_EXCEEDED.value
    assert second.retry_after == 9.0

    # 审计记录可追溯，且只含本租户。
    records = recorder.records_for("tenant-a")
    for r in records:
        print(f"[audit] {r}")
    assert [r.decision for r in records] == ["allowed", "rejected"]
    assert recorder.records_for("tenant-b") == []


def test_quota_store_down_fail_closed():
    recorder = AuditRecorder()
    guard = QuotaGuard(_BrokenCounter(), recorder, policy=DegradationPolicy.FAIL_CLOSED)
    decision = guard.check(TenantIdentity("tenant-a"), now=0.0)
    _show("store_down_fail_closed", "tenant-a", decision)

    assert not decision.allowed
    assert decision.reason == RejectReason.QUOTA_UNAVAILABLE.value
    assert decision.degraded
    # 拒绝依据仍被审计记录。
    assert recorder.records_for("tenant-a")[0].reason == RejectReason.QUOTA_UNAVAILABLE.value


def test_quota_store_down_fail_open():
    recorder = AuditRecorder()
    guard = QuotaGuard(_BrokenCounter(), recorder, policy=DegradationPolicy.FAIL_OPEN)
    decision = guard.check(TenantIdentity("tenant-a"), now=0.0)
    _show("store_down_fail_open", "tenant-a", decision)

    assert decision.allowed
    assert decision.reason == "degraded_fail_open"
    assert decision.degraded
    assert recorder.records_for("tenant-a")[0].reason == "degraded_fail_open"


def test_audit_failure_does_not_change_decision(caplog):
    guard = QuotaGuard(QuotaCounter(limit=5, window_seconds=10.0), _BrokenRecorder())
    with caplog.at_level("ERROR", logger="quota_guard"):
        decision = guard.check(TenantIdentity("tenant-a"), now=0.0)
    _show("audit_down", "tenant-a", decision)

    # 审计失败不静默：判定仍放行但标记 degraded，且日志留有记录。
    assert decision.allowed
    assert decision.degraded
    assert any("audit write failed" in r.message for r in caplog.records)


def test_tenant_state_isolated_under_guard():
    recorder = AuditRecorder()
    guard = QuotaGuard(QuotaCounter(limit=1, window_seconds=10.0), recorder)
    guard.check(TenantIdentity("tenant-a"), now=0.0)
    blocked_a = guard.check(TenantIdentity("tenant-a"), now=0.0)
    ok_b = guard.check(TenantIdentity("tenant-b"), now=0.0)
    _show("isolation_a", "tenant-a", blocked_a)
    _show("isolation_b", "tenant-b", ok_b)

    assert not blocked_a.allowed and ok_b.allowed
    assert {r.tenant_id for r in recorder.records_for("tenant-a")} == {"tenant-a"}
    assert {r.tenant_id for r in recorder.records_for("tenant-b")} == {"tenant-b"}
