"""F8 P4：建卡「功能位参考」归档过滤单测（全 mock / 离线）。

背景（2026-10-02 用户真机抓到）：P3 换血归档了旧模板，但 plans.py archetypes 端点
走的 `vector_index.search_similar` 是底层 KNN，不查模板状态 → 池子里已归档旧模板的
残留块照样被召回（「没有位阶/性格的鬼卡」）。修复：多取再按状态过滤，凑满 3 张。
钉住三条行为：
1. archived 模板的命中被剔除；
2. 孤儿块（source_id 无对应模板）被剔除；
3. active 命中凑满 3 张，ref 带结构化 ranks/traits。
"""
import uuid
from types import SimpleNamespace

import pytest

import app.core.database as dbmod
from app.models.orm import (
    ArticleORM, ArticlePlanORM, PlotTemplateORM, PlannedCharORM,
    ProjectORM, VolumeORM,
)
from app.services import plot_template_crud as tpl
from app.routers import plans as plans_router


@pytest.fixture()
def db(test_db):
    return test_db


def _mk_world(db):
    p = ProjectORM(id="p1", name="F8P4")
    v = VolumeORM(id="v1", project_id="p1", name="卷一")
    a = ArticleORM(id="a1", project_id="p1", volume_id="v1", name="篇一")
    plan = ArticlePlanORM(id="pl1", project_id="p1", article_id="a1", plan={"lines": []})
    db.add_all([p, v, a, plan])
    db.commit()
    pc = PlannedCharORM(id="pc1", plan_id="pl1", project_id="p1", article_id="a1",
                        name="王景琰", slot="挚友", slot_desc="主角的引路挚友",
                        first_appearance=1, status="pending")
    db.add(pc)
    # 旧模板（archived）：cast 无 ranks/traits —— P3 归档的 v3 骨架形态
    old = PlotTemplateORM(
        id="t_old", name="参悟传承·围困被困", scale="arc", genre_tags=["原子骨架"],
        logline="旧骨架", status="archived",
        structure={"cast": [{"slot": "质疑同门", "desc": "对主角的诡异手段提出质疑",
                              "mode": "阻碍", "beats": [], "srcs": []}]})
    # 新模板（active）：F8 形态，带 ranks/traits
    new1 = PlotTemplateORM(
        id="t_new1", name="化身引路型挚友", scale="character", genre_tags=["角色模板"],
        logline="引路挚友", status="active",
        structure={"cast": [{"slot": "核心配角位", "desc": "引主角入门的同门挚友",
                              "mode": "助力", "ranks": ["该修士/长辈（具体位阶未观测）"],
                              "traits": {"rationality": 4, "warmth": 4, "guile": -4},
                              "behavior_patterns": ["先引路后托付"],
                              "relation_patterns": [{"target": "同伴", "pattern": "倾力引路"}],
                              "slot_kind": "person", "beats": [], "srcs": []}]})
    new2 = PlotTemplateORM(
        id="t_new2", name="谨慎自保型主角", scale="character", genre_tags=["角色模板"],
        logline="谨慎主角", status="active",
        structure={"cast": [{"slot": "主角位", "desc": "底层求生谨慎算计的主角",
                              "mode": "自利", "ranks": ["炼气期（杂务弟子）"],
                              "traits": {"rationality": 10, "risk": -7},
                              "behavior_patterns": ["藏匿试验后再用"],
                              "relation_patterns": [], "slot_kind": "person",
                              "beats": [], "srcs": []}]})
    db.add_all([pc, old, new1, new2])
    db.commit()
    return pc


def _hit(trow, cast_idx=0):
    c = (trow.structure or {}).get("cast")[cast_idx]
    return SimpleNamespace(chunk_text=tpl.archetype_text(c), score=0.9,
                           source_id=trow.id)


def test_archetypes_filters_archived_and_orphans(db, monkeypatch):
    pc = _mk_world(db)

    # 池子里的命中顺序：归档块在前、孤儿块次之、active 垫后 —— 修复前会返回前两个鬼卡
    fake_hits = [
        _hit(db.query(PlotTemplateORM).filter_by(id="t_old").first()),
        SimpleNamespace(chunk_text="孤儿块的文本", score=0.85, source_id="no_such_template"),
        _hit(db.query(PlotTemplateORM).filter_by(id="t_new1").first()),
        _hit(db.query(PlotTemplateORM).filter_by(id="t_new2").first()),
    ]
    captured = {}

    def fake_search(db_, pool, source_type, query, top_k=3):
        captured["top_k"] = top_k
        return fake_hits

    monkeypatch.setattr(plans_router.vector_index, "search_similar", fake_search)

    r = plans_router.get_planned_char_archetypes("p1", "a1", pc.id, db)
    items = r["data"]["archetypes"]
    assert [it["template_id"] for it in items] == ["t_new1", "t_new2"]
    assert captured["top_k"] == 12
    # ref 结构化字段到位（前端两框的原料）
    assert items[0]["ref"]["traits"] and items[0]["ref"]["ranks"]
    assert items[0]["ref"]["traits"]["rationality"] == 4
