"""按租户的固定窗口配额计数器。

窗口语义（确定且可复现）：
- 窗口起点按 window_seconds 对齐：window_start = floor(now / window_seconds) * window_seconds。
- 进入新窗口时计数自动重置。
- 同一租户并发下通过锁保证不超放；不同租户使用各自桶，互不影响。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaDecision:
    """一次配额判定的结果。"""

    allowed: bool
    remaining: int
    retry_after: float | None
    window_start: float


class QuotaStoreUnavailable(Exception):
    """计数后端不可用（用于真实后端故障与降级路径测试）。"""


class QuotaCounter:
    """内存版固定窗口计数器，按租户隔离。"""

    def __init__(self, limit: int, window_seconds: float) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        # tenant_id -> (window_index, count)
        self._buckets: dict[str, tuple[int, int]] = {}

    def _window_index(self, now: float) -> int:
        return math.floor(now / self._window_seconds)

    def try_acquire(self, tenant_id: str, now: float) -> QuotaDecision:
        """尝试为租户占用一个配额。并发下不得超放。"""
        idx = self._window_index(now)
        window_start = idx * self._window_seconds
        retry_after = window_start + self._window_seconds - now

        with self._lock:
            bucket_idx, count = self._buckets.get(tenant_id, (idx, 0))
            if bucket_idx != idx:
                # 窗口推进：重置计数。
                bucket_idx, count = idx, 0

            if count >= self._limit:
                self._buckets[tenant_id] = (bucket_idx, count)
                return QuotaDecision(
                    allowed=False,
                    remaining=0,
                    retry_after=retry_after,
                    window_start=window_start,
                )

            count += 1
            self._buckets[tenant_id] = (bucket_idx, count)
            return QuotaDecision(
                allowed=True,
                remaining=self._limit - count,
                retry_after=None,
                window_start=window_start,
            )
