"""按租户配置：不同租户不同上限与窗口，互不影响；各自窗口内并发不超放。"""

import threading

from app.config import TenantQuotaConfig
from app.quota import QuotaCounter


def _counter() -> QuotaCounter:
    return QuotaCounter(
        limit=2,
        window_seconds=60.0,
        tenant_overrides={
            "tenant-vip": TenantQuotaConfig(limit=4, window_seconds=10.0),
        },
    )


def test_override_tenant_uses_own_limit_and_window():
    counter = _counter()
    # tenant-vip：上限 4、窗口 10s；默认租户：上限 2、窗口 60s。
    vip = [counter.try_acquire("tenant-vip", now=100.0) for _ in range(5)]
    plain = [counter.try_acquire("tenant-b", now=100.0) for _ in range(3)]
    for i, r in enumerate(vip):
        print(f"\n[tenant-quota] tenant-vip req#{i} -> allowed={r.allowed} "
              f"remaining={r.remaining} retry_after={r.retry_after}")
    for i, r in enumerate(plain):
        print(f"[tenant-quota] tenant-b  req#{i} -> allowed={r.allowed} "
              f"remaining={r.remaining} retry_after={r.retry_after}")

    assert [r.allowed for r in vip] == [True, True, True, True, False]
    # vip 的窗口是 10s：100.0 落入 [100,110)，重试提示为距 110 的时间。
    assert vip[4].retry_after == 10.0
    assert [r.allowed for r in plain] == [True, True, False]
    # 默认租户窗口仍是 60s：[60,120)，重试提示为距 120 的时间。
    assert plain[2].retry_after == 20.0


def test_override_window_resets_independently():
    counter = _counter()
    assert counter.try_acquire("tenant-vip", now=100.0).allowed
    # 默认租户在 now=105 仍处于 [60,120) 窗口；vip 在 110 已进入新窗口。
    assert counter.try_acquire("tenant-b", now=105.0).allowed
    assert counter.try_acquire("tenant-b", now=105.0).allowed
    assert not counter.try_acquire("tenant-b", now=105.0).allowed  # 默认上限 2 已用完
    vip_new_window = counter.try_acquire("tenant-vip", now=110.0)
    print(f"\n[tenant-quota] vip@110 -> allowed={vip_new_window.allowed} "
          f"window_start={vip_new_window.window_start}")
    assert vip_new_window.allowed
    assert vip_new_window.window_start == 110.0  # 按 vip 自己的 10s 窗口对齐


def test_concurrent_burst_respects_per_tenant_limit():
    counter = _counter()
    outcomes: dict[str, list[bool]] = {"tenant-vip": [], "tenant-b": []}
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker(tenant: str):
        barrier.wait()
        decision = counter.try_acquire(tenant, now=1000.0)
        with lock:
            outcomes[tenant].append(decision.allowed)

    threads = [threading.Thread(target=worker, args=(t,))
               for t in ("tenant-vip", "tenant-b") for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    vip_allowed = sum(outcomes["tenant-vip"])
    plain_allowed = sum(outcomes["tenant-b"])
    print(f"\n[tenant-quota] burst vip allowed={vip_allowed}/20 (limit 4); "
          f"plain allowed={plain_allowed}/20 (limit 2)")
    assert vip_allowed == 4   # 各自上限互不干扰
    assert plain_allowed == 2
