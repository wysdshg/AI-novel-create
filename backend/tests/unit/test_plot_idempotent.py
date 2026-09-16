"""08-B8③ 幂等跳过：`merge_arcs` / `distill_all` 重复跑不再重复烧 LLM 钱。

判据：
- `merge_arcs`：该书全部有段的章都已带弧 → `skipped=True`（**不调 LLM、不清数据**）；
  部分覆盖（增量灌了新段）→ 全量重算（新段须与旧段一起重归并）；`force=True` 强制重算。
- `distill_all`：弧集合 + 聚类参数与上次成功运行一致（`app_configs` 指纹）→ `skipped=True`；
  任一变化（新弧 / 改 threshold / 换书池）→ 正常跑，跑完更新指纹（含部分失败，重试失败组用 force）。

商讨商定（2026-09-16）：跳过检查必须在**一切清理 / LLM 动作之前**
（`replace_drafts` 会删 draft 模板 —— 指纹一致却先清了库就是中间态损坏）。
"""
import pytest

import app.services.plot_import as pi
import app.services.plot_distill as pd

ARC_JSON = '{"arcs": [{"name": "弧一", "summary": "弧摘要", "segments": [1, 2]}]}'


def _mk_seg(db, cid, no, seg_no, arc_no=None):
    db.add(pi.ChapterSummaryORM(id=cid, book_name="书", chapter_no=no,
                                summary="概", segment_no=seg_no, segment_summary="段概",
                                arc_no=arc_no))


def _no_llm(*a, **kw):
    raise AssertionError("跳过路径不应触发任何 LLM 调用")


# ===========================================================================
# merge_arcs：全有弧 → 跳过；部分覆盖 → 全量重算；force → 强制重算
# ===========================================================================

class TestMergeArcsIdempotent:

    def test_skip_when_all_arced(self, test_db, monkeypatch):
        for cid, no, arc in (("a", 1, 1), ("b", 2, 1), ("c", 3, 2)):
            _mk_seg(test_db, cid, no, 1 if no <= 2 else 2, arc_no=arc)
        test_db.commit()
        monkeypatch.setattr(pi, "_ds_post", _no_llm)
        monkeypatch.setattr(pi, "_ms_post", _no_llm)

        r = pi.merge_arcs(test_db, "书")

        assert r.get("skipped") is True
        assert r["arcs"] == 2 and r["segments"] == 3
        # 数据未被清（跳过 ≠ 重算前清空）
        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert all(x.arc_no for x in rows)

    def test_runs_when_partially_covered(self, test_db, monkeypatch):
        # 模拟增量：老段带弧，新段没弧 → 全量重算（新段与旧段一起重归并）
        _mk_seg(test_db, "a", 1, 1, arc_no=1)
        _mk_seg(test_db, "b", 2, 2)
        _mk_seg(test_db, "c", 3, 2)
        test_db.commit()
        monkeypatch.setattr(pi, "ds_key", lambda db: "fake-key")
        monkeypatch.setattr(pi, "_ds_post", lambda *a, **kw: ARC_JSON)

        r = pi.merge_arcs(test_db, "书")

        assert r.get("skipped") is None
        assert r["arcs"] >= 1
        rows = test_db.query(pi.ChapterSummaryORM).all()
        assert all(x.arc_no for x in rows), "重算后兜底全覆盖"

    def test_runs_when_none_arced(self, test_db, monkeypatch):
        _mk_seg(test_db, "a", 1, 1)
        _mk_seg(test_db, "b", 2, 1)
        test_db.commit()
        monkeypatch.setattr(pi, "ds_key", lambda db: "fake-key")
        monkeypatch.setattr(pi, "_ds_post", lambda *a, **kw: ARC_JSON)

        r = pi.merge_arcs(test_db, "书")
        assert r.get("skipped") is None
        assert r["arcs"] >= 1

    def test_force_overrides_skip(self, test_db, monkeypatch):
        for cid, no, arc in (("a", 1, 1), ("b", 2, 1)):
            _mk_seg(test_db, cid, no, 1, arc_no=arc)
        test_db.commit()
        monkeypatch.setattr(pi, "ds_key", lambda db: "fake-key")
        monkeypatch.setattr(pi, "_ds_post", lambda *a, **kw: ARC_JSON)

        r = pi.merge_arcs(test_db, "书", force=True)
        assert r.get("skipped") is None


# ===========================================================================
# distill_all：指纹一致 → 跳过；无指纹 → 跑并存储；参数变化 → 重跑
# ===========================================================================

class TestDistillIdempotent:

    @staticmethod
    def _mk_arcs(test_db):
        for cid, no in (("a", 1), ("b", 2)):
            _mk_seg(test_db, cid, no, 1, arc_no=1)
        for r in test_db.query(pi.ChapterSummaryORM).all():
            r.arc_name = "弧一"
            r.arc_summary = "弧摘要"
        test_db.commit()

    def test_skip_when_fingerprint_matches(self, test_db, monkeypatch):
        self._mk_arcs(test_db)
        # 先真实计算并存指纹
        fp = pd._distill_fingerprint(test_db, None, 0.80, 1)
        from app.services import app_config
        app_config.set_value(test_db, "plot_distill.fingerprint.all", fp)
        # 指纹一致 → 应在「昂贵/有状态」的动作前退出：
        #   cluster_arcs（要调 embedding）+ replace_drafts（删 draft 模板）都不许发生。
        # 注：collect_arcs 在指纹计算里会被调一次（廉价 DB 读），不能拿它当断言点。
        monkeypatch.setattr(pd, "cluster_arcs", _no_llm)

        r = pd.distill_all(test_db)

        assert r.get("skipped") is True
        assert r["fingerprint"] == fp
        # draft 模板一个没少（跳过 ≠ 清库重建）
        assert test_db.query(pd.PlotTemplateORM).count() == 0

    def test_runs_when_no_fingerprint_then_stores(self, test_db, monkeypatch):
        self._mk_arcs(test_db)
        monkeypatch.setattr(pd, "cluster_arcs",
                            lambda db, arcs, threshold=0: [])  # 0 组 → 不触 LLM

        r = pd.distill_all(test_db)
        assert r.get("skipped") is None and r["groups"] == 0

        # 指纹已存 → 第二次跑跳过
        r2 = pd.distill_all(test_db)
        assert r2.get("skipped") is True

    def test_threshold_change_triggers_rerun(self, test_db, monkeypatch):
        self._mk_arcs(test_db)
        monkeypatch.setattr(pd, "cluster_arcs", lambda db, arcs, threshold=0: [])
        from app.services import app_config

        pd.distill_all(test_db, threshold=0.80)
        # 换 threshold → 指纹变 → 不跳过（再次跑）
        r = pd.distill_all(test_db, threshold=0.85)
        assert r.get("skipped") is None
        # 且新指纹已覆盖旧指纹
        fp085 = pd._distill_fingerprint(test_db, None, 0.85, 1)
        assert app_config.get(test_db, "plot_distill.fingerprint.all") == fp085

    def test_pool_fingerprints_are_independent(self, test_db, monkeypatch):
        self._mk_arcs(test_db)
        monkeypatch.setattr(pd, "cluster_arcs", lambda db, arcs, threshold=0: [])
        pd.distill_all(test_db, book_names=None)      # 全库池
        r = pd.distill_all(test_db, book_names=["书"])  # 单书池：独立指纹 → 不跳过
        assert r.get("skipped") is None
