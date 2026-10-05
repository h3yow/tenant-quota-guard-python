"""按租户生效的配额：不同租户不同上限与窗口，并发与隔离语义不变。"""

import threading

from app.config import QuotaSettings, TenantQuotaRule
from app.quota import QuotaCounter


def _settings() -> QuotaSettings:
    return QuotaSettings(
        default_limit=2,
        default_window_seconds=60.0,
        tenant_overrides={"tenant-vip": TenantQuotaRule(limit=5, window_seconds=10.0)},
    )


def test_override_limit_and_window_apply():
    counter = QuotaCounter(settings=_settings())
    # 默认租户：60 秒窗口内最多 2 次。
    default_results = [counter.try_acquire("tenant-a", now=1000.0).allowed for _ in range(3)]
    # 覆盖租户：10 秒窗口内最多 5 次。
    vip_results = [counter.try_acquire("tenant-vip", now=1000.0).allowed for _ in range(6)]
    print(f"\n[tenant-quota] tenant-a (default 2/60s) -> {default_results}")
    print(f"[tenant-quota] tenant-vip (override 5/10s) -> {vip_results}")
    assert default_results == [True, True, False]
    assert vip_results == [True, True, True, True, True, False]


def test_override_window_resets_on_its_own_alignment():
    counter = QuotaCounter(settings=_settings())
    # tenant-vip 窗口 10 秒：1005 在窗口 [1000,1010)，1010 进入下一窗口。
    for _ in range(5):
        assert counter.try_acquire("tenant-vip", now=1005.0).allowed
    blocked = counter.try_acquire("tenant-vip", now=1009.9)
    after = counter.try_acquire("tenant-vip", now=1010.0)
    print(f"\n[tenant-quota] vip before reset -> allowed={blocked.allowed} "
          f"retry_after={blocked.retry_after:.4f}")
    print(f"[tenant-quota] vip after reset  -> allowed={after.allowed} window_start={after.window_start}")
    assert not blocked.allowed
    assert after.allowed and after.window_start == 1010.0
    # 默认租户仍按 60 秒窗口：1010 与 1000 处于同一窗口 [960,1020)。
    assert counter.try_acquire("tenant-a", now=1000.0).allowed
    assert counter.try_acquire("tenant-a", now=1010.0).allowed
    blocked_a = counter.try_acquire("tenant-a", now=1010.0)
    print(f"[tenant-quota] tenant-a 3rd in 60s window -> allowed={blocked_a.allowed}")
    assert not blocked_a.allowed


def test_concurrent_burst_respects_per_tenant_limit():
    """覆盖租户并发突发不超自己的上限，且不影响默认租户计数。"""
    counter = QuotaCounter(settings=_settings())
    threads_n = 100
    outcomes: dict[str, list[bool]] = {"tenant-vip": [], "tenant-a": []}
    lock = threading.Lock()
    barrier = threading.Barrier(threads_n * 2)

    def worker(tenant: str):
        barrier.wait()
        decision = counter.try_acquire(tenant, now=2000.0)
        with lock:
            outcomes[tenant].append(decision.allowed)

    threads = [threading.Thread(target=worker, args=(t,))
               for t in ("tenant-vip", "tenant-a") for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    vip_allowed = sum(outcomes["tenant-vip"])
    a_allowed = sum(outcomes["tenant-a"])
    print(f"\n[tenant-quota] burst vip -> allowed={vip_allowed}/{threads_n} (limit=5)")
    print(f"[tenant-quota] burst tenant-a -> allowed={a_allowed}/{threads_n} (limit=2)")
    assert vip_allowed == 5
    assert a_allowed == 2
