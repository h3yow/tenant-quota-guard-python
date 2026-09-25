"""租户身份解析：缺失、非法、冲突三种原因可区分。"""

import pytest

from app.errors import QuotaGuardError, RejectReason
from app.identity import parse_tenant_identity


def _show(case: str, headers: dict, query: dict, outcome: str) -> None:
    print(f"\n[identity] case={case} headers={headers} query={query} -> {outcome}")


def test_valid_header_only():
    headers, query = {"x-tenant-id": "tenant-a"}, {}
    identity = parse_tenant_identity(headers, query)
    _show("header_only", headers, query, f"ok tenant={identity.tenant_id}")
    assert identity.tenant_id == "tenant-a"


def test_valid_query_only():
    headers, query = {}, {"tenant_id": "tenant-b"}
    identity = parse_tenant_identity(headers, query)
    _show("query_only", headers, query, f"ok tenant={identity.tenant_id}")
    assert identity.tenant_id == "tenant-b"


def test_consistent_header_and_query():
    headers = {"x-tenant-id": "tenant-a"}
    query = {"tenant_id": "tenant-a"}
    identity = parse_tenant_identity(headers, query)
    _show("consistent", headers, query, f"ok tenant={identity.tenant_id}")
    assert identity.tenant_id == "tenant-a"


def test_missing_identity():
    headers, query = {}, {}
    with pytest.raises(QuotaGuardError) as exc_info:
        parse_tenant_identity(headers, query)
    _show("missing", headers, query, f"reject reason={exc_info.value.reason}")
    assert exc_info.value.reason is RejectReason.TENANT_MISSING


@pytest.mark.parametrize("bad", ["", "A", "ab", "1abc", "a" * 33, "bad_id", "bad id"])
def test_invalid_format(bad):
    headers, query = {"x-tenant-id": bad}, {}
    with pytest.raises(QuotaGuardError) as exc_info:
        parse_tenant_identity(headers, query)
    _show("invalid", headers, query, f"reject reason={exc_info.value.reason}")
    assert exc_info.value.reason is RejectReason.TENANT_INVALID


def test_conflicting_sources():
    headers = {"x-tenant-id": "tenant-a"}
    query = {"tenant_id": "tenant-b"}
    with pytest.raises(QuotaGuardError) as exc_info:
        parse_tenant_identity(headers, query)
    _show("conflict", headers, query, f"reject reason={exc_info.value.reason}")
    assert exc_info.value.reason is RejectReason.TENANT_CONFLICT
