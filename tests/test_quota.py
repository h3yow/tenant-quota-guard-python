"""配额计数：窗口语义确定、并发突发不超放、租户间互不影响。"""

import threading

from app.quota import QuotaCounter


def test_window_allows_up_to_limit_then_rejects():
    counter = QuotaCounter(limit=3, window_seconds=10.0)
    results = [counter.try_acquire("tenant-a", now=100.0) for _ in range(5)]
    for i, r in enumerate(results):
        print(f"\n[quota] tenant=tenant-a req#{i} now=100.0 -> allowed={r.allowed} "
              f"remaining={r.remaining} retry_after={r.retry_after}")
    assert [r.allowed for r in results] == [True, True, True, False, False]
    # 超限响应携带重试提示：距下一窗口的时间。
    assert results[3].retry_after == 10.0  # 110 - 100


def test_window_reset_is_deterministic():
    counter = QuotaCounter(limit=2, window_seconds=10.0)
    assert counter.try_acquire("tenant-a", now=101.0).allowed
    assert counter.try_acquire("tenant-a", now=109.9).allowed
    rejected = counter.try_acquire("tenant-a", now=109.9)
    print(f"\n[quota] before reset -> allowed={rejected.allowed} retry_after={rejected.retry_after}")
    assert not rejected.allowed
    # 窗口边界对齐：110 进入新窗口，计数重置。
    after = counter.try_acquire("tenant-a", now=110.0)
    print(f"[quota] after reset  -> allowed={after.allowed} window_start={after.window_start}")
    assert after.allowed and after.window_start == 110.0


def test_tenants_are_isolated():
    counter = QuotaCounter(limit=1, window_seconds=10.0)
    assert counter.try_acquire("tenant-a", now=0.0).allowed
    blocked_a = counter.try_acquire("tenant-a", now=0.0)
    ok_b = counter.try_acquire("tenant-b", now=0.0)
    print(f"\n[quota] tenant-a 2nd -> allowed={blocked_a.allowed}; "
          f"tenant-b 1st -> allowed={ok_b.allowed}")
    assert not blocked_a.allowed
    assert ok_b.allowed


def test_concurrent_burst_never_exceeds_limit():
    limit, threads = 50, 200
    counter = QuotaCounter(limit=limit, window_seconds=60.0)
    outcomes: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker():
        barrier.wait()
        decision = counter.try_acquire("tenant-a", now=1000.0)
        with lock:
            outcomes.append(decision.allowed)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    allowed = sum(outcomes)
    print(f"\n[quota] burst tenant=tenant-a limit={limit} threads={threads} "
          f"-> allowed={allowed} rejected={threads - allowed}")
    assert allowed == limit  # 并发突发下绝不超过上限
