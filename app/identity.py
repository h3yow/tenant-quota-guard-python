"""租户身份解析与校验。

从请求头与查询参数解析租户身份：缺失、格式非法、多来源冲突分别对应不同拒绝原因。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import QuotaGuardError, RejectReason

TENANT_HEADER = "x-tenant-id"
TENANT_QUERY_PARAM = "tenant_id"

# 租户 ID 格式：字母开头，仅含小写字母、数字、中划线，长度 3~32。
_TENANT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,31}$")


@dataclass(frozen=True)
class TenantIdentity:
    """校验通过的租户身份。"""

    tenant_id: str


def _validate_format(raw: str) -> str:
    value = raw.strip()
    if not _TENANT_PATTERN.match(value):
        raise QuotaGuardError(
            RejectReason.TENANT_INVALID,
            "tenant id format is invalid",
        )
    return value


def parse_tenant_identity(headers: dict[str, str], query_params: dict[str, str]) -> TenantIdentity:
    """从请求头与查询参数解析租户身份。

    - 两者都缺失 -> TENANT_MISSING
    - 任一格式非法 -> TENANT_INVALID
    - 两者都存在但不一致 -> TENANT_CONFLICT
    """
    # headers 在 ASGI/FastAPI 中已小写化；此处再兜底一次。
    lowered = {k.lower(): v for k, v in headers.items()}
    header_value = lowered.get(TENANT_HEADER)
    query_value = query_params.get(TENANT_QUERY_PARAM)

    if header_value is None and query_value is None:
        raise QuotaGuardError(
            RejectReason.TENANT_MISSING,
            "tenant identity is missing",
        )

    header_tenant = _validate_format(header_value) if header_value is not None else None
    query_tenant = _validate_format(query_value) if query_value is not None else None

    if header_tenant is not None and query_tenant is not None and header_tenant != query_tenant:
        raise QuotaGuardError(
            RejectReason.TENANT_CONFLICT,
            "tenant identity conflict between header and query param",
        )

    return TenantIdentity(tenant_id=header_tenant or query_tenant)  # type: ignore[arg-type]
