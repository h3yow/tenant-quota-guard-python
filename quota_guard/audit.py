"""审计记录：每次判定都留下可追溯、按租户隔离的记录。"""

import threading
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AuditRecord:
    """一次判定留下的可追溯记录。

    ``reason`` 为稳定的原因码（与 errors 模块中的 reason 对齐），
    ``detail`` 为人类可读的判定依据，不包含其他租户的信息。
    """

    tenant_id: str
    allowed: bool
    reason: str
    detail: str = ""


class AuditSink(Protocol):
    """审计记录写入端协议。"""

    def write(self, record: AuditRecord) -> None:
        ...


@dataclass
class InMemoryAuditSink:
    """进程内审计收集器，主要用于测试与本地验证。"""

    records: list[AuditRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def write(self, record: AuditRecord) -> None:
        with self._lock:
            self.records.append(record)

    def for_tenant(self, tenant_id: str) -> list[AuditRecord]:
        """按租户过滤记录，验证租户间审计隔离。"""
        with self._lock:
            return [r for r in self.records if r.tenant_id == tenant_id]
