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
