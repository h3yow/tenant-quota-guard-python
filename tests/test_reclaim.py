"""状态回收：过期桶可清理、清理与判定并发结果一致、强制清退不影响其他租户。"""

import threading

from app.config import TenantQuotaConfig
from app.quota import QuotaCounter


def test_reclaim_idle_removes_only_expired_buckets():
    counter = QuotaCounter(limit=2, window_seconds=10.0)
    counter.try_acquire("tenant-old", now=0.0)    # 窗口 [0,10)
    counter.try_acquire("tenant-live", now=95.0)  # 窗口 [90,100)

    removed = counter.reclaim_idle(now=95.0)
    print(f"\n[reclaim] now=95.0 removed={removed} tracked={counter.tracked_tenants()}")
    assert removed == ["tenant-old"]
    assert counter.tracked_tenants() == ["tenant-live"]


def test_reclaim_idle_does_not_change_decisions():
    counter = QuotaCounter(limit=2, window_seconds=10.0)
    counter.try_acquire("tenant-a", now=0.0)
    counter.try_acquire("tenant-a", now=1.0)
    blocked = counter.try_acquire("tenant-a", now=2.0)
    assert not blocked.allowed

    # 同一窗口内回收：桶未过期，什么都不清，超限判定不受影响。
    assert counter.reclaim_idle(now=2.0) == []
    still_blocked = counter.try_acquire("tenant-a", now=3.0)
    print(f"\n[reclaim] same-window reclaim -> still_blocked={not still_blocked.allowed}")
    assert not still_blocked.allowed

    # 窗口过期后回收：清理的是“本就会重置”的状态，新窗口判定与未回收时一致。
    removed = counter.reclaim_idle(now=10.0)
    print(f"[reclaim] after expiry removed={removed}")
    assert removed == ["tenant-a"]
    fresh = counter.try_acquire("tenant-a", now=10.0)
    assert fresh.allowed and fresh.remaining == 1


def test_reclaim_at_window_boundary_counts_correctly():
    """窗口边界附近：回收过期桶后，新窗口的计数从零开始且不超放。"""
    counter = QuotaCounter(limit=3, window_seconds=10.0)
    for _ in range(3):
        counter.try_acquire("tenant-a", now=9.9)
    assert not counter.try_acquire("tenant-a", now=9.9).allowed

    counter.reclaim_idle(now=10.0)  # 边界点：旧桶过期被清
    results = [counter.try_acquire("tenant-a", now=10.0) for _ in range(4)]
    allowed = sum(r.allowed for r in results)
    print(f"\n[reclaim] boundary allowed={allowed}/4 after reclaim")
    assert allowed == 3  # 恰好重置为上限，不多不少


def test_concurrent_reclaim_and_acquire_is_consistent():
    """回收与请求并发进行：同窗口内回收不清活桶，放行数严格等于上限。"""
    limit, threads = 50, 200
    counter = QuotaCounter(limit=limit, window_seconds=60.0)
    outcomes: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads + 1)

    def worker():
        barrier.wait()
        decision = counter.try_acquire("tenant-a", now=1000.0)
        with lock:
            outcomes.append(decision.allowed)

    def reclaimer():
        barrier.wait()
        for _ in range(50):
            counter.reclaim_idle(now=1000.0)  # 与请求同窗口：不应清掉任何活桶

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    ts.append(threading.Thread(target=reclaimer))
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    allowed = sum(outcomes)
    print(f"\n[reclaim] concurrent same-window allowed={allowed}/{threads} (limit {limit})")
    assert allowed == limit
    assert counter.tracked_tenants() == ["tenant-a"]


def test_evict_tenant_removes_only_that_tenant():
    counter = QuotaCounter(
        limit=1, window_seconds=10.0,
        tenant_overrides={"tenant-vip": TenantQuotaConfig(limit=1, window_seconds=10.0)},
    )
    counter.try_acquire("tenant-a", now=0.0)
    counter.try_acquire("tenant-vip", now=0.0)

    assert counter.evict_tenant("tenant-a")
    assert not counter.evict_tenant("tenant-a")  # 已清，再次清理返回 False
    print(f"\n[reclaim] evict tenant-a -> tracked={counter.tracked_tenants()}")
    assert counter.tracked_tenants() == ["tenant-vip"]

    # 其他租户状态不受影响：vip 仍被自己窗口内的计数拦截。
    assert not counter.try_acquire("tenant-vip", now=1.0).allowed
