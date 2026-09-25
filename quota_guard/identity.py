"""租户身份解析与校验。

识别方式：
- 请求头 ``X-Tenant-Id``（首选来源）；
- 查询参数 ``tenant_id``（兼容来源）。

两个来源同时存在且取值不一致时视为冲突，必须拒绝。
"""

import re
from dataclasses import dataclass

from quota_guard.errors import (
    ConflictingIdentityError,
    InvalidIdentityError,
    MissingIdentityError,
)

TENANT_HEADER = "x-tenant-id"
TENANT_QUERY_PARAM = "tenant_id"

# 租户 ID 格式：字母开头，后接字母/数字/连字符/下划线，长度 1~64。
_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class TenantIdentity:
    """已校验的租户身份。"""

    tenant_id: str


def _validate(raw: str, source: str) -> str:
    value = raw.strip()
    if not value:
        raise InvalidIdentityError(f"tenant id from {source} is blank")
    if not _TENANT_ID_PATTERN.match(value):
        raise InvalidIdentityError(
            f"tenant id from {source} has illegal format"
        )
    return value


def parse_identity(headers: dict[str, str], query: dict[str, str]) -> TenantIdentity:
    """从请求头与查询参数解析租户身份。

    - 两个来源都缺失 -> MissingIdentityError
    - 任一来源格式非法 -> InvalidIdentityError
    - 两个来源均合法但不一致 -> ConflictingIdentityError
    """
    candidates: list[tuple[str, str]] = []

    header_value = headers.get(TENANT_HEADER)
    if header_value is not None:
        candidates.append((TENANT_HEADER, _validate(header_value, TENANT_HEADER)))

    query_value = query.get(TENANT_QUERY_PARAM)
    if query_value is not None:
        candidates.append(
            (TENANT_QUERY_PARAM, _validate(query_value, TENANT_QUERY_PARAM))
        )

    if not candidates:
        raise MissingIdentityError(
            f"tenant id required via header '{TENANT_HEADER}' "
            f"or query param '{TENANT_QUERY_PARAM}'"
        )

    distinct = {value for _, value in candidates}
    if len(distinct) > 1:
        sources = ", ".join(source for source, _ in candidates)
        raise ConflictingIdentityError(
            f"conflicting tenant ids from {sources}"
        )

    return TenantIdentity(tenant_id=candidates[0][1])
