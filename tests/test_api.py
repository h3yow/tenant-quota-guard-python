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


@pytest.fixture()
def client_with_overrides():
    # 带租户覆盖与显式降级配置的治理实例。
    from app.config import DegradationPolicy, GuardConfig, TenantQuotaConfig

    main.guard_config = GuardConfig(
        default_limit=5,
        default_window_seconds=60.0,
        default_policy=DegradationPolicy.FAIL_CLOSED,
        unavailable_retry_after=3.0,
        tenant_overrides={
            "tenant-vip": TenantQuotaConfig(limit=8, window_seconds=60.0),
        },
    )
    main.quota_counter = QuotaCounter(
        limit=main.guard_config.default_limit,
        window_seconds=main.guard_config.default_window_seconds,
        tenant_overrides=main.guard_config.tenant_overrides,
    )
    main.audit_recorder = AuditRecorder()
    main.quota_guard = QuotaGuard(
        main.quota_counter, main.audit_recorder, config=main.guard_config
    )
    return TestClient(main.app)


def test_per_tenant_limit_over_http(client_with_overrides):
    client = client_with_overrides
    # 默认租户上限 5，vip 租户上限 8。
    for _ in range(5):
        assert client.get("/", headers={"x-tenant-id": "tenant-a"}).status_code == 200
    resp_default = client.get("/", headers={"x-tenant-id": "tenant-a"})
    _show("default_limit_5", resp_default)
    assert resp_default.status_code == 429

    for i in range(8):
        resp = client.get("/", headers={"x-tenant-id": "tenant-vip"})
        assert resp.status_code == 200, f"vip req#{i} unexpectedly {resp.status_code}"
    resp_vip = client.get("/", headers={"x-tenant-id": "tenant-vip"})
    _show("vip_limit_8", resp_vip)
    assert resp_vip.status_code == 429


def test_identity_rejections_are_audited(client_with_overrides):
    client = client_with_overrides
    resp_missing = client.get("/")
    resp_invalid = client.get("/", headers={"x-tenant-id": "!!bad!!"})
    resp_conflict = client.get("/", headers={"x-tenant-id": "tenant-a"},
                               params={"tenant_id": "tenant-b"})
    _show("missing", resp_missing)
    _show("invalid", resp_invalid)
    _show("conflict", resp_conflict)
    assert (resp_missing.status_code, resp_invalid.status_code,
            resp_conflict.status_code) == (401, 400, 409)

    from app.guard import UNIDENTIFIED_TENANT
    unidentified = main.audit_recorder.records_for(UNIDENTIFIED_TENANT)
    a_records = main.audit_recorder.records_for("tenant-a")
    b_records = main.audit_recorder.records_for("tenant-b")
    for r in unidentified + a_records + b_records:
        print(f"[audit] {r}")
    # 缺失与非法格式都归入保留键；冲突在两个候选租户下各自留痕。
    assert [r.reason for r in unidentified] == ["tenant_missing", "tenant_invalid"]
    assert [r.reason for r in a_records] == ["tenant_conflict"]
    assert [r.reason for r in b_records] == ["tenant_conflict"]
    # 隔离：未涉及的租户无任何记录。
    assert main.audit_recorder.records_for("tenant-c") == []


def test_backend_failure_503_with_retry_hint(client_with_overrides):
    client = client_with_overrides

    class _Down:
        def try_acquire(self, tenant_id, now):
            raise ConnectionError("connection refused")

    main.quota_guard = QuotaGuard(_Down(), main.audit_recorder, config=main.guard_config)
    resp = client.get("/", headers={"x-tenant-id": "tenant-a"})
    _show("backend_down_fail_closed", resp)
    assert resp.status_code == 503
    assert resp.json()["error"]["reason"] == "quota_unavailable"
    assert resp.headers["Retry-After"] == "3"  # unavailable_retry_after=3.0
    # 拒绝依据已留痕。
    records = main.audit_recorder.records_for("tenant-a")
    print(f"[audit] {records[0]}")
    assert records[0].reason == "quota_unavailable"
