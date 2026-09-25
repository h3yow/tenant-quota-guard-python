"""租户身份解析：缺失、非法、冲突必须可区分地被拒绝。"""

import logging

import pytest

from quota_guard.errors import (
    ConflictingIdentityError,
    InvalidIdentityError,
    MissingIdentityError,
)
from quota_guard.identity import parse_identity

logger = logging.getLogger("quota_guard.test")


def test_header_identity_ok():
    identity = parse_identity({"x-tenant-id": "acme"}, {})
    logger.info("input headers=%s query=%s -> tenant=%s",
                {"x-tenant-id": "acme"}, {}, identity.tenant_id)
    assert identity.tenant_id == "acme"


def test_query_identity_ok():
    assert parse_identity({}, {"tenant_id": "acme"}).tenant_id == "acme"


def test_consistent_sources_ok():
    identity = parse_identity({"x-tenant-id": "acme"}, {"tenant_id": "acme"})
    assert identity.tenant_id == "acme"


def test_missing_identity():
    with pytest.raises(MissingIdentityError) as exc_info:
        parse_identity({}, {})
    logger.info("input headers={} query={} -> rejected reason=%s",
                exc_info.value.reason)
    assert exc_info.value.reason == "missing_identity"


@pytest.mark.parametrize("raw", ["", "   ", "1abc", "bad id", "a" * 65, "租户"])
def test_invalid_identity(raw):
    with pytest.raises(InvalidIdentityError) as exc_info:
        parse_identity({"x-tenant-id": raw}, {})
    logger.info("input header x-tenant-id=%r -> rejected reason=%s",
                raw, exc_info.value.reason)
    assert exc_info.value.reason == "invalid_identity"


def test_conflicting_identity():
    with pytest.raises(ConflictingIdentityError) as exc_info:
        parse_identity({"x-tenant-id": "acme"}, {"tenant_id": "other"})
    logger.info("input header=acme query=other -> rejected reason=%s",
                exc_info.value.reason)
    assert exc_info.value.reason == "conflicting_identity"
