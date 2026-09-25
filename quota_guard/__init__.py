"""多租户请求配额治理层。"""

from quota_guard.errors import (
    ConflictingIdentityError,
    IdentityError,
    InvalidIdentityError,
    MissingIdentityError,
    QuotaExceededError,
    QuotaGuardError,
)
from quota_guard.guard import Decision, QuotaGuard
from quota_guard.identity import TenantIdentity, parse_identity
from quota_guard.quota import InMemoryQuotaStore, QuotaStore

__all__ = [
    "ConflictingIdentityError",
    "Decision",
    "IdentityError",
    "InMemoryQuotaStore",
    "InvalidIdentityError",
    "MissingIdentityError",
    "QuotaExceededError",
    "QuotaGuard",
    "QuotaGuardError",
    "QuotaStore",
    "TenantIdentity",
    "parse_identity",
]
