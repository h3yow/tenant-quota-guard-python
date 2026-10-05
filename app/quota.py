"""按租户的固定窗口配额计数器。

窗口语义（确定且可复现）：
- 每个租户使用自己生效的 window_seconds：
  window_start = floor(now / window_seconds) * window_seconds。
- 进入新窗口时该租户的计数自动重置。
- 同一租户并发下通过锁保证不超放；不同租户使用各自桶与各自窗口，互不影响。

状态回收（防止历史租户状态无限堆积）：
- reclaim_expired(now)：只清理"窗口已经完整过去"的桶。这类桶的计数对当前及
  未来判定本就无效（下次判定必然重置），因此清理与并发判定同时进行时结果一致，
  不会在窗口边界附近漏计或误判。
- reclaim_tenants(ids)：显式清理已确认不再活跃的租户，会连当前窗口的计数一起
  移除；只对确定下线的租户使用。
- 两种清理与 try_acquire 共用同一把锁，清理-判定之间不会交错出不一致状态。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Iterable

from .config import QuotaSettings


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
    """内存版固定窗口计数器，按租户隔离、按租户生效窗口计数。"""

    def __init__(
        self,
        limit: int | None = None,
        window_seconds: float | None = None,
        settings: QuotaSettings | None = None,
    ) -> None:
        if settings is None:
            if limit is None or window_seconds is None:
                raise ValueError("either settings or both limit and window_seconds are required")
            settings = QuotaSettings(default_limit=limit, default_window_seconds=window_seconds)
        self._settings = settings
        self._lock = threading.Lock()
        # tenant_id -> (window_index, count)，window_index 按该租户自己的窗口长度计算。
        self._buckets: dict[str, tuple[int, int]] = {}

    def _window_index(self, tenant_id: str, now: float) -> int:
        return math.floor(now / self._settings.rule_for(tenant_id).window_seconds)

    def try_acquire(self, tenant_id: str, now: float) -> QuotaDecision:
        """尝试为租户占用一个配额。并发下不得超放。"""
        rule = self._settings.rule_for(tenant_id)
        idx = math.floor(now / rule.window_seconds)
        window_start = idx * rule.window_seconds
        retry_after = window_start + rule.window_seconds - now

        with self._lock:
            bucket_idx, count = self._buckets.get(tenant_id, (idx, 0))
            if bucket_idx != idx:
                # 窗口推进：重置计数。
                bucket_idx, count = idx, 0

            if count >= rule.limit:
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
                remaining=rule.limit - count,
                retry_after=None,
                window_start=window_start,
            )

    def reclaim_expired(self, now: float) -> list[str]:
        """清理窗口已经完整过去的租户桶，返回被清理的租户列表。

        只移除 bucket_idx < 当前窗口索引 的桶：这些计数对当前及未来判定
        没有任何影响，因此与并发 try_acquire 同时进行也不会改变判定结果。
        """
        reclaimed: list[str] = []
        with self._lock:
            for tenant_id, (bucket_idx, _count) in list(self._buckets.items()):
                current_idx = self._window_index(tenant_id, now)
                if bucket_idx < current_idx:
                    del self._buckets[tenant_id]
                    reclaimed.append(tenant_id)
        return reclaimed

    def reclaim_tenants(self, tenant_ids: Iterable[str]) -> list[str]:
        """显式清理已确认不再活跃的租户（含当前窗口计数），返回实际清理的租户。"""
        reclaimed: list[str] = []
        with self._lock:
            for tenant_id in tenant_ids:
                if tenant_id in self._buckets:
                    del self._buckets[tenant_id]
                    reclaimed.append(tenant_id)
        return reclaimed

    def tracked_tenants(self) -> list[str]:
        """当前仍持有计数状态的租户（用于观测与测试）。"""
        with self._lock:
            return sorted(self._buckets)
