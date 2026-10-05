"""状态回收：过期状态可清理、清理与并发判定结果一致、不影响其他租户。"""

import threading

from app.config import QuotaSettings, TenantQuotaRule
from app.quota import QuotaCounter


def _counter() -> QuotaCounter:
    return QuotaCounter(settings=QuotaSettings(
        default_limit=3,
        default_window_seconds=60.0,
        tenant_overrides={"tenant-vip": TenantQuotaRule(limit=3, window_seconds=10.0)},
    ))


def test_reclaim_expired_only_drops_finished_windows():
    counter = _counter()
    counter.try_acquire("tenant-old", now=0.0)      # 窗口 [0,60)，已过去
    counter.try_acquire("tenant-vip", now=0.0)      # 窗口 [0,10)，已过去
    counter.try_acquire("tenant-active", now=100.0) # 窗口 [60,120)，仍有效
    print(f"\n[reclaim] before -> tracked={counter.tracked_tenants()}")

    reclaimed = counter.reclaim_expired(now=100.0)
    print(f"[reclaim] reclaimed={reclaimed} after -> tracked={counter.tracked_tenants()}")
    assert sorted(reclaimed) == ["tenant-old", "tenant-vip"]
    assert counter.tracked_tenants() == ["tenant-active"]


def test_reclaim_expired_respects_per_tenant_window():
    """同一时刻，窗口短的租户状态先过期，窗口长的保留。"""
    counter = _counter()
    counter.try_acquire("tenant-vip", now=0.0)   # 10s 窗口
    counter.try_acquire("tenant-a", now=0.0)     # 60s 窗口
    reclaimed = counter.reclaim_expired(now=30.0)
    print(f"\n[reclaim] at now=30 -> reclaimed={reclaimed} tracked={counter.tracked_tenants()}")
    # tenant-vip 的窗口 [0,10) 已结束；tenant-a 的窗口 [0,60) 仍在生效。
    assert reclaimed == ["tenant-vip"]
    assert counter.tracked_tenants() == ["tenant-a"]


def test_reclaim_does_not_lose_counts_within_window_boundary():
    """窗口未结束时绝不清理：清理后同窗口请求仍按原计数判定。"""
    counter = QuotaCounter(limit=2, window_seconds=10.0)
    assert counter.try_acquire("tenant-a", now=100.0).allowed
    assert counter.try_acquire("tenant-a", now=101.0).allowed
    # 窗口 [100,110) 未结束，清理不应动它。
    assert counter.reclaim_expired(now=105.0) == []
    blocked = counter.try_acquire("tenant-a", now=109.9)
    print(f"\n[reclaim] after no-op reclaim, 3rd in window -> allowed={blocked.allowed}")
    assert not blocked.allowed  # 计数没丢，第 3 次仍被拒


def test_reclaim_concurrent_with_burst_keeps_decisions_consistent():
    """清理与并发突发同时进行：放行总数仍严格等于上限。"""
    limit, threads_n = 50, 200
    counter = QuotaCounter(limit=limit, window_seconds=60.0)
    outcomes: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads_n + 1)

    def worker():
        barrier.wait()
        decision = counter.try_acquire("tenant-a", now=1000.0)
        with lock:
            outcomes.append(decision.allowed)

    def reclaimer():
        barrier.wait()
        for _ in range(50):
            counter.reclaim_expired(now=1000.0)  # 同一窗口内清理，必须是无操作

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    threads.append(threading.Thread(target=reclaimer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(outcomes)
    print(f"\n[reclaim] burst+reclaim -> allowed={allowed} rejected={threads_n - allowed}")
    assert allowed == limit


def test_reclaim_tenants_explicit_and_isolated():
    """显式清理已下线租户：只清指定租户，其他租户计数不受影响。"""
    counter = _counter()
    for _ in range(2):
        counter.try_acquire("tenant-dead", now=100.0)
        counter.try_acquire("tenant-a", now=100.0)
    reclaimed = counter.reclaim_tenants(["tenant-dead", "tenant-gone"])
    print(f"\n[reclaim] explicit -> reclaimed={reclaimed} tracked={counter.tracked_tenants()}")
    assert reclaimed == ["tenant-dead"]  # tenant-gone 本无状态
    assert counter.tracked_tenants() == ["tenant-a"]
    # tenant-a 的计数原样保留：上限 3，已用 2，再放行 1 次后即拒。
    assert counter.try_acquire("tenant-a", now=100.0).allowed
    assert not counter.try_acquire("tenant-a", now=100.0).allowed
