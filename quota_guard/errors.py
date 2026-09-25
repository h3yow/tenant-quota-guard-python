"""治理层错误类型：每种失败原因都有可区分的类型与原因码。"""


class QuotaGuardError(Exception):
    """治理层错误基类，携带稳定的 reason 码供调用方区分。"""

    reason: str = "internal_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class IdentityError(QuotaGuardError):
    """租户身份相关错误基类。"""

    reason = "identity_error"


class MissingIdentityError(IdentityError):
    reason = "missing_identity"


class InvalidIdentityError(IdentityError):
    reason = "invalid_identity"


class ConflictingIdentityError(IdentityError):
    reason = "conflicting_identity"


class QuotaExceededError(QuotaGuardError):
    reason = "quota_exceeded"

    def __init__(self, message: str = "", retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class QuotaStoreError(QuotaGuardError):
    """计数后端不可用或写入失败。"""

    reason = "quota_store_error"


class AuditError(QuotaGuardError):
    """审计记录写入失败。"""

    reason = "audit_error"
