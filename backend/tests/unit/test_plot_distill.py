"""Phase 7.1 模板凝练：弧收集 / 跨书聚类 / 凝练入库 / 一键流程。

覆盖重点：
- 聚类在**向量不可用时退化为每弧一组**（不硬报错）；
- 凝练结果缺字段时不入库（不写半成品）；
- **单组失败不中断整体**（批量流程的稳定性）；
- variants 的 src 溯源正确（模板最有价值的部分）。
"""
import json

import app.services.embedding_client as ec
from app.models.orm import PlotTemplateORM
from app.services import plot_distill as pd


def _seed_arcs(test_db, spec):
    """spec: [(book, arc_no, arc_name, [(seg_no, label, seg_summary, [chapters])])]"""
    for book, arc_no, arc_name, segs in spec:
        for seg_no, label, ssum, chapters in segs:
            for ch in chapters:
                test_db.add(pd.ChapterSummaryORM(
                    id=f"{book}-{ch}", book_name=book, chapter_no=ch,
                    summary=f"{book}第{ch}章概括", segment_no=seg_no,
                    segment_summary=ssum, plot_label=label,
                    arc_no=arc_no, arc_name=arc_name, arc_summary=f"{arc_name}的弧概括"))
    test_db.commit()


class TestCollectArcs:
    def test_aggregate_beats(self, test_db):
        _seed_arcs(test_db, [
            ("甲书", 1, "觉醒", [
                (1, "日常", "段一概括", [1, 2]),
                (2, "冲突", "段二概括", [3]),
            ]),
            ("乙书", 1, "夺舍", [(1, "诡异", "段三概括", [1])]),
        ])
        arcs = pd.collect_arcs(test_db)
        assert len(arcs) == 2
        a = [x for x in arcs if x["book"] == "甲书"][0]
        assert a["name"] == "觉醒" and a["summary"] == "觉醒的弧概括"
        assert len(a["beats"]) == 2
        assert a["beats"][0]["label"] == "日常"
        assert a["beats"][0]["chapters"] == [1, 2]
        assert a["beats"][1]["chapters"] == [3]

    def test_filter_books(self, test_db):
        _seed_arcs(test_db, [
            ("甲书", 1, "A", [(1, "l", "s", [1])]),
            ("乙书", 1, "B", [(1, "l", "s", [1])]),
        ])
        assert len(pd.collect_arcs(test_db, ["甲书"])) == 1

    def test_empty(self, test_db):
        assert pd.collect_arcs(test_db) == []


class TestClusterArcs:
    def _arcs(self):
        return [
            {"book": "甲书", "arc_no": 1, "name": "学院大比", "summary": "封闭赛场阶梯竞争", "beats": []},
            {"book": "乙书", "arc_no": 1, "name": "学院比试", "summary": "同门擂台比试晋级", "beats": []},
            {"book": "丙书", "arc_no": 2, "name": "秘境寻宝", "summary": "上古遗迹探索夺宝", "beats": []},
        ]

    def test_vector_clusters_similar(self, test_db, monkeypatch):
        monkeypatch.setattr(pd.vector_index, "enabled", lambda db: True)
        vecs = [[1.0, 0.0, 0.0], [0.99, 0.01, 0.0], [0.0, 1.0, 0.0]]   # 前两个相近
        monkeypatch.setattr(ec, "embed_texts", lambda texts, db=None: vecs[:len(texts)])
        groups = pd.cluster_arcs(test_db, self._arcs(), threshold=0.9)
        assert len(groups) == 2
        big = [g for g in groups if len(g["arcs"]) == 2][0]
        assert sorted(big["books"]) == ["乙书", "甲书"]
        assert big["avg_sim"] and big["avg_sim"] > 0.9

    def test_no_vector_falls_back(self, test_db, monkeypatch):
        monkeypatch.setattr(pd.vector_index, "enabled", lambda db: False)
        groups = pd.cluster_arcs(test_db, self._arcs())
        assert len(groups) == 3
        assert all(len(g["arcs"]) == 1 and g["avg_sim"] is None for g in groups)

    def test_embed_failure_falls_back(self, test_db, monkeypatch):
        """embedding 抛错时也要退化，不炸。"""
        monkeypatch.setattr(pd.vector_index, "enabled", lambda db: True)

        def boom(texts, db=None):
            raise RuntimeError("embed down")
        monkeypatch.setattr(ec, "embed_texts", boom)
        groups = pd.cluster_arcs(test_db, self._arcs())
        assert len(groups) == 3


