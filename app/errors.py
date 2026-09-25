"""治理层错误类型定义。

所有对外暴露的错误都可区分原因，且不携带内部堆栈或跨租户信息。
"""

from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    """请求被拒绝的可区分原因。"""

    TENANT_MISSING = "tenant_missing"
    TENANT_INVALID = "tenant_invalid"
    TENANT_CONFLICT = "tenant_conflict"
    QUOTA_EXCEEDED = "quota_exceeded"
    QUOTA_UNAVAILABLE = "quota_unavailable"


class QuotaGuardError(Exception):
    """治理层判定拒绝时抛出的异常。"""

    def __init__(self, reason: RejectReason, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after = retry_after
