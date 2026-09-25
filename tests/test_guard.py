"""QuotaGuard 编排：身份拒绝、超限拦截、审计记录与异常降级。"""

import logging

import pytest

from quota_guard import InMemoryQuotaStore, QuotaGuard
from quota_guard.audit import AuditRecord, InMemoryAuditSink
from quota_guard.guard import UNIDENTIFIED_TENANT, DegradePolicy

logger = logging.getLogger("quota_guard.test")


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class BrokenStore:
    """模拟计数后端不可用。"""

    def hit(self, *args):
        raise RuntimeError("backend connection lost")


class BrokenSink:
    """模拟审计写入失败。"""

    def write(self, record: AuditRecord) -> None:
        raise RuntimeError("disk full")


def make_guard(policy=DegradePolicy.FAIL_CLOSED, limit=2, window=10.0,
               store=None, sink=None, clock=None):
    clock = clock or FakeClock()
    sink = sink or InMemoryAuditSink()
    store = store or InMemoryQuotaStore(clock=clock)
    return QuotaGuard(store, sink, limit=limit, window_seconds=window,
                      policy=policy), sink, clock


def check_and_log(guard, headers, query):
    decision = guard.check(headers, query)
    logger.info("input headers=%s query=%s -> decision allowed=%s reason=%s "
                "retry_after=%.3f degraded=%s",
                headers, query, decision.allowed, decision.reason,
                decision.retry_after, decision.degraded)
    return decision


def test_allow_and_audit_trail():
    guard, sink, _ = make_guard()
    d = check_and_log(guard, {"x-tenant-id": "acme"}, {})
    assert d.allowed and d.reason == "allowed" and d.tenant_id == "acme"

    records = sink.for_tenant("acme")
    assert len(records) == 1
    assert records[0].allowed and records[0].reason == "allowed"
    assert "1/2" in records[0].detail  # 判定依据可追溯


@pytest.mark.parametrize("headers,query,reason", [
    ({}, {}, "missing_identity"),
    ({"x-tenant-id": "!!"}, {}, "invalid_identity"),
    ({"x-tenant-id": "a"}, {"tenant_id": "b"}, "conflicting_identity"),
])
def test_identity_rejections_are_distinguishable(headers, query, reason):
    guard, sink, _ = make_guard()
    d = check_and_log(guard, headers, query)
    assert not d.allowed and d.reason == reason
    # 身份问题也留审计痕迹，且不归属任何真实租户
    records = sink.for_tenant(UNIDENTIFIED_TENANT)
    assert len(records) == 1 and records[0].reason == reason


def test_quota_exceeded_has_retry_hint_and_audits():
    guard, sink, clock = make_guard(limit=2, window=10.0)
    h = {"x-tenant-id": "acme"}
    check_and_log(guard, h, {})
    check_and_log(guard, h, {})
    d = check_and_log(guard, h, {})

    assert not d.allowed and d.reason == "quota_exceeded"
    assert d.retry_after > 0

    # 窗口推进后恢复放行
    clock.now += 10.0
    d2 = check_and_log(guard, h, {})
    assert d2.allowed

    reasons = [r.reason for r in sink.for_tenant("acme")]
    assert reasons == ["allowed", "allowed", "quota_exceeded", "allowed"]


def test_tenant_decisions_do_not_interfere():
    guard, sink, _ = make_guard(limit=1)
    check_and_log(guard, {"x-tenant-id": "a"}, {})
    da = check_and_log(guard, {"x-tenant-id": "a"}, {})
    db = check_and_log(guard, {"x-tenant-id": "b"}, {})

    assert not da.allowed and da.reason == "quota_exceeded"
    assert db.allowed  # 租户 a 超限不影响租户 b
    assert [r.reason for r in sink.for_tenant("a")] == ["allowed", "quota_exceeded"]
    assert [r.reason for r in sink.for_tenant("b")] == ["allowed"]


def test_store_failure_fail_closed():
    guard, sink, _ = make_guard(policy=DegradePolicy.FAIL_CLOSED,
                                store=BrokenStore())
    d = check_and_log(guard, {"x-tenant-id": "acme"}, {})
    assert not d.allowed and d.reason == "degraded_fail_closed" and d.degraded
    # 判定依据可追溯
    assert "quota store failure" in sink.for_tenant("acme")[0].detail


def test_store_failure_fail_open():
    guard, sink, _ = make_guard(policy=DegradePolicy.FAIL_OPEN,
                                store=BrokenStore())
    d = check_and_log(guard, {"x-tenant-id": "acme"}, {})
    assert d.allowed and d.reason == "degraded_fail_open" and d.degraded


def test_store_failure_does_not_leak_internals():
    guard, _, _ = make_guard(store=BrokenStore())
    d = guard.check({"x-tenant-id": "acme"}, {})
    # Decision 中不得出现堆栈或异常消息
    assert "backend connection lost" not in str(d)
    assert "Traceback" not in str(d)


def test_audit_failure_fail_closed_denies():
    guard, _, _ = make_guard(policy=DegradePolicy.FAIL_CLOSED, sink=BrokenSink())
    d = check_and_log(guard, {"x-tenant-id": "acme"}, {})
    assert not d.allowed and d.reason == "degraded_fail_closed" and d.degraded


def test_audit_failure_fail_open_keeps_decision_but_marks_degraded():
    guard, _, _ = make_guard(policy=DegradePolicy.FAIL_OPEN, sink=BrokenSink())
    d = check_and_log(guard, {"x-tenant-id": "acme"}, {})
    assert d.allowed and d.reason == "allowed" and d.degraded


def test_audit_failure_is_logged_not_swallowed(caplog):
    guard, _, _ = make_guard(sink=BrokenSink())
    with caplog.at_level(logging.ERROR, logger="quota_guard"):
        guard.check({"x-tenant-id": "acme"}, {})
    assert any("audit write failed" in r.message for r in caplog.records)
