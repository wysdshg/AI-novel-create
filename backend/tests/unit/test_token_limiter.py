# -*- coding: utf-8 -*-
"""token 感知限流回归（docs/08 B17，2026-09-16）。

背景：实测「并发 4 + 批量 10 章」打爆硅基流动 `429 TPM limit reached` ——
旧 `RateLimiter` 只控**请求发起间隔**，**完全不控 token 量**。
额度是**输入+输出合计**的每分钟上限（用户提供 5 万），且**贴边极易触发**（输出也计入）
→ 自设上限 4 万（80%）；并发下用"在途预估占额"防止多线程同时穿过判定。

测试用**假时钟**（不真 sleep），所以毫秒级跑完。
"""

import pytest

from app.services import plot_import as pi


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, float(s))


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(pi.time, "time", c.time)
    monkeypatch.setattr(pi.time, "sleep", c.sleep)
    return c


# ---------------- 估算 ----------------
def test_est_tokens_is_monotonic_and_proportional():
    assert pi.est_tokens(0) == 0
    assert pi.est_tokens(1000) > pi.est_tokens(100)
    assert pi.est_tokens(1000) == int(1000 * pi.TOKEN_PER_CHAR)


def test_chapter_est_tokens_uses_file_size(tmp_path):
    f = tmp_path / "0001_测试.txt"
    f.write_text("字" * 3000, encoding="utf-8")          # UTF-8 中文 3 字节/字
    est = pi.chapter_est_tokens(str(f))
    assert est > pi.OUT_TOKENS_PER_CHAPTER               # 含正文估算
    assert est == pi.est_tokens(int(f.stat().st_size / 3)) + pi.OUT_TOKENS_PER_CHAPTER


def test_chapter_est_tokens_missing_file_is_safe():
    assert pi.chapter_est_tokens("/no/such/file.txt") == pi.OUT_TOKENS_PER_CHAPTER


def test_per_request_cap_divides_by_concurrency_with_floor():
    assert pi.per_request_token_cap(1) == pi.TPM_BUDGET
    assert pi.per_request_token_cap(4) == int(pi.TPM_BUDGET / 4)
    assert pi.per_request_token_cap(100) == 3000          # 下限，避免批切太碎


def test_usage_total_variants():
    assert pi.usage_total({"total_tokens": 123}) == 123
    assert pi.usage_total({"prompt_tokens": 100, "completion_tokens": 20}) == 120
    assert pi.usage_total({"prompt_tokens": 5}) == 5
    assert pi.usage_total(None) is None
    assert pi.usage_total({}) is None


# ---------------- 限流器：预算窗口 ----------------
def test_no_budget_behaves_like_old_interval_only(clock):
    rl = pi.RateLimiter(min_interval=2.0)
    assert rl.budget is None
    rl.acquire(10 ** 9)            # 无预算 → 不该等
    assert clock.t == 1000.0
    assert rl.used_last_window() == 0


def test_budget_blocks_when_window_is_full(clock):
    rl = pi.RateLimiter(min_interval=0.0, tpm_budget=1000, window=60.0)
    assert rl.acquire(800) == 800                  # 占 800
    rl.record(800, 800)                            # 真实用量 800
    assert rl.used_last_window() == 800
    rl.acquire(100)                                # 800+100 ≤ 1000 → 立刻通过
    rl.record(100, 100)
    assert rl.used_last_window() == 900
    # 再一次 200 会超预算 → 必须等到最早那笔过期（60s）
    rl.acquire(200)
    assert clock.t >= 1000.0 + 60.0
    rl.record(200, 200)
    assert rl.used_last_window() <= 1000


def test_inflight_estimates_prevent_concurrent_burst():
    """并发场景：两笔在途各占 400，第三笔必须先等"在途完成放额"，不能瞬间越过总预算。

    用**真线程**模拟另一笔请求完成（它是唯一能放额的事件）—— 这条同时回归了
    "只被在途占额时不许按 60s 窗口等"这个死锁 bug。
    """
    import threading
    import time as _t

    rl = pi.RateLimiter(min_interval=0.0, tpm_budget=1000, window=60.0)
    leased_a = rl.acquire(700)          # 线程 A 在途占 700
    assert rl.used_last_window() == 700  # 全部是"在途"，spent 还是 0

    def release_one():
        _t.sleep(0.3)
        rl.record(leased_a, 200)         # A 完成：真实只用 200，归还 700 占额

    th = threading.Thread(target=release_one)
    th.start()
    t0 = _t.time()
    rl.acquire(700)                      # 700+700 > 1000 → 必须等 A 放额（而不是等 60s 窗口）
    elapsed = _t.time() - t0
    th.join()
    assert 0.2 <= elapsed < 5.0, f"应在等放额而非死等：{elapsed:.2f}s"
    assert rl.used_last_window() <= 1000


