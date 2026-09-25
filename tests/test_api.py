"""HTTP 接入层：状态码可区分、带重试提示、不泄露内部信息、租户隔离。"""

import logging

import pytest
from fastapi.testclient import TestClient

from main import app, guard

logger = logging.getLogger("quota_guard.test")


@pytest.fixture
def client():
    return TestClient(app)


def get_and_log(client, path="/", **kwargs):
    resp = client.get(path, **kwargs)
    logger.info("input path=%s kwargs=%s -> status=%d body=%s",
                path, kwargs, resp.status_code, resp.json())
    return resp


def test_missing_identity_400(client):
    resp = get_and_log(client)
    assert resp.status_code == 400
    assert resp.json() == {"error": "missing_identity"}


def test_conflicting_identity_400(client):
    resp = get_and_log(client, headers={"X-Tenant-Id": "a"},
                        params={"tenant_id": "b"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "conflicting_identity"


def test_allowed_request_200(client):
    resp = get_and_log(client, headers={"X-Tenant-Id": "api-tenant-ok"})
    assert resp.status_code == 200


def test_quota_exceeded_429_with_retry_after(client):
    # 复用 main 中的 guard：limit=100，窗口 60s
    headers = {"X-Tenant-Id": "api-tenant-burst"}
    for _ in range(100):
        resp = client.get("/", headers=headers)
        assert resp.status_code == 200
    resp = get_and_log(client, headers=headers)
    assert resp.status_code == 429
    assert resp.json() == {"error": "quota_exceeded"}
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0


def test_tenant_isolation_over_http(client):
    headers_a = {"X-Tenant-Id": "api-tenant-burst"}  # 上个用例已打满
    resp_a = get_and_log(client, headers=headers_a)
    resp_b = get_and_log(client, headers={"X-Tenant-Id": "api-tenant-fresh"})
    assert resp_a.status_code == 429
    assert resp_b.status_code == 200  # 租户互不影响


def test_error_response_has_no_internals(client):
    resp = get_and_log(client)
    body = resp.text
    assert "Traceback" not in body
    assert "quota_guard" not in body
