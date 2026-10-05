"""后端故障降级：任意故障类型都按该租户生效策略处理，判定/审计/响应语义一致。"""

import pytest

from app.audit import AuditRecorder
from app.config import DegradationPolicy, QuotaSettings, TenantQuotaRule
from app.errors import RejectReason
from app.guard import UNKNOWN_TENANT_KEY, QuotaGuard
from app.identity import TenantIdentity
from app.quota import QuotaCounter


class _FlakyCounter(QuotaCounter):
    """以指定异常模拟后端故障（连接失败 / 超时 / 未预料的运行错误）。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def try_acquire(self, tenant_id, now):
        raise self._exc


def _settings(default_policy=DegradationPolicy.FAIL_CLOSED) -> QuotaSettings:
    return QuotaSettings(
        default_limit=10,
        default_window_seconds=60.0,
        default_policy=default_policy,
        tenant_overrides={
            "tenant-vip": TenantQuotaRule(limit=10, window_seconds=30.0,
                                          policy=DegradationPolicy.FAIL_OPEN),
        },
    )


def _show(case: str, tenant: str, decision) -> None:
    print(f"\n[degrade] case={case} tenant={tenant} -> allowed={decision.allowed} "
          f"reason={decision.reason} degraded={decision.degraded} "
          f"retry_after={decision.retry_after}")


@pytest.mark.parametrize("exc", [
    ConnectionError("connection refused"),
    TimeoutError("backend read timed out"),
    RuntimeError("unexpected backend bug"),
])
def test_any_backend_failure_follows_default_fail_closed(exc):
    """默认策略 fail-closed：不论后端以哪种方式失败，都拒绝且语义一致。"""
    recorder = AuditRecorder()
    guard = QuotaGuard(_FlakyCounter(exc), recorder, settings=_settings())
    decision = guard.check(TenantIdentity("tenant-a"), now=0.0)
    _show(type(exc).__name__, "tenant-a", decision)

    assert not decision.allowed
    assert decision.reason == RejectReason.QUOTA_UNAVAILABLE.value
    assert decision.degraded
    # 拒绝携带重试提示（该租户生效窗口长度）。
    assert decision.retry_after == 60.0
    # 审计与判定语义一致。
    record = recorder.records_for("tenant-a")[0]
    print(f"[audit] {record}")
    assert record.decision == "rejected"
    assert record.reason == RejectReason.QUOTA_UNAVAILABLE.value
    assert type(exc).__name__ in record.detail


@pytest.mark.parametrize("exc", [
    ConnectionError("connection refused"),
    TimeoutError("backend read timed out"),
    RuntimeError("unexpected backend bug"),
])
def test_any_backend_failure_follows_tenant_fail_open(exc):
    """tenant-vip 单独配置 fail_open：同样的故障对它放行，互不影响其他租户。"""
    recorder = AuditRecorder()
    guard = QuotaGuard(_FlakyCounter(exc), recorder, settings=_settings())
    vip = guard.check(TenantIdentity("tenant-vip"), now=0.0)
    plain = guard.check(TenantIdentity("tenant-a"), now=0.0)
    _show(type(exc).__name__, "tenant-vip", vip)
    _show(type(exc).__name__, "tenant-a", plain)

    assert vip.allowed and vip.reason == "degraded_fail_open" and vip.degraded
    assert not plain.allowed and plain.reason == RejectReason.QUOTA_UNAVAILABLE.value
    # 各自审计归属各自租户。
    assert recorder.records_for("tenant-vip")[0].reason == "degraded_fail_open"
    assert recorder.records_for("tenant-a")[0].reason == RejectReason.QUOTA_UNAVAILABLE.value


def test_identity_rejections_are_audited_and_isolated():
    """身份校验未通过的拒绝也留下可追溯依据，且按归属键隔离。"""
    recorder = AuditRecorder()
    guard = QuotaGuard(QuotaCounter(limit=5, window_seconds=60.0), recorder)

    missing = guard.check_request({}, {}, now=0.0)
    invalid = guard.check_request({"x-tenant-id": "!!bad!!"}, {}, now=0.0)
    conflict = guard.check_request({"x-tenant-id": "tenant-a"}, {"tenant_id": "tenant-b"}, now=0.0)
    ok = guard.check_request({"x-tenant-id": "tenant-a"}, {}, now=0.0)
    for case, d in [("missing", missing), ("invalid", invalid),
                    ("conflict", conflict), ("ok", ok)]:
        _show(case, d.tenant_id, d)

    assert missing.reason == RejectReason.TENANT_MISSING.value
    assert invalid.reason == RejectReason.TENANT_INVALID.value
    assert conflict.reason == RejectReason.TENANT_CONFLICT.value
    assert ok.allowed

    # 身份缺失：归属 <unknown> 桶；格式非法：归属原始值桶；冲突：归属请求头给出的租户。
    unknown_records = recorder.records_for(UNKNOWN_TENANT_KEY)
    bad_records = recorder.records_for("!!bad!!")
    a_records = recorder.records_for("tenant-a")
    for r in unknown_records + bad_records + a_records:
        print(f"[audit] {r}")
    assert [r.reason for r in unknown_records] == [RejectReason.TENANT_MISSING.value]
    assert [r.reason for r in bad_records] == [RejectReason.TENANT_INVALID.value]
    # tenant-a 桶里有冲突拒绝与正常放行两条，互不占别的租户的桶。
    assert [r.reason for r in a_records] == [
        RejectReason.TENANT_CONFLICT.value, "within_quota",
    ]
    assert recorder.records_for("tenant-b") == []
