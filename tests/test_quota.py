"""配额计数：窗口语义可复现，并发突发不超放，租户严格隔离。"""

import logging
import threading

from quota_guard.quota import InMemoryQuotaStore

logger = logging.getLogger("quota_guard.test")


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_fixed_window_count_and_reject():
    clock = FakeClock()
    store = InMemoryQuotaStore(clock=clock)

    states = [store.hit("acme", limit=3, window_seconds=10) for _ in range(4)]
    logger.info("tenant=acme limit=3 window=10s states=%s",
                [(s.count, s.allowed) for s in states])

    assert [s.allowed for s in states] == [True, True, True, False]
    # 超限不增加计数
    assert states[3].count == 3
    # 重试提示指向窗口重置时刻
    assert abs(states[3].retry_after - 10.0) < 1e-9


def test_window_advance_and_reset():
    clock = FakeClock()
    store = InMemoryQuotaStore(clock=clock)

    store.hit("acme", limit=1, window_seconds=10)
    blocked = store.hit("acme", limit=1, window_seconds=10)
    assert not blocked.allowed

    clock.now += 10.0  # 窗口推进
    fresh = store.hit("acme", limit=1, window_seconds=10)
    logger.info("tenant=acme after window advance: count=%d window_start=%s",
                fresh.count, fresh.window_start)
    assert fresh.allowed and fresh.count == 1
    assert fresh.window_start == 1010.0


def test_tenant_isolation():
    clock = FakeClock()
    store = InMemoryQuotaStore(clock=clock)

    store.hit("a", limit=1, window_seconds=10)
    blocked_a = store.hit("a", limit=1, window_seconds=10)
    first_b = store.hit("b", limit=1, window_seconds=10)
    logger.info("tenant=a blocked=%s, tenant=b first allowed=%s",
                not blocked_a.allowed, first_b.allowed)

    assert not blocked_a.allowed
    assert first_b.allowed and first_b.count == 1


def test_concurrent_burst_never_exceeds_limit():
    """同一租户 300 并发抢 50 个配额：放行数必须恰好等于上限。"""
    store = InMemoryQuotaStore(clock=FakeClock())
    limit, total = 50, 300
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        state = store.hit("burst", limit=limit, window_seconds=60)
        with lock:
            results.append(state.allowed)

    threads = [threading.Thread(target=worker) for _ in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(results)
    logger.info("tenant=burst limit=%d concurrent=%d -> allowed=%d",
                limit, total, allowed)
    assert allowed == limit


def test_concurrent_multi_tenant_isolation():
    """多租户并发抢占：各租户放行数互不影响，各自恰好等于上限。"""
    store = InMemoryQuotaStore(clock=FakeClock())
    tenants = {"a": 10, "b": 20, "c": 30}
    results: dict[str, list[bool]] = {t: [] for t in tenants}
    lock = threading.Lock()

    def worker(tenant: str, limit: int):
        state = store.hit(tenant, limit=limit, window_seconds=60)
        with lock:
            results[tenant].append(state.allowed)

    threads = [
        threading.Thread(target=worker, args=(tenant, limit))
        for tenant, limit in tenants.items()
        for _ in range(limit * 4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for tenant, limit in tenants.items():
        allowed = sum(results[tenant])
        logger.info("tenant=%s limit=%d concurrent=%d -> allowed=%d",
                    tenant, limit, len(results[tenant]), allowed)
        assert allowed == limit
