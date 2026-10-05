"""端到端：HTTP 层状态码可区分、超限带重试提示、多租户并发互不超放。"""

import threading

import pytest
from fastapi.testclient import TestClient

import main
from app.guard import DegradationPolicy, QuotaGuard
from app.quota import QuotaCounter
from app.audit import AuditRecorder


@pytest.fixture()
def client():
    # 每个用例使用独立的小配额治理实例，避免相互影响。
    main.quota_counter = QuotaCounter(limit=5, window_seconds=60.0)
    main.audit_recorder = AuditRecorder()
    main.quota_guard = QuotaGuard(
        main.quota_counter, main.audit_recorder, policy=DegradationPolicy.FAIL_CLOSED
    )
    return TestClient(main.app)


def _show(case: str, resp) -> None:
    print(f"\n[api] case={case} -> status={resp.status_code} body={resp.json()} "
          f"retry_after={resp.headers.get('Retry-After')}")


def test_missing_identity_401(client):
    resp = client.get("/")
    _show("missing", resp)
    assert resp.status_code == 401
    assert resp.json()["error"]["reason"] == "tenant_missing"


def test_invalid_identity_400(client):
    resp = client.get("/", headers={"x-tenant-id": "!!bad!!"})
    _show("invalid", resp)
    assert resp.status_code == 400
    assert resp.json()["error"]["reason"] == "tenant_invalid"


def test_conflicting_identity_409(client):
    resp = client.get("/", headers={"x-tenant-id": "tenant-a"}, params={"tenant_id": "tenant-b"})
    _show("conflict", resp)
    assert resp.status_code == 409
    assert resp.json()["error"]["reason"] == "tenant_conflict"


def test_quota_exceeded_429_with_retry_after(client):
    for i in range(5):
        assert client.get("/", headers={"x-tenant-id": "tenant-a"}).status_code == 200
    resp = client.get("/", headers={"x-tenant-id": "tenant-a"})
    _show("exceeded", resp)
    assert resp.status_code == 429
    assert resp.json()["error"]["reason"] == "quota_exceeded"
    assert "Retry-After" in resp.headers

    # 其他租户不受影响。
    other = client.get("/", headers={"x-tenant-id": "tenant-b"})
    _show("other_tenant_unaffected", other)
    assert other.status_code == 200


def test_concurrent_multi_tenant_burst(client):
    """两租户并发抢占：各自放行数都不超上限，且互不影响。"""
    results: dict[str, list[int]] = {"tenant-a": [], "tenant-b": []}
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(tenant: str):
        barrier.wait()
        resp = client.get("/", headers={"x-tenant-id": tenant})
        with lock:
            results[tenant].append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("tenant-a", "tenant-b") for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for tenant, codes in results.items():
        ok = sum(1 for c in codes if c == 200)
        rejected = sum(1 for c in codes if c == 429)
        print(f"\n[api] burst tenant={tenant} -> ok={ok} rejected={rejected}")
        assert ok == 5
        assert rejected == 5


# ---- 新增行为：按租户配置、身份拒绝审计、后端故障降级响应 ----

from app.config import QuotaSettings, TenantQuotaRule
from app.guard import UNKNOWN_TENANT_KEY


@pytest.fixture()
def tenant_client():
    """带按租户覆盖配置的治理实例：tenant-vip 上限 2 次 / 10 秒窗口。"""
    settings = QuotaSettings(
        default_limit=5,
        default_window_seconds=60.0,
        default_policy=DegradationPolicy.FAIL_CLOSED,
        tenant_overrides={"tenant-vip": TenantQuotaRule(limit=2, window_seconds=10.0)},
    )
    main.quota_settings = settings
    main.quota_counter = QuotaCounter(settings=settings)
    main.audit_recorder = AuditRecorder()
    main.quota_guard = QuotaGuard(main.quota_counter, main.audit_recorder, settings=settings)
    return TestClient(main.app)


def test_per_tenant_limit_overrides_default(tenant_client):
    # tenant-vip 用自己的上限 2：第 3 次即 429。
    assert tenant_client.get("/", headers={"x-tenant-id": "tenant-vip"}).status_code == 200
    assert tenant_client.get("/", headers={"x-tenant-id": "tenant-vip"}).status_code == 200
    vip = tenant_client.get("/", headers={"x-tenant-id": "tenant-vip"})
    _show("vip_override_limit", vip)
    assert vip.status_code == 429

    # 默认租户不受覆盖影响，也不受 vip 计数影响。
    for _ in range(5):
        assert tenant_client.get("/", headers={"x-tenant-id": "tenant-a"}).status_code == 200
    plain = tenant_client.get("/", headers={"x-tenant-id": "tenant-a"})
    _show("default_limit", plain)
    assert plain.status_code == 429


def test_identity_rejection_is_audited(tenant_client):
    resp = tenant_client.get("/")
    _show("missing_audited", resp)
    assert resp.status_code == 401
    records = main.audit_recorder.records_for(UNKNOWN_TENANT_KEY)
    for r in records:
        print(f"[audit] {r}")
    assert [r.reason for r in records] == ["tenant_missing"]
    # 审计按归属键隔离：其他租户桶里没有这条记录。
    assert main.audit_recorder.records_for("tenant-a") == []


def test_backend_failure_fail_closed_503_with_retry_hint(tenant_client):
    class _Down:
        def try_acquire(self, tenant_id, now):
            raise TimeoutError("backend read timed out")

    main.quota_guard = QuotaGuard(_Down(), main.audit_recorder, settings=main.quota_settings)
    resp = tenant_client.get("/", headers={"x-tenant-id": "tenant-a"})
    _show("backend_timeout_fail_closed", resp)
    assert resp.status_code == 503
    assert resp.json()["error"]["reason"] == "quota_unavailable"
    # 拒绝携带重试提示，响应头与响应体一致。
    assert resp.json()["error"]["retry_after"] == 60.0
    assert "Retry-After" in resp.headers
    # 判定依据已审计。
    record = main.audit_recorder.records_for("tenant-a")[0]
    print(f"[audit] {record}")
    assert record.decision == "rejected" and record.reason == "quota_unavailable"


def test_backend_failure_fail_open_per_tenant(tenant_client):
    class _Down:
        def try_acquire(self, tenant_id, now):
            raise ConnectionError("connection refused")

    main.quota_guard = QuotaGuard(_Down(), main.audit_recorder, settings=main.quota_settings)
    # tenant-vip 未单独配置策略时走默认 fail_closed；这里给它换 fail_open 配置再验。
    settings = QuotaSettings(
        default_limit=5,
        default_window_seconds=60.0,
        default_policy=DegradationPolicy.FAIL_CLOSED,
        tenant_overrides={"tenant-vip": TenantQuotaRule(
            limit=2, window_seconds=10.0, policy=DegradationPolicy.FAIL_OPEN)},
    )
    main.quota_guard = QuotaGuard(_Down(), main.audit_recorder, settings=settings)

    vip = tenant_client.get("/", headers={"x-tenant-id": "tenant-vip"})
    plain = tenant_client.get("/", headers={"x-tenant-id": "tenant-a"})
    _show("vip_fail_open", vip)
    _show("plain_fail_closed", plain)
    assert vip.status_code == 200
    assert vip.headers["X-Quota-Degraded"] == "true"
    assert plain.status_code == 503
    # 对外结果与各自策略语义一致，且都有审计依据。
    assert main.audit_recorder.records_for("tenant-vip")[0].reason == "degraded_fail_open"
    assert main.audit_recorder.records_for("tenant-a")[0].reason == "quota_unavailable"
