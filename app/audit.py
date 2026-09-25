"""审计记录：每个租户的判定依据可追溯，且租户间严格隔离。"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditRecord:
    """一条判定审计记录。"""

    tenant_id: str
    decision: str  # "allowed" | "rejected"
    reason: str
    detail: str


class AuditStoreUnavailable(Exception):
    """审计后端写入失败。"""


class AuditRecorder:
    """内存审计记录器，按租户隔离存储。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, list[AuditRecord]] = {}

    def record(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.setdefault(record.tenant_id, []).append(record)

    def records_for(self, tenant_id: str) -> list[AuditRecord]:
        """仅返回指定租户的审计记录，租户间不可互查。"""
        with self._lock:
            return list(self._records.get(tenant_id, []))
