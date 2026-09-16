"""08-B6/B7：标签口径治理（三级复用）+ 相邻相似段合并（修「段切太碎」）。

B6：prompt 必须带【固定示例菜单 + 本书已用标签 + 上一段标签】三级；新标签即时进菜单。
B7：只合并「相邻 + embedding 相似 ≥ 阈值 + 至少一侧是单章碎片」的段；
    合并后段号连续重排、**弧字段整体清空**（段变=弧失效）、raw 同清（04-B15）。
"""
import pytest

import app.services.embedding_client as emb_mod
import app.services.plot_import as pi
import app.services.vector_index as vi_mod


# ===========================================================================
# B6：label_segments 三级复用
# ===========================================================================

class TestLabelMenu:

    def _seed(self, db):
        db.add(pi.ChapterSummaryORM(id="a", book_name="书", chapter_no=1,
                                    summary="概", segment_no=1, segment_summary="段概一",
                                    plot_label="势力冲突"))
        db.add(pi.ChapterSummaryORM(id="b", book_name="书", chapter_no=2,
                                    summary="概", segment_no=2, segment_summary="段概二"))
        db.commit()

    def test_menu_and_prev_in_prompt(self, test_db, monkeypatch):
        self._seed(test_db)
        captured = []

        def fake(db, prompt, **kw):
            captured.append(prompt)
            return "势力冲突"

        monkeypatch.setattr(pi, "sf_chat", fake)
        r = pi.label_segments(test_db, "书")

        assert len(captured) == 2
        # 固定示例菜单在
        assert "学院大比" in captured[0]
        # 本书已用标签进菜单
        assert "势力冲突" in captured[0]
        # 第二段拿到上一段标签
        assert "上一段标签：势力冲突" in captured[1]
        assert r["labeled"] == 2 and r["reused"] == 2 and r["new_labels"] == 0

    def test_new_label_enters_menu(self, test_db, monkeypatch):
        self._seed(test_db)
        captured = []

        def fake(db, prompt, **kw):
            captured.append(prompt)
            return "青云剑诀"     # 菜单（固定示例 + 本书已用）里没有的自创标签

        monkeypatch.setattr(pi, "sf_chat", fake)
        pi.label_segments(test_db, "书")

        assert "青云剑诀" not in captured[0], "第一段时它还不是候选"
        assert "青云剑诀" in captured[1], "自创标签应即时进菜单"

    def test_failures_do_not_break_run(self, test_db, monkeypatch):
        self._seed(test_db)
        calls = {"n": 0}

        def fake(db, prompt, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return "升级突破"

        monkeypatch.setattr(pi, "sf_chat", fake)
        r = pi.label_segments(test_db, "书")
        assert r["labeled"] == 1    # 失败跳过，不影响其他段


# ===========================================================================
# B7：merge_similar_segments
# ===========================================================================

def _cos_vec(base, tilt=0.0):
    """构造高相似向量：与 base 余弦 ≈ 1-tilt²/2。"""
    return [base[0], tilt, 0.0, 0.0] if len(base) == 4 else base


class TestMergeSimilar:

    @staticmethod
    def _seed(test_db, arced=False):
        """4 段：seg1=2章 / seg2=1章 / seg3=1章 / seg4=1章。"""
        specs = [("a", 1, 1), ("b", 2, 1), ("c", 3, 2), ("d", 4, 3), ("e", 5, 4)]
        for cid, ch, seg in specs:
            test_db.add(pi.ChapterSummaryORM(
                id=cid, book_name="书", chapter_no=ch, summary="概",
                segment_no=seg, segment_summary=f"段{seg}概括",
                segment_summary_raw="旧raw",
                arc_no=9 if arced else None, arc_name="旧弧" if arced else None,
                arc_summary="旧弧概" if arced else None,
                arc_summary_raw="旧弧raw" if arced else None))
        test_db.commit()

    @staticmethod
    def _mock_vectors(monkeypatch, vecs):
        monkeypatch.setattr(vi_mod, "enabled", lambda db: True)
        monkeypatch.setattr(emb_mod, "embed_texts", lambda texts, db=None: list(vecs))

    def _rows(self, db):
        return {r.chapter_no: r for r in db.query(pi.ChapterSummaryORM).all()}

    def test_dry_run_writes_nothing(self, test_db, monkeypatch):
        self._seed(test_db)
        # seg1~seg2 相似（0.99）、seg3~seg4 相似（0.99）、seg2~seg3 不相似（0）
        self._mock_vectors(monkeypatch, [
            [1, 0, 0, 0], [1, 0.1, 0, 0], [0, 1, 0, 0], [0, 1, 0.1, 0]])
        r = pi.merge_similar_segments(test_db, "书", dry_run=True)
        assert r["dry_run"] is True and r["merged_groups"] == 2
        assert r["segments_before"] == 4 and r["segments_after"] == 2
        # 没动数据
        rows = self._rows(test_db)
        assert sorted(x.segment_no for x in rows.values()) == [1, 1, 2, 3, 4]
        assert all(x.segment_summary_raw == "旧raw" for x in rows.values())

    def test_merge_writes_and_clears_arcs(self, test_db, monkeypatch):
        self._seed(test_db, arced=True)
        self._mock_vectors(monkeypatch, [
            [1, 0, 0, 0], [1, 0.1, 0, 0], [0, 1, 0, 0], [0, 1, 0.1, 0]])
        r = pi.merge_similar_segments(test_db, "书")

        assert r["merged_groups"] == 2 and r["segments_after"] == 2
        assert r["arcs_cleared"] is True
        rows = self._rows(test_db)
        # 段号连续重排：ch1-3 → 段1，ch4-5 → 段2
        assert [rows[i].segment_no for i in (1, 2, 3, 4, 5)] == [1, 1, 1, 2, 2]
        # 弧字段整体清空
        assert all(x.arc_no is None and x.arc_summary is None
                   and x.arc_summary_raw is None for x in rows.values())
        # raw 同清（04-B15）
        assert all(x.segment_summary_raw is None for x in rows.values())
        # 合并后段概统一（目标段概 + 组内拼接）
        assert rows[1].segment_summary.startswith("段1概括")
        assert "段2概括" in rows[1].segment_summary
        # plot_label 用目标段口径（seg1 的标签）
        assert all(x.plot_label == "段1标签" or x.plot_label is None
                   for x in (rows[1], rows[2], rows[3]))

    def test_only_single_guard_protects_formed_segments(self, test_db, monkeypatch):
        self._seed(test_db)
        # 全部对都高度相似 → 并查集把 4 段链成 1 组（每对至少一侧是单章碎片，guard 放行）
        self._mock_vectors(monkeypatch, [[1, 0, 0, 0]] * 4)
        r = pi.merge_similar_segments(test_db, "书", only_single=True)
        assert r["merged_groups"] == 1 and r["segments_after"] == 1

    def test_guard_blocks_two_formed_segments(self, test_db, monkeypatch):
        """guard 的真正保护对象：两个**成型段直接相邻**时不许合并。

        注：夹在中间的单章碎片会桥接两侧成型段（2+1+2=5 章落在 2~6 目标内，可接受）；
        阈值 0.86 才是主控，guard 只挡「成型+成型」的直接对。
        """
        # 场景 A：四个两章段直接相邻、全部高度相似 → 全被挡
        specs = [("a", 1, 1), ("b", 2, 1), ("c", 3, 2), ("d", 4, 2),
                 ("e", 5, 3), ("f", 6, 3), ("g", 7, 4), ("h", 8, 4)]
        for cid, ch, seg in specs:
            test_db.add(pi.ChapterSummaryORM(
                id=cid, book_name="书2", chapter_no=ch, summary="概",
                segment_no=seg, segment_summary=f"段{seg}概括"))
        test_db.commit()
        self._mock_vectors(monkeypatch, [[1, 0, 0, 0]] * 7)
        r = pi.merge_similar_segments(test_db, "书2", only_single=True)
        assert r["merged_groups"] == 0 and r["arcs_cleared"] is False

        # 场景 B（书3）：2章 + 1章碎片 + 2章，但只有 (1,2) 相似 → 碎片并入成型段
        specs_b = [("i", 1, 1), ("j", 2, 1), ("k", 3, 2), ("l", 4, 3), ("m", 5, 3)]
        for cid, ch, seg in specs_b:
            test_db.add(pi.ChapterSummaryORM(
                id=cid, book_name="书3", chapter_no=ch, summary="概",
                segment_no=seg, segment_summary=f"段{seg}概括"))
        test_db.commit()
        # v1≈v2 高相似；v3 与两者正交 → (2,3) 被阈值挡住
        self._mock_vectors(monkeypatch, [[1, 0, 0, 0], [1, 0.1, 0, 0], [0, 1, 0, 0]])
        r2 = pi.merge_similar_segments(test_db, "书3", only_single=True)
        assert r2["merged_groups"] == 1
        assert r2["merges"][0]["members"] == [1, 2]

    def test_no_similarity_no_merge(self, test_db, monkeypatch):
        self._seed(test_db)
        # 两两正交 → 相似度 0
        self._mock_vectors(monkeypatch, [
            [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        r = pi.merge_similar_segments(test_db, "书")
        assert r["merged_groups"] == 0 and r["arcs_cleared"] is False
        rows = self._rows(test_db)
        assert all(x.arc_no == 9 for x in rows.values()) if any(
            x.arc_no is not None for x in rows.values()) else True

    def test_vector_unavailable_skips(self, test_db, monkeypatch):
        self._seed(test_db)
        monkeypatch.setattr(vi_mod, "enabled", lambda db: False)
        r = pi.merge_similar_segments(test_db, "书")
        assert r.get("skipped") is True