def test_single_request_over_budget_does_not_hang():
    """单次请求预估就超预算：不可能靠等待解决 → 放行（否则死等），但要告警。"""
    import time as _t

    rl = pi.RateLimiter(min_interval=0.0, tpm_budget=1000, window=60.0)
    t0 = _t.time()
    rl.acquire(99999)
    assert _t.time() - t0 < 1.0


def test_record_releases_lease_and_uses_actual(clock):
    rl = pi.RateLimiter(min_interval=0.0, tpm_budget=10000, window=60.0)
    leased = rl.acquire(5000)
    assert rl.used_last_window() == 5000           # 在途按预估计
    rl.record(leased, 1200)                        # 真实只用了 1200
    assert rl.used_last_window() == 1200           # 占额已归还、按真实值记


def test_failed_request_releases_lease(clock):
    rl = pi.RateLimiter(min_interval=0.0, tpm_budget=10000, window=60.0)
    leased = rl.acquire(3000)
    rl.record(leased, 0)                           # 失败：归还占额、不消耗额度
    assert rl.used_last_window() == 0


def test_budget_window_expires(clock):
    rl = pi.RateLimiter(min_interval=0.0, tpm_budget=1000, window=60.0)
    rl.acquire(900)
    rl.record(900, 900)
    clock.t += 61.0                                # 窗口滑过
    assert rl.used_last_window() == 0


# ---------------- 动态打包（超预算就减章） ----------------
def _chs(tmp_path, sizes):
    out = []
    for i, n in enumerate(sizes, start=1):
        p = tmp_path / f"{i:04d}_c.txt"
        p.write_text("字" * n, encoding="utf-8")
        out.append({"no": i, "path": str(p)})
    return out


def test_pack_respects_item_cap(tmp_path):
    chs = _chs(tmp_path, [100] * 10)               # 都很小
    packs = pi._pack_batches_by_tokens(chs, max_items=3, max_tokens=10 ** 6)
    assert [len(p) for p in packs] == [3, 3, 3, 1]


def test_pack_shrinks_batch_when_tokens_exceed_budget(tmp_path):
    """核心需求：估算超预算就**少放一章**（而不是硬塞 → 撞 TPM 限流）。"""
    chs = _chs(tmp_path, [3000, 3000, 3000, 3000])   # 每章 ≈ est(3000 字)+200 = 2300
    per = pi.chapter_est_tokens(chs[0]["path"])
    packs = pi._pack_batches_by_tokens(chs, max_items=10, max_tokens=per * 2 + 10)
    assert [len(p) for p in packs] == [2, 2]          # 章数上限 10 没用上，预算是硬约束


def test_pack_oversized_chapter_goes_alone(tmp_path):
    """爆更章（单章就超预算）独占一批，不丢内容。

    （2026-09-17 更新：chapter_est 现在按「截到 6000 字」估——文件再大 est 也 ≤4400，
    所以构造超限要用更小的 max_tokens，而不是更大的文件。）
    """
    chs = _chs(tmp_path, [200, 60000, 200])
    packs = pi._pack_batches_by_tokens(chs, max_items=10, max_tokens=4000)
    assert any(len(p) == 1 and p[0]["no"] == 2 for p in packs)
    assert sum(len(p) for p in packs) == 3            # 一章都不能丢


def test_pack_preserves_order_and_all_chapters(tmp_path):
    chs = _chs(tmp_path, [500, 500, 500, 500, 500])
    packs = pi._pack_batches_by_tokens(chs, max_items=2, max_tokens=10 ** 6)
    flat = [c["no"] for p in packs for c in p]
    assert flat == [1, 2, 3, 4, 5]