class TestDistillTemplate:
    def _arcs(self):
        return [{
            "book": "甲书", "arc_no": 1, "name": "觉醒",
            "summary": "主角觉醒金手指",
            "beats": [{"label": "诡异", "summary": "梦入宗门", "chapters": [1, 2]}],
        }]

    def _mock(self, monkeypatch, payload):
        monkeypatch.setattr(pd, "ds_key", lambda db: "k")
        monkeypatch.setattr(pd, "_ds_post", lambda k, c, **kw: payload)

    def _payload(self):
        return json.dumps({
            "name": "金手指觉醒", "logline": "绝境中获得逆天传承",
            "genre_tags": ["玄幻", "成长"],
            "structure": {"phases": [{"phase": "开局", "beats": [
                {"beat": "异常入梦", "variants": [{"src": "甲书", "how": "梦中成为宗门弟子"}]}]}]},
            "pitfalls": ["觉醒过程太随意"], "rhythm": "1-2",
        }, ensure_ascii=False)

    def test_create_draft_with_source(self, test_db, monkeypatch):
        self._mock(monkeypatch, self._payload())
        o, _raw = pd.distill_template(test_db, self._arcs())
        assert o is not None
        assert o.name == "金手指觉醒" and o.scale == "arc"
        assert o.status == "draft"                       # 一律 draft 待人工审核
        assert o.source_stats["book_names"] == ["甲书"]
        assert o.source_stats["arc_refs"] == ["甲书#1:觉醒"]
        v = o.structure["phases"][0]["beats"][0]["variants"][0]
        assert v["src"] == "甲书"                        # 溯源必须在
        assert o.pitfalls == ["觉醒过程太随意"]

    def test_missing_fields_not_saved(self, test_db, monkeypatch):
        self._mock(monkeypatch, json.dumps({"name": "只有名字"}))
        o, raw = pd.distill_template(test_db, self._arcs())
        assert o is None and raw
        assert test_db.query(PlotTemplateORM).count() == 0

    def test_empty_arcs_raises(self, test_db):
        try:
            pd.distill_template(test_db, [])
            raised = False
        except ValueError:
            raised = True
        assert raised

    def test_no_key_raises(self, test_db, monkeypatch):
        monkeypatch.setattr(pd, "ds_key", lambda db: None)
        try:
            pd.distill_template(test_db, self._arcs())
            raised = False
        except RuntimeError as e:
            raised = "Key" in str(e)
        assert raised


