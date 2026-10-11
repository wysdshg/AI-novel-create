# -*- coding: utf-8 -*-
"""F7A 修复单测：B20 补跑漏闸（A）+ 流式限流占额泄漏（C）。

两处管线 bug（2026-10-08 我是掌门跑批实测停闸后定位，PM 裁决 A+C 同批修）：

A. `import_chapters_batch` 的**漏章补跑路径**写库时不调 `_dup_summary_chapters`
   -> 主批检测到的复制退化，补跑时若再次退化（模型行为，非抖动）就直接入库。
   修法：补跑两处写库前各挂一次闸；命中 -> 允许**一次**补跑重算；再退化 ->
   记台账（`outputs/f7a/退化未解决.json`）且**不写库**（不无限重试）。

C. `_chat_post_stream`（网关路）只 `rate.acquire()` 占额、**从不 `rate.record()` 归还**
   -> `RateLimiter._inflight` 单调累积，触顶后 `acquire()` 的 while True 永久阻塞
   （静默死锁：不抛异常、不超时、进程存活、零吞吐）。
   修法：三条出口（成功 / HTTPError / 其它异常）各补 `rate.record()`，
   语义对齐非流式 `_chat_post`（L403 真实 usage、L407/L429 失败归 0）。

对照写法仿 `outputs/f7a/_repro_leak.py`（用假 HTTP，不打真实网关）。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app.services import plot_import as pi


# ──────────────────────────── 公共夹具 ────────────────────────────
def _mk_book(tmp_path, chapters):
    d = tmp_path / "book"
    d.mkdir(exist_ok=True)
    for no, title in chapters.items():
        (d / f"{no:04d}_第{no}章 {title}.txt").write_text(
            f"第{no}章 {title}\n\n正文甲乙丙。\n", encoding="utf-8")
    return str(d)


_SCENES = [
    "李少阳在望雪山击退外门执事郭一森",
    "兰玲儿在玲儿峰宣布要庇护李少阳",
    "石云与羊正一为风灵剑在大殿争执",
    "黄一山夜访送来鲜雪霜所制的诛妖索",
    "赵诗诗在坊市收购三品聚灵草种子",
    "紫衣提醒李少阳寿元已被抽取过半",
]


def _sum(tag) -> str:
    """按 tag 取一段**彼此不相似**的正常概括（长度过 `_valid_summary` 15 字下限）。

    刻意让各章文本差异足够大：`_dup_pairs` 的近似判定（ratio ≥ 0.85）本就会把
    「只差一个数字」的模板文本判成复制 —— 那是判据的正确行为，不是 bug，
    测试数据不该踩这条线。
    """
    if isinstance(tag, int):
        return f"{_SCENES[tag % len(_SCENES)]}，这一段的经过与结果都记在此处备查。"
    return f"{tag}：这是同一段退化文本，模型对每章都返回了它，内容完全一致无差异。"


class _FakeResp(io.BytesIO):
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_http(monkeypatch, *, body: dict | None = None,
                       raise_exc: Exception | None = None):
    """桩掉 urllib opener：不打真实网关，直接返回可控响应或抛异常。"""
    payload = json.dumps(body or {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 42},
    }).encode()

    def fake_open(req, timeout=None):
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp(payload)

    monkeypatch.setattr(pi.urllib.request, "build_opener",
                        lambda *a, **k: type("O", (), {"open": staticmethod(fake_open)})())


# ═══════════════════════════ C：流式占额归还 ═══════════════════════════
class TestStreamReturnsRateQuota:
    """C：`_chat_post_stream` 三条出口必须 `rate.record()`，否则 inflight 泄漏 -> 静默死锁。"""

    def test_success_returns_leased_quota(self, monkeypatch):
        """成功出口：归还占额，且用厂商回传的真实 usage 纠正。"""
        _install_fake_http(monkeypatch)
        monkeypatch.setattr(pi, "_stream_text",
                            lambda url, key, body, timeout=300: ("ok", {"total_tokens": 42}))
        rate = pi.RateLimiter(0.0, tpm_budget=50_000)
        pi._chat_post_stream("http://x/v1/chat/completions", "k",
                             {"messages": [{"role": "user", "content": "字" * 500}],
                              "max_tokens": 50},
                             rate=rate, attempts=1)
        # inflight 必须归零（否则额度只减不还，几批后永久阻塞）
        assert rate._inflight == 0, f"成功出口未归还占额，inflight={rate._inflight}"
        assert rate.used_last_window() == 42, "应按真实 usage(42) 记账"

    def test_runtime_error_returns_zero(self, monkeypatch):
        """失败出口：归还占额但计 0（对齐非流式 L429 `rate.record(leased, 0)`）。"""
        def boom(url, key, body, timeout=300):
            raise RuntimeError("流断了")
        monkeypatch.setattr(pi, "_stream_text", boom)
        rate = pi.RateLimiter(0.0, tpm_budget=50_000)
        with pytest.raises(RuntimeError):
            pi._chat_post_stream("http://x/v1/chat/completions", "k",
                                 {"messages": [{"role": "user", "content": "字" * 500}],
                                  "max_tokens": 50},
                                 rate=rate, attempts=1)
        assert rate._inflight == 0, "失败出口未归还占额，inflight 会永久泄漏"

    def test_no_leak_across_many_calls(self, monkeypatch):
        """回归防线：连打 N 次后额度不累积 —— 修复前 N×est 单调增长并触顶死锁。"""
        _install_fake_http(monkeypatch)
        monkeypatch.setattr(pi, "_stream_text",
                            lambda url, key, body, timeout=300: ("ok", {"total_tokens": 42}))
        rate = pi.RateLimiter(0.0, tpm_budget=50_000)
        for _ in range(12):
            pi._chat_post_stream("http://x/v1/chat/completions", "k",
                                 {"messages": [{"role": "user", "content": "字" * 500}],
                                  "max_tokens": 50},
                                 rate=rate, attempts=1)
        assert rate._inflight == 0
        # 12 次真实用量各 42 -> 窗口内应为 12×42，且远小于 12×est（约 12×3550）
        assert rate.used_last_window() == 12 * 42

    def test_attempts_exhausted_still_returns(self, monkeypatch):
        """重试用尽后抛错（不静默返回空串），但占额必须已归还。"""
        def boom(url, key, body, timeout=300):
            raise RuntimeError("一直失败")
        monkeypatch.setattr(pi, "_stream_text", boom)
        monkeypatch.setattr(pi.time, "sleep", lambda s: None)   # 别真退避
        rate = pi.RateLimiter(0.0, tpm_budget=50_000)
        with pytest.raises(RuntimeError):
            pi._chat_post_stream("http://x/v1/chat/completions", "k",
                                 {"messages": [{"role": "user", "content": "字" * 500}],
                                  "max_tokens": 50},
                                 rate=rate, attempts=3)
        assert rate._inflight == 0, "重试用尽后占额泄漏"

    def test_non_stream_unchanged(self, monkeypatch):
        """对照组：非流式路保持原语义（成功按真实 usage），本次不改它。"""
        _install_fake_http(monkeypatch)
        rate = pi.RateLimiter(0.0, tpm_budget=50_000)
        pi._chat_post("http://x/v1/chat/completions", "k",
                      {"messages": [{"role": "user", "content": "字" * 500}],
                       "max_tokens": 50},
                      timeout=30, rate=rate)
        assert rate._inflight == 0
        assert rate.used_last_window() == 42


# ═══════════════════════════ A：B20 补跑挂闸 ═══════════════════════════
class TestB20GateOnRetryPath:
    """A：补跑路径两处写库前必须挂复制退化闸；再退化 -> 记台账且不写库。"""

    def test_retry_degenerate_not_persisted(self, test_db, tmp_path, monkeypatch):
        """主批退化 -> 补跑仍退化 -> **不写库**（修复前会写进去）。"""
        book = _mk_book(tmp_path, {1: "甲", 2: "乙", 3: "丙"})
        monkeypatch.setattr(pi, "sf_key", lambda db: "k")
        monkeypatch.setattr(pi, "_fetch_gw_sf_key_count", lambda db: 1)

        calls = {"n": 0}

        def fake_post(key, content, **kw):
            """无论主批还是补跑，都返回同一段逐字相同的退化文本。"""
            calls["n"] += 1
            import re
            nos = [int(x) for x in re.findall(r"【第(\d+)章", content)]
            return json.dumps({"chapters": [{"no": n, "summary": _sum("退化")} for n in nos]},
                              ensure_ascii=False)

        monkeypatch.setattr(pi, "_sf_post", fake_post)
        monkeypatch.setattr(pi, "_record_unresolved_degenerate", lambda *a, **k: None)

        st = pi.import_chapters_batch(test_db, book, "书", batch_size=3, concurrency=1)

        assert calls["n"] >= 2, "应至少跑过一次补跑"
        rows = test_db.query(pi.ChapterSummaryORM).order_by(
            pi.ChapterSummaryORM.chapter_no).all()
        # 主批路径的既有语义（test_summary_dedup 固化）：退化串里保留**首个**，
        # 它可能真是该章内容 —— 本次不改主批，只补补跑闸。
        # 补跑拿到的是 3 章一簇全退化 -> 必须**整簇拒收**（修复前 ch2/ch3 会带着
        # 同一段退化文本入库，正是本次事故形态）。
        assert [r.chapter_no for r in rows] == [1], \
            f"补跑整簇退化必须全拒收，只应剩主批保留的首章，实际={[r.chapter_no for r in rows]}"
        assert rows[0].summary == _sum("退化")

    def test_retry_degenerate_goes_to_ledger(self, test_db, tmp_path, monkeypatch):
        """再退化要进台账 `outputs/f7a/退化未解决.json`（PM 跑完统一裁决）。"""
        book = _mk_book(tmp_path, {1: "甲", 2: "乙", 3: "丙"})
        monkeypatch.setattr(pi, "sf_key", lambda db: "k")
        monkeypatch.setattr(pi, "_fetch_gw_sf_key_count", lambda db: 1)
        import re

        def fake_post(key, content, **kw):
            nos = [int(x) for x in re.findall(r"【第(\d+)章", content)]
            return json.dumps({"chapters": [{"no": n, "summary": _sum("退化")} for n in nos]},
                              ensure_ascii=False)

        monkeypatch.setattr(pi, "_sf_post", fake_post)

        seen: list[dict] = []
        monkeypatch.setattr(pi, "_record_unresolved_degenerate",
                            lambda book_name, rows: seen.extend(rows))

        pi.import_chapters_batch(test_db, book, "书", batch_size=3, concurrency=1)

        assert seen, "再退化的章必须记台账"
        assert {r["chapter_no"] for r in seen} == {2, 3}, \
            f"退��串里主批保留首章（本次不动主批语义），补跑整簇进台账；实际={sorted(r['chapter_no'] for r in seen)}"
        assert all("summary" in r for r in seen), "台账要留退化文本供 PM 判读"
        assert all(r["summary"] == _sum("退化") for r in seen)

    def test_retry_recovers_persists(self, test_db, tmp_path, monkeypatch):
        """主批退化但补跑拿到正常文本 -> 正常入库（闸不能误伤真修复）。"""
        book = _mk_book(tmp_path, {1: "甲", 2: "乙", 3: "丙"})
        monkeypatch.setattr(pi, "sf_key", lambda db: "k")
        monkeypatch.setattr(pi, "_fetch_gw_sf_key_count", lambda db: 1)
        import re

        state = {"n": 0}

        def fake_post(key, content, **kw):
            state["n"] += 1
            nos = [int(x) for x in re.findall(r"【第(\d+)章", content)]
            if state["n"] == 1:                      # 主批：全部退化
                summ = {n: _sum("退化") for n in nos}
            else:                                    # 补跑：每章不同 -> 正常
                summ = {n: _sum(n) for n in nos}
            return json.dumps({"chapters": [{"no": n, "summary": s} for n, s in summ.items()]},
                              ensure_ascii=False)

        monkeypatch.setattr(pi, "_sf_post", fake_post)
        monkeypatch.setattr(pi, "_record_unresolved_degenerate", lambda *a, **k: None)

        st = pi.import_chapters_batch(test_db, book, "书", batch_size=3, concurrency=1)

        assert st["done"] == 3, "补跑恢复正常 -> 三章全部入库（闸不能误伤真修复）"
        rows = test_db.query(pi.ChapterSummaryORM).order_by(
            pi.ChapterSummaryORM.chapter_no).all()
        assert [r.chapter_no for r in rows] == [1, 2, 3]
        assert len({r.summary for r in rows}) == 3, "三章概括应互不相同"

    def test_single_chapter_fallback_degenerate_not_persisted(
            self, test_db, tmp_path, monkeypatch):
        """单章兜底路径也要挂闸：兜底两两退化时不得写库（修复前会写）。"""
        book = _mk_book(tmp_path, {1: "甲", 2: "乙", 3: "丙"})
        monkeypatch.setattr(pi, "sf_key", lambda db: "k")
        monkeypatch.setattr(pi, "_fetch_gw_sf_key_count", lambda db: 1)

        def fake_post(key, content, **kw):
            # 批量 prompt -> 返回不可解析（逼它走单章兜底）；单章 -> 退化文本
            if "【第" in content:
                return "这不是 JSON"
            return _sum("退化")

        monkeypatch.setattr(pi, "_sf_post", fake_post)
        seen: list[dict] = []
        monkeypatch.setattr(pi, "_record_unresolved_degenerate",
                            lambda book_name, rows: seen.extend(rows))

        pi.import_chapters_batch(test_db, book, "书", batch_size=3, concurrency=1)

        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert len(rows) == 0, \
            f"单章兜底整簇退化也不得写库，实际入库 {[(r.chapter_no, r.summary) for r in rows]}"
        assert seen and {r["chapter_no"] for r in seen} == {1, 2, 3}

    def test_ledger_written_to_disk(self, tmp_path, monkeypatch):
        """台账落盘形态：JSON、含 book/章号/退化文本/原因/时间。"""
        target = tmp_path / "退化未解决.json"
        monkeypatch.setattr(pi, "DEGENERATE_LEDGER_PATH", str(target))
        pi._record_unresolved_degenerate("我是掌门", [
            {"chapter_no": 149, "title": "第150章",
             "summary": "退化文本内容在此", "reason": "dup"},
        ])
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["book"] == "我是掌门"
        assert data["entries"][0]["chapter_no"] == 149
        assert data["entries"][0]["reason"] == "dup"

    def test_ledger_appends_not_overwrites(self, tmp_path, monkeypatch):
        """台账是追加语义（跨 chunk / 跨次跑批累积），不覆盖旧条目。"""
        target = tmp_path / "退化未解决.json"
        monkeypatch.setattr(pi, "DEGENERATE_LEDGER_PATH", str(target))
        pi._record_unresolved_degenerate("书", [
            {"chapter_no": 1, "title": "t1", "summary": "s1", "reason": "dup"}])
        pi._record_unresolved_degenerate("书", [
            {"chapter_no": 2, "title": "t2", "summary": "s2", "reason": "dup"}])
        data = json.loads(target.read_text(encoding="utf-8"))
        assert [e["chapter_no"] for e in data["entries"]] == [1, 2]

    def test_ledger_path_is_repo_root(self):
        """台账必须落在**仓库根** outputs/f7a/（PM 按约定路径取）。

        踩过的坑：本文件在 backend/app/services/，上溯 2 级会落到 backend/outputs/f7a/ ——
        台账确实写了，但 PM 去 outputs/f7a/ 找会扑空。
        """
        p = Path(pi.DEGENERATE_LEDGER_PATH)
        assert p.name == "退化未解决.json"
        assert p.parent.name == "f7a"
        assert p.parent.parent.name == "outputs"
        # 上溯到 backend 的上一级才是仓库根
        assert p.parent.parent.parent.name != "backend", \
            f"台账落在 {p} —— 上溯层级错，PM 按 outputs/f7a/ 约定路径取不到"
        assert (p.parent.parent.parent / "backend").is_dir()

    def test_ledger_write_failure_does_not_break_batch(
            self, test_db, tmp_path, monkeypatch):
        """台账写失败（磁盘满/路径不存在）不许让跑批崩 —— 台账是旁路。"""
        book = _mk_book(tmp_path, {1: "甲", 2: "乙", 3: "丙"})
        monkeypatch.setattr(pi, "sf_key", lambda db: "k")
        monkeypatch.setattr(pi, "_fetch_gw_sf_key_count", lambda db: 1)
        monkeypatch.setattr(pi, "DEGENERATE_LEDGER_PATH", "/nonexistent-dir/x/y.json")
        import re

        def fake_post(key, content, **kw):
            nos = [int(x) for x in re.findall(r"【第(\d+)章", content)]
            return json.dumps({"chapters": [{"no": n, "summary": _sum("退化")} for n in nos]},
                              ensure_ascii=False)

        monkeypatch.setattr(pi, "_sf_post", fake_post)

        st = pi.import_chapters_batch(test_db, book, "书", batch_size=3, concurrency=1)
        # 主批保留退化串首章（既有语义，本次不动主批），补跑整簇拒收
        assert st["done"] == 1, "台账写失败也不该让跑批崩，且退化簇仍拒收"
        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert [r.chapter_no for r in rows] == [1]