"""按租户的固定窗口配额计数。

窗口语义（确定且可复现）：
- 窗口为固定窗口：某租户首次计数时开启窗口，起点为当时时钟读数；
- 窗口长度 ``window_seconds`` 到期后，下一次计数自动开启新窗口并重置计数；
- 超限的请求不增加计数，返回的 ``retry_after`` 为距窗口重置的秒数；
- 时钟可注入，测试中用假时钟即可完整复现窗口推进与重置。

隔离性：每个租户独立的窗口状态与锁，任一租户的计数变化不影响其他租户。
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WindowState:
    """某租户当前窗口的计数快照。"""

    window_start: float
    count: int
    limit: int
    retry_after: float
    allowed: bool


class QuotaStore(Protocol):
    """配额计数后端协议，便于替换为外部存储。"""

    def hit(self, tenant_id: str, limit: int, window_seconds: float) -> WindowState:
        """为租户计一次数并返回窗口状态；超限时不增加计数。"""
        ...


@dataclass
class _TenantWindow:
    window_start: float
    count: int


class InMemoryQuotaStore:
    """进程内线程安全的固定窗口计数器。

    通过“全局锁 + 每租户状态”保证同一租户并发突发下的计数原子性：
    检查窗口与递增计数在同一把锁内完成，不会出现超出上限的放行。
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._windows: dict[str, _TenantWindow] = {}

    def hit(self, tenant_id: str, limit: int, window_seconds: float) -> WindowState:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        with self._lock:
            now = self._clock()
            window = self._windows.get(tenant_id)
            if window is None or now >= window.window_start + window_seconds:
                # 首次访问或窗口到期：开启新窗口，计数重置。
                window = _TenantWindow(window_start=now, count=0)
                self._windows[tenant_id] = window

            retry_after = max(0.0, window.window_start + window_seconds - now)
            if window.count >= limit:
                # 超限：不增加计数，仅返回当前状态与重试提示。
                return WindowState(
                    window_start=window.window_start,
                    count=window.count,
                    limit=limit,
                    retry_after=retry_after,
                    allowed=False,
                )

            window.count += 1
            return WindowState(
                window_start=window.window_start,
                count=window.count,
                limit=limit,
                retry_after=retry_after,
                allowed=True,
            )