class TestDistillAll:
    def test_pipeline_two_groups(self, test_db, monkeypatch):
        _seed_arcs(test_db, [
            ("甲书", 1, "觉醒", [(1, "诡异", "梦入宗门", [1, 2])]),
            ("乙书", 1, "夺舍", [(1, "斗法", "神魂交战", [1, 2])]),
        ])
        monkeypatch.setattr(pd.vector_index, "enabled", lambda db: False)   # 退化为每弧一组
        monkeypatch.setattr(pd, "ds_key", lambda db: "k")
        monkeypatch.setattr(pd, "_ds_post", lambda k, c, **kw: json.dumps({
            "name": "觉醒套路", "structure": {"phases": [{"phase": "开局", "beats": []}]},
        }, ensure_ascii=False))
        st = pd.distill_all(test_db)
        assert st["groups"] == 2 and st["created"] == 2
        assert test_db.query(PlotTemplateORM).count() == 2

    def test_min_arcs_filter(self, test_db, monkeypatch):
        _seed_arcs(test_db, [("甲书", 1, "A", [(1, "l", "s", [1])])])
        monkeypatch.setattr(pd.vector_index, "enabled", lambda db: False)
        monkeypatch.setattr(pd, "ds_key", lambda db: "k")
        monkeypatch.setattr(pd, "_ds_post", lambda k, c, **kw: "{}")
        st = pd.distill_all(test_db, min_arcs=2)
        assert st["groups"] == 0 and st["created"] == 0

    def test_single_group_failure_not_fatal(self, test_db, monkeypatch):
        _seed_arcs(test_db, [("甲书", 1, "A", [(1, "l", "s", [1])])])
        monkeypatch.setattr(pd.vector_index, "enabled", lambda db: False)
        monkeypatch.setattr(pd, "ds_key", lambda db: "k")

        def boom(k, c, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(pd, "_ds_post", boom)
        st = pd.distill_all(test_db)
        assert st["created"] == 0 and st["failed"]
        assert "boom" in st["failed"][0]["reason"]

    def test_llm_call_has_no_active_txn(self, test_db, monkeypatch):
        """LLM 调用时必须**没有活跃事务**（2026-09-14 修，用量记账锁的根因）。

        SQLAlchemy Session 是 autobegin：collect_arcs 的 SELECT 会留一个悬挂读事务，
        而记账回调在 `_ds_post` 内同步执行 → 独立 Session 写 llm_usage_logs 必撞 SQLite 锁
        （实测整轮 38/38 记账全丢）。此测试钉住：distill_template 在发起 LLM 调用前已
        rollback 掉那个读事务。
        """
        from unittest.mock import MagicMock
        from sqlalchemy import text
        from app.services import plot_template_crud as tpl_crud
        seen: dict = {}

        def fake_post(k, c, **kw):
            seen["in_txn"] = test_db.in_transaction()
            return json.dumps({"name": "X", "structure": {"phases": []}},
                              ensure_ascii=False)

        monkeypatch.setattr(pd, "ds_key", lambda db: "k")
        monkeypatch.setattr(pd, "_ds_post", fake_post)
        monkeypatch.setattr(tpl_crud, "create", lambda db, d: MagicMock(id="x", name=d.get("name")))

        test_db.execute(text("SELECT 1"))          # 制造悬挂读事务
        assert test_db.in_transaction() is True
        pd.distill_template(test_db, [{"book": "甲书", "arc_no": 1, "name": "X",
                                       "summary": "s", "beats": []}])
        assert seen["in_txn"] is False             # 调用前已 rollback

    def test_pool_scoped_cleanup(self, test_db, monkeypatch):
        """按池清理（2026-09-14 加）：book_names 给定时只清「来源书在本池」的 draft，
        池外 draft 与 reviewed 必须保留 —— 这是「按题材分池凝练」的前提，
        否则跑历史池会把玄幻池的 draft 一起清掉。
        """
        from app.services import plot_template_crud as tpl_crud
        _seed_arcs(test_db, [("甲书", 1, "觉醒", [(1, "诡异", "梦入宗门", [1, 2])])])
        for name, status, books in [("甲池旧模板", "draft", ["甲书"]),
                                    ("乙池旧模板", "draft", ["乙书"]),
                                    ("已审模板", "reviewed", ["甲书"])]:
            tpl_crud.create(test_db, {"name": name, "status": status,
                                      "source_stats": {"books": 1, "book_names": books}})
        monkeypatch.setattr(pd.vector_index, "enabled", lambda db: False)
        monkeypatch.setattr(tpl_crud, "index_template", lambda db, o: None)
        monkeypatch.setattr(pd, "ds_key", lambda db: "k")
        monkeypatch.setattr(pd, "_ds_post", lambda k, c, **kw: json.dumps({
            "name": "新甲池模板",
            "structure": {"phases": [{"phase": "开局", "beats": []}]},
        }, ensure_ascii=False))

        st = pd.distill_all(test_db, book_names=["甲书"], min_arcs=1)

        names = {t.name for t in test_db.query(PlotTemplateORM).all()}
        assert "甲池旧模板" not in names      # 本池 draft → 被清
        assert "乙池旧模板" in names          # 池外 draft → 保留
        assert "已审模板" in names            # reviewed → 永不动
        assert st["created"] == 1             # 甲书那条弧凝练出 1 个新模板
