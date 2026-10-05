"""按租户的固定窗口配额计数器。

窗口语义（确定且可复现）：
- 窗口起点按该租户生效的 window_seconds 对齐：
  window_start = floor(now / window_seconds) * window_seconds。
- 进入新窗口时计数自动重置。
- 同一租户并发下通过锁保证不超放；不同租户使用各自桶与各自配置，互不影响。

状态回收：
- reclaim_idle(now)：仅清理“窗口已过期”的桶。过期桶对后续判定没有任何影响
  （新请求落入新窗口本就会重置计数），因此回收与正常判定并发进行时结果一致，
  窗口边界附近也不会漏计或误判。
- evict_tenant(tenant_id)：强制清理已下线租户的状态。调用方需确保该租户
  不再活跃；清理在锁内原子完成，不影响其他租户。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Mapping

from .config import TenantQuotaConfig


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
    """内存版固定窗口计数器，按租户隔离，支持按租户覆盖上限与窗口。"""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        tenant_overrides: Mapping[str, TenantQuotaConfig] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        # TenantQuotaConfig 自身已在构造时校验；此处仅拷贝，避免外部后续修改。
        self._overrides: dict[str, TenantQuotaConfig] = dict(tenant_overrides or {})
        self._lock = threading.Lock()
        # tenant_id -> (window_index, count)
        self._buckets: dict[str, tuple[int, int]] = {}

    def _limit_for(self, tenant_id: str) -> int:
        override = self._overrides.get(tenant_id)
        return override.limit if override is not None else self._limit

    def _window_for(self, tenant_id: str) -> float:
        override = self._overrides.get(tenant_id)
        return override.window_seconds if override is not None else self._window_seconds

    @staticmethod
    def _window_index(now: float, window_seconds: float) -> int:
        return math.floor(now / window_seconds)

    def try_acquire(self, tenant_id: str, now: float) -> QuotaDecision:
        """尝试为租户占用一个配额。并发下不得超放。"""
        limit = self._limit_for(tenant_id)
        window_seconds = self._window_for(tenant_id)
        idx = self._window_index(now, window_seconds)
        window_start = idx * window_seconds
        retry_after = window_start + window_seconds - now

        with self._lock:
            bucket_idx, count = self._buckets.get(tenant_id, (idx, 0))
            if bucket_idx != idx:
                # 窗口推进：重置计数。
                bucket_idx, count = idx, 0

            if count >= limit:
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
                remaining=limit - count,
                retry_after=None,
                window_start=window_start,
            )

    def reclaim_idle(self, now: float) -> list[str]:
        """清理窗口已过期的租户桶，返回被清理的租户列表。

        一致性论证：被清理的桶其窗口严格早于当前窗口，任何后续请求都会落入
        更新窗口并重置计数，因此删除它不会改变任何未来判定结果；清理与
        try_acquire 在同一把锁下互斥，窗口边界附近也不会出现漏计或误判。
        """
        removed: list[str] = []
        with self._lock:
            for tenant_id, (bucket_idx, _count) in list(self._buckets.items()):
                current_idx = self._window_index(now, self._window_for(tenant_id))
                if bucket_idx < current_idx:
                    del self._buckets[tenant_id]
                    removed.append(tenant_id)
        return removed

    def evict_tenant(self, tenant_id: str) -> bool:
        """强制清理指定租户的状态（用于租户已确认下线）。返回是否确有状态被清理。"""
        with self._lock:
            return self._buckets.pop(tenant_id, None) is not None

    def tracked_tenants(self) -> list[str]:
        """当前仍持有计数状态的租户（供运维观察与测试核对）。"""
        with self._lock:
            return sorted(self._buckets)
