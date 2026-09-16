"""Phase 7.1 导入管线：切章 / 概括入库 / 段切分 / 分类（全部 mock LLM，不出网）。

覆盖重点：
- **幂等断点续跑**：已概括的章重跑自动跳过；失败章跳过不阻断，重跑自动补；
- **全覆盖兜底**：LLM 段切分漏标章节 → 归入最后一段；JSON 解析失败 → 整批回退单段；
- **写回正确性**：segment_no / segment_summary / plot_label 落到对应章节行。
"""
import json

from app.services import plot_import as pi


def _mk_book(tmp_path, chapters: dict[int, str]) -> str:
    book = tmp_path / "testbook"
    book.mkdir(exist_ok=True)
    for no, text in chapters.items():
        (book / f"{no:04d}_第{no}章测试.txt").write_text(text, encoding="utf-8")
    return str(book)


def _fake_sum(n: int) -> str:
    """够长的假概括（真实概括 80~120 字，管线有 ≥15 字的有效性校验）。"""
    return f"第{n}章概括：主角在测试场景中完成了一件重要事件，过程与结果均符合预期。"


class TestDiscover:
    def test_parse_names_sorted(self, tmp_path):
        book = _mk_book(tmp_path, {12: "乙", 1: "甲", 3: "丙"})
        chs = pi.discover_chapters(book)
        assert [c["no"] for c in chs] == [1, 3, 12]
        assert chs[0]["title"] == "第1章测试"

    def test_empty_dir_raises_on_import(self, test_db, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        try:
            pi.import_chapters(test_db, str(empty), "书")
            raised = False
        except RuntimeError:
            raised = True
        assert raised, "空目录应报错而不是静默成功"


class TestImportChapters:
    def test_import_and_idempotent(self, test_db, tmp_path, monkeypatch):
        book = _mk_book(tmp_path, {1: "第一章正文甲甲甲", 2: "第二章正文乙乙乙"})
        outs = iter(["概括一", "概括二"])
        monkeypatch.setattr(pi, "sf_chat", lambda db, c, **kw: next(outs))
        st = pi.import_chapters(test_db, book, "测试书")
        assert st["done"] == 2 and st["skipped"] == 0 and st["failed"] == 0
        rows = test_db.query(pi.ChapterSummaryORM).order_by(
            pi.ChapterSummaryORM.chapter_no).all()
        assert rows[0].summary == "概括一" and rows[1].summary == "概括二"
        assert rows[0].title == "第1章测试"

        st2 = pi.import_chapters(test_db, book, "测试书")   # 幂等重跑
        assert st2["skipped"] == 2 and st2["done"] == 0

    def test_failed_chapter_skips_and_backfills(self, test_db, tmp_path, monkeypatch):
        book = _mk_book(tmp_path, {1: "甲甲甲", 2: "乙乙乙（第二章）", 3: "丙丙丙"})
        state = {"n": 0}

        def fake(db, c, **kw):
            state["n"] += 1
            if state["n"] == 2:      # 第二次调用（第 2 章）失败
                raise RuntimeError("boom")
            return "概括"
        monkeypatch.setattr(pi, "sf_chat", fake)
        st = pi.import_chapters(test_db, book, "书")
        assert st["done"] == 2 and st["failed"] == 1
        st2 = pi.import_chapters(test_db, book, "书")   # 重跑补失败章
        assert st2["done"] == 1 and st2["skipped"] == 2
        assert test_db.query(pi.ChapterSummaryORM).count() == 3

    def test_range_filter(self, test_db, tmp_path, monkeypatch):
        book = _mk_book(tmp_path, {i: f"正文{i}" for i in (1, 2, 3, 4)})
        monkeypatch.setattr(pi, "sf_chat", lambda db, c, **kw: "概")
        st = pi.import_chapters(test_db, book, "书", start=2, end=3)
        assert st["done"] == 2
        nos = [r.chapter_no for r in test_db.query(pi.ChapterSummaryORM).all()]
        assert sorted(nos) == [2, 3]


class TestLabel:
    def test_label_writeback(self, test_db, monkeypatch):
        test_db.add(pi.ChapterSummaryORM(id="a", book_name="书", chapter_no=1,
                                         summary="概", segment_no=1, segment_summary="段概"))
        test_db.add(pi.ChapterSummaryORM(id="b", book_name="书", chapter_no=2,
                                         summary="概", segment_no=1, segment_summary="段概"))
        test_db.commit()
        monkeypatch.setattr(pi, "sf_chat", lambda db, c, **kw: "学院大比")
        r = pi.label_segments(test_db, "书")
        assert r["labeled"] == 1 and r["segments"] == 1
        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert all(r.plot_label == "学院大比" for r in rows)

    def test_label_failure_skips(self, test_db, monkeypatch):
        test_db.add(pi.ChapterSummaryORM(id="a", book_name="书", chapter_no=1,
                                         summary="概", segment_no=1, segment_summary="段概"))
        test_db.commit()

        def fake(db, c, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(pi, "sf_chat", fake)
        r = pi.label_segments(test_db, "书")
        assert r["labeled"] == 0    # 失败跳过，不影响其他段


class TestExportReport:
    def test_report_written(self, test_db, tmp_path):
        test_db.add(pi.ChapterSummaryORM(id="a", book_name="书", chapter_no=1,
                                         title="第一章", summary="概一",
                                         segment_no=1, segment_summary="段概",
                                         plot_label="学院大比"))
        test_db.add(pi.ChapterSummaryORM(id="b", book_name="书", chapter_no=2,
                                         title="第二章", summary="概二",
                                         segment_no=1, segment_summary="段概",
                                         plot_label="学院大比"))
        test_db.commit()
        out = tmp_path / "report.md"
        path = pi.export_report(test_db, "书", str(out))
        text = out.read_text(encoding="utf-8")
        assert path == str(out)
        assert "段 1 · 学院大比（第 1、2 章）" in text
        assert "概一" in text and "概二" in text
        assert "第一章" in text

    def test_report_no_segment(self, test_db, tmp_path):
        """没跑段切分时也要能导出（显示「未分段」，不炸）。"""
        test_db.add(pi.ChapterSummaryORM(id="a", book_name="书", chapter_no=1, summary="概"))
        test_db.commit()
        path = pi.export_report(test_db, "书", str(tmp_path / "r.md"))
        text = (tmp_path / "r.md").read_text(encoding="utf-8")
        assert path.endswith("r.md")
        assert "概" in text and "未分段" in text


class TestBatchSummarize:
    """批量概括（一次 N 章）+ 并发路径。

    重点：JSON 解析容错、模型漏章计入 failed（重跑可补）、幂等、
    并发下 DB 写入仍在主线程（session 不跨线程）。
    """

    def _mk(self, tmp_path, nos):
        return _mk_book(tmp_path, {n: f"第{n}章正文甲乙丙" for n in nos})

    def _mock(self, monkeypatch, payload):
        monkeypatch.setattr(pi, "sf_key", lambda db: "test-key")

        if callable(payload):
            monkeypatch.setattr(pi, "_sf_post", payload)
        else:
            monkeypatch.setattr(pi, "_sf_post", lambda k, c, **kw: payload)

    def test_batch_ok(self, test_db, tmp_path, monkeypatch):
        book = self._mk(tmp_path, [1, 2, 3])
        self._mock(monkeypatch, json.dumps({"chapters": [
            {"no": 1, "summary": _fake_sum(1)},
            {"no": 2, "summary": _fake_sum(2)},
            {"no": 3, "summary": _fake_sum(3)},
        ]}, ensure_ascii=False))
        st = pi.import_chapters_batch(test_db, book, "书", batch_size=3)
        assert st["done"] == 3 and st["failed"] == 0 and st["batches"] == 1
        rows = test_db.query(pi.ChapterSummaryORM).order_by(
            pi.ChapterSummaryORM.chapter_no).all()
        assert [r.summary for r in rows] == [_fake_sum(1), _fake_sum(2), _fake_sum(3)]

    def test_missing_chapter_counted_failed(self, test_db, tmp_path, monkeypatch):
        """模型只回 2 章 → 漏的那章计 failed（重跑会补），不入库空概括。"""
        book = self._mk(tmp_path, [1, 2, 3])
        self._mock(monkeypatch, json.dumps({"chapters": [
            {"no": 1, "summary": _fake_sum(1)}, {"no": 2, "summary": _fake_sum(2)},
        ]}, ensure_ascii=False))
        st = pi.import_chapters_batch(test_db, book, "书", batch_size=3)
        assert st["done"] == 2 and st["failed"] == 1
        assert test_db.query(pi.ChapterSummaryORM).count() == 2

    def test_bad_json_all_failed_but_no_crash(self, test_db, tmp_path, monkeypatch):
        book = self._mk(tmp_path, [1, 2])
        self._mock(monkeypatch, "这不是 JSON")
        st = pi.import_chapters_batch(test_db, book, "书", batch_size=2)
        assert st["done"] == 0 and st["failed"] == 2
        assert test_db.query(pi.ChapterSummaryORM).count() == 0

    def test_idempotent_skip(self, test_db, tmp_path, monkeypatch):
        book = self._mk(tmp_path, [1, 2, 3])
        test_db.add(pi.ChapterSummaryORM(id="x", book_name="书", chapter_no=1, summary="已有"))
        test_db.commit()
        self._mock(monkeypatch, json.dumps({"chapters": [
            {"no": 2, "summary": _fake_sum(2)}, {"no": 3, "summary": _fake_sum(3)},
        ]}, ensure_ascii=False))
        st = pi.import_chapters_batch(test_db, book, "书", batch_size=3)
        assert st["skipped"] == 1 and st["done"] == 2

    def test_concurrent_path_writes_all(self, test_db, tmp_path, monkeypatch):
        """并发路径：多批并行调用，写库仍在主线程 → 章节数正确。"""
        book = self._mk(tmp_path, [1, 2, 3, 4, 5, 6])

        def fake_post(key, content, **kw):
            nos = [int(x) for x in __import__("re").findall(r"【第(\d+)章", content)]
            return json.dumps({"chapters": [{"no": n, "summary": _fake_sum(n)} for n in nos]},
                              ensure_ascii=False)
        self._mock(monkeypatch, fake_post)
        st = pi.import_chapters_batch(test_db, book, "书", batch_size=2, concurrency=3)
        assert st["done"] == 6 and st["failed"] == 0
        assert test_db.query(pi.ChapterSummaryORM).count() == 6

    def test_no_key_raises(self, test_db, tmp_path, monkeypatch):
        book = self._mk(tmp_path, [1])
        monkeypatch.setattr(pi, "sf_key", lambda db: None)
        try:
            pi.import_chapters_batch(test_db, book, "书")
            raised = False
        except RuntimeError as e:
            raised = "Key" in str(e)
        assert raised, "无 Key 应明确报错"


class TestMergeArcs:
    """故事弧归并（DeepSeek 升档）：JSON 解析、全覆盖兜底、幂等清旧、无段数据早退。"""

    def _seed(self, test_db, seg_map: dict[int, list[int]]):
        for seg_no, chapters in seg_map.items():
            for ch in chapters:
                test_db.add(pi.ChapterSummaryORM(
                    id=f"c{ch}", book_name="书", chapter_no=ch, summary=f"第{ch}章概括",
                    segment_no=seg_no, segment_summary=f"段{seg_no}概括", plot_label="测标签"))
        test_db.commit()

    def _mock_ds(self, monkeypatch, payload):
        monkeypatch.setattr(pi, "ds_key", lambda db: "ds-key")
        monkeypatch.setattr(pi, "_ds_post", lambda k, c, **kw: payload)

    def test_merge_ok(self, test_db, monkeypatch):
        self._seed(test_db, {1: [1, 2], 2: [3, 4], 3: [5, 6]})
        self._mock_ds(monkeypatch, json.dumps({"arcs": [
            {"segments": [1, 2], "name": "觉醒", "summary": "弧一概括"},
            {"segments": [3], "name": "试炼", "summary": "弧二概括"},
        ]}, ensure_ascii=False))
        r = pi.merge_arcs(test_db, "书")
        assert r["arcs"] == 2 and r["segments"] == 3 and r["leftover"] == 0
        rows = test_db.query(pi.ChapterSummaryORM).order_by(
            pi.ChapterSummaryORM.chapter_no).all()
        assert rows[0].arc_no == 1 and rows[0].arc_name == "觉醒"
        assert rows[0].arc_summary == "弧一概括"
        assert rows[2].arc_no == 1          # 段2 与段1 同属弧1
        assert rows[4].arc_no == 2 and rows[4].arc_name == "试炼"

    def test_uncovered_segments_fallback(self, test_db, monkeypatch):
        """模型只归并部分段 → 剩余段兜底归入最后一个弧（保证全覆盖、不丢数据）。"""
        self._seed(test_db, {1: [1], 2: [2], 3: [3]})
        self._mock_ds(monkeypatch, json.dumps({"arcs": [
            {"segments": [1], "name": "只归一个", "summary": "s"}]}, ensure_ascii=False))
        r = pi.merge_arcs(test_db, "书")
        assert r["leftover"] == 2
        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert all(x.arc_no is not None for x in rows)   # 全覆盖

    def test_no_segment_data_early_return(self, test_db, monkeypatch):
        test_db.add(pi.ChapterSummaryORM(id="a", book_name="书", chapter_no=1, summary="概"))
        test_db.commit()
        r = pi.merge_arcs(test_db, "书")
        assert r["arcs"] == 0 and "error" in r

    def test_bad_json_keeps_segment_data(self, test_db, monkeypatch):
        self._seed(test_db, {1: [1], 2: [2]})
        self._mock_ds(monkeypatch, "不是 JSON")
        r = pi.merge_arcs(test_db, "书")
        assert r["arcs"] == 0 and "error" in r
        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert all(x.segment_no is not None and x.arc_no is None for x in rows)

    def test_remerge_clears_old_arc(self, test_db, monkeypatch):
        """重算前必须清旧 arc 标记（否则旧弧号残留产生交错）。

        08-B8③ 后「全部段已有弧」默认幂等跳过 → 本测试语义变为「强制重算」：
        force=True 时依旧先清旧标记再重建。
        """
        self._seed(test_db, {1: [1], 2: [2]})
        test_db.query(pi.ChapterSummaryORM).update({"arc_no": 9, "arc_name": "旧的"})
        test_db.commit()
        self._mock_ds(monkeypatch, json.dumps({"arcs": [
            {"segments": [1, 2], "name": "新弧", "summary": "新概括"}]}, ensure_ascii=False))
        pi.merge_arcs(test_db, "书", force=True)
        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert all(x.arc_no == 1 and x.arc_name == "新弧" for x in rows)

    def test_no_ds_key_raises(self, test_db, monkeypatch):
        self._seed(test_db, {1: [1]})
        monkeypatch.setattr(pi, "ds_key", lambda db: None)
        try:
            pi.merge_arcs(test_db, "书")
            raised = False
        except RuntimeError as e:
            raised = "Key" in str(e)
        assert raised, "无 DeepSeek Key 应明确报错"


class TestArcProvider:
    """弧归并 provider 可选（2026-09-15）：deepseek / modelscope 两条路，逻辑共用只换端点。

    重点验证：① 魔搭走 `_ms_post` 且发对思考字段；② 两条路的入参互不串（不能误调 ds_key）；
    ③ 非法 provider 明确报错；④ 记账 scene 前缀能被 make_usage_cb 正确映射成 vendor。
    """

    def _seed(self, test_db):
        test_db.add(pi.ChapterSummaryORM(
            id="c1", book_name="书", chapter_no=1, summary="第1章概括",
            segment_no=1, segment_summary="段1概括", plot_label="测"))
        test_db.commit()

    def test_modelscope_uses_ms_post_and_ms_key(self, test_db, monkeypatch):
        self._seed(test_db)
        seen = {}

        def fake_ms(key, content, **kw):
            seen["key"] = key
            seen["scene"] = kw.get("on_usage")
            return json.dumps({"arcs": [{"segments": [1], "name": "弧", "summary": "s"}]},
                              ensure_ascii=False)
        monkeypatch.setattr(pi, "ms_key", lambda db: "ms-key")
        monkeypatch.setattr(pi, "_ms_post", fake_ms)
        # ds_key / _ds_post 必须**不被调用** —— 挂成会炸的桩来钉住这一点
        monkeypatch.setattr(pi, "ds_key", lambda db: (_ for _ in ()).throw(
            AssertionError("provider=modelscope 时不该读 DeepSeek Key")))
        monkeypatch.setattr(pi, "_ds_post", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("provider=modelscope 时不该调 _ds_post")))

        r = pi.merge_arcs(test_db, "书", provider="modelscope")
        assert r["arcs"] == 1
        assert seen["key"] == "ms-key"

    def test_default_provider_is_deepseek(self, test_db, monkeypatch):
        """不传 provider → 走 DeepSeek（向后兼容，老命令行为不变）。"""
        self._seed(test_db)
        called = {"ds": 0}
        monkeypatch.setattr(pi, "ds_key", lambda db: "ds-key")

        def fake_ds(key, content, **kw):
            called["ds"] += 1
            return json.dumps({"arcs": [{"segments": [1], "name": "弧", "summary": "s"}]},
                              ensure_ascii=False)
        monkeypatch.setattr(pi, "_ds_post", fake_ds)
        pi.merge_arcs(test_db, "书")
        assert called["ds"] == 1

    def test_unknown_provider_raises(self, test_db):
        self._seed(test_db)
        try:
            pi.merge_arcs(test_db, "书", provider="openai")
            raised = False
        except ValueError:
            raised = True
        assert raised, "未知 provider 应明确报错，而不是静默走默认"

    def test_no_ms_key_raises(self, test_db, monkeypatch):
        self._seed(test_db)
        monkeypatch.setattr(pi, "ms_key", lambda db: None)
        try:
            pi.merge_arcs(test_db, "书", provider="modelscope")
            raised = False
        except RuntimeError as e:
            raised = "魔搭" in str(e) or "Key" in str(e)
        assert raised, "无魔搭 Key 应明确报错"

    def test_ms_post_body_disables_thinking(self, monkeypatch):
        """_ms_post 必须发 chat_template_kwargs.enable_thinking=False。

        这是 ModelScope Qwen3.x 的硬坑（docs/04 §B2/B13）：默认思考开，
        reasoning 会吃光 max_tokens 并污染 content。字段名必须走 chat_template_kwargs
        （顶层 thinking.type 对魔搭无效）。
        """
        captured = {}

        def fake_chat(url, key, body, **kw):
            captured["url"] = url
            captured["body"] = body
            return "{}"
        monkeypatch.setattr(pi, "_chat_post", fake_chat)
        pi._ms_post("k", "hello", max_tokens=123)
        assert "modelscope" in captured["url"]
        assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert captured["body"]["model"] == pi.MS_MODEL
        assert captured["body"]["max_tokens"] == 123


class TestUsageCbVendorMapping:
    """记账回调的 scene→vendor/model 映射（2026-09-15 修：新增 ms_ 前缀）。

    回归用例：此前用 startswith("ds_") 二分，ms_arc 会被误记成 siliconflow/Qwen3-8B，
    成本分析看错账。
    """

    def test_prefix_mapping(self, monkeypatch):
        import app.services.usage_crud as usage_crud
        import app.core.database as database
        recorded = {}

        class _FakeSession:
            def close(self):
                pass

        monkeypatch.setattr(database, "get_engine", lambda: None)
        monkeypatch.setattr(database, "SessionLocal", _FakeSession)

        def fake_record(db, **kw):
            recorded.update(kw)
        monkeypatch.setattr(usage_crud, "record_usage", fake_record)

        for scene, want_vendor, want_model in [
            ("ds_arc", "deepseek", pi.DS_MODEL),
            ("ms_arc", "modelscope", pi.MS_MODEL),
            ("sf_label", "siliconflow", pi.SF_MODEL),
        ]:
            recorded.clear()
            cb = pi.make_usage_cb(scene)
            cb({"prompt_tokens": 1}, 10, True)
            assert recorded.get("vendor") == want_vendor, f"{scene} vendor"
            assert recorded.get("model_name") == want_model, f"{scene} model"


class TestReportThreeLevels:
    def test_report_with_arc(self, test_db, tmp_path):
        test_db.add(pi.ChapterSummaryORM(
            id="a", book_name="书", chapter_no=1, title="第一章", summary="章概括一",
            segment_no=1, segment_summary="段概括", plot_label="学院大比",
            arc_no=1, arc_name="觉醒", arc_summary="弧概括"))
        test_db.commit()
        pi.export_report(test_db, "书", str(tmp_path / "r.md"))
        text = (tmp_path / "r.md").read_text(encoding="utf-8")
        assert "## 弧 1 · 觉醒" in text
        assert "### 段 1 · 学院大比" in text
        assert "**弧概括**：弧概括" in text
        assert "故事弧：1 个" in text
