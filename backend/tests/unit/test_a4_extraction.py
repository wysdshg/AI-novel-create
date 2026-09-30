# -*- coding: utf-8 -*-
"""A4 抽取分级单测：normalize kind 白名单 / item+skill 同步（ai_generated、重名 skip）/
E3 惯例词归一（命中全局条目库不建本地条目）。"""
import uuid

import pytest

from app.models.orm import ProjectORM, SkillORM
from app.services import ingestion as ing
from app.services import item_crud, skill_crud, faction_crud
from app.services import global_ref_crud as gr


def test_normalize_filters_unknown_kind():
    d = ing.normalize_extract({"summary": "s", "new_entities": [
        {"kind": "item", "name": "淬体丹", "brief": "淬炼肉身", "category": "丹药"},
        {"kind": "法宝", "name": "不知道是什么", "brief": "x"},          # 白名单外 → 丢
        {"kind": "skill", "name": "焚天掌", "brief": "喷火", "category": "拳法"},
        {"name": "没 kind", "brief": "x"},                                # 无 kind → 默认 character ✓ 保留
    ]})
    ne = d["new_entities"]
    kinds = [x["kind"] for x in ne]
    assert kinds == ["item", "skill", "character"]
    assert ne[0]["category"] == "丹药"


def test_extract_prompt_has_item_skill_and_grading():
    src = open(r"E:\AI小说创作\backend\app\services\ingestion.py", encoding="utf-8").read()
    assert "character/faction/location/item/skill" in src
    assert "new_entities 分级" in src
    assert "ai_generated" in src


def test_item_skill_sync_from_extract(test_db):
    pid = "p1"
    test_db.add(ProjectORM(id=pid, name="A4 测试"))
    test_db.commit()
    ents = [
        {"kind": "item", "name": "淬体丹", "brief": "淬炼肉身", "category": "丹药"},
        {"kind": "item", "name": "淬体丹", "brief": "重复"},               # 重名 → skip
        {"kind": "skill", "name": "焚天掌", "brief": "喷火", "category": "拳法"},
        {"kind": "faction", "name": "青云宗", "brief": "正道"},            # 走 faction 链
    ]
    st_i = item_crud.sync_from_extract(test_db, pid, ents)
    st_s = skill_crud.sync_from_extract(test_db, pid, ents)
    st_f = faction_crud.sync_from_extract(test_db, pid, ents)   # faction 链不受影响（原逻辑）
    assert st_i == {"created": 1, "skipped": 1, "normalized": 0, "linked": 0}
    assert st_s == {"created": 1, "skipped": 0, "normalized": 0, "linked": 0}
    assert st_f == {"created": 1, "skipped": 0}
    it = item_crud.list_items(test_db, pid)[0]
    assert it.name == "淬体丹" and it.category == "丹药" and it.ai_generated is True
    sk = skill_crud.sync_from_extract and test_db.query(
        __import__("app.models.orm", fromlist=["SkillORM"]).SkillORM).filter_by(project_id=pid).all()
    assert sk[0].name == "焚天掌" and sk[0].ai_generated is True
    # faction 链不受影响（原逻辑照常）
    fs = faction_crud.list_factions(test_db, pid)
    assert [f.name for f in fs] == ["青云宗"]


# ------------------------- E3 · 惯例词归一（docs/03 阶段E） -------------------------

def test_e3_sync_normalizes_convention_words(test_db):
    """命中全局条目库（主名/别名）的惯例词不建本地条目，计数进 normalized。"""
    pid = "p-e3"
    test_db.add(ProjectORM(id=pid, name="E3 测试"))
    test_db.commit()
    gr.add_item(test_db, name="洗髓丹", category="丹药", brief="改造根骨资质的入门丹药",
                genre="仙侠", aliases=["伐毛洗髓丹"])
    gr.add_skill(test_db, name="御剑术", category="攻击技", brief="驭使飞剑攻敌", genre="仙侠")
    ents = [
        {"kind": "item", "name": "洗髓丹", "brief": "AI 自己写的一版", "category": "丹药"},  # 主名命中
        {"kind": "item", "name": "伐毛洗髓丹", "brief": "别名命中"},                          # 别名命中
        {"kind": "item", "name": "伐骨丹", "brief": "硬造词", "category": "丹药"},            # 库外 → 照常建
        {"kind": "skill", "name": "御剑术", "brief": "通用剑术", "category": "攻击技"},        # 技能惯例词
        {"kind": "skill", "name": "焚天掌", "brief": "独创", "category": "拳法"},             # 照常建
    ]
    st_i = item_crud.sync_from_extract(test_db, pid, ents)
    st_s = skill_crud.sync_from_extract(test_db, pid, ents)
    assert st_i == {"created": 1, "skipped": 0, "normalized": 2, "linked": 0}
    assert st_s == {"created": 1, "skipped": 0, "normalized": 1, "linked": 0}
    # 本地只落库外独创词；惯例词不留本地副本（指向全局条目库）
    assert [it.name for it in item_crud.list_items(test_db, pid)] == ["伐骨丹"]
    assert [sk.name for sk in test_db.query(SkillORM).filter_by(project_id=pid).all()] == ["焚天掌"]


def test_e3_disabled_entry_does_not_normalize(test_db):
    """disabled 条目不算惯例词（normalize_lookup 只认 active）→ 照常建本地条目。"""
    pid = "p-e3b"
    test_db.add(ProjectORM(id=pid, name="E3 下线词"))
    test_db.commit()
    gr.add_item(test_db, name="旧词丹", category="丹药", brief="已下线的口径", genre="仙侠")
    test_db.query(gr.GlobalItemORM).filter_by(name="旧词丹").update({"status": "disabled"})
    test_db.commit()
    st = item_crud.sync_from_extract(
        test_db, pid, [{"kind": "item", "name": "旧词丹", "brief": "x", "category": "丹药"}])
    assert st == {"created": 1, "skipped": 0, "normalized": 0, "linked": 0}


# ------------------------- A7 · 抽取归属连边（2026-10-01 拍板口径） -------------------------

def _make_project_with_chars(test_db, pid: str, names: list[str]) -> None:
    from app.models.orm import CharacterORM
    test_db.add(ProjectORM(id=pid, name=f"{pid} 项目"))
    for n in names:
        test_db.add(CharacterORM(id=uuid.uuid4().hex, project_id=pid, name=n,
                                 status="alive", created_at=__import__("datetime").datetime.utcnow()))
    test_db.commit()


def _edges_of(test_db, pid: str) -> list:
    from app.models.orm import EntityRelationORM
    return test_db.query(EntityRelationORM).filter_by(project_id=pid).all()


def test_a7_owner_link_creates_edges(test_db):
    """AI 填 owner → 「持有物品」「掌握技能」边自动落边表（GraphRAG 数据源）。"""
    pid = "p-a7"
    _make_project_with_chars(test_db, pid, ["李长生"])
    ents = [
        {"kind": "item", "name": "青云剑", "brief": "本命飞剑", "category": "武器",
         "owner": "李长生"},
        {"kind": "skill", "name": "青元剑诀", "brief": "剑修功法", "category": "剑术",
         "owner": "李长生"},
    ]
    st_i = item_crud.sync_from_extract(test_db, pid, ents,
                                       chapter_characters=["李长生"], chapter_no=5)
    st_s = skill_crud.sync_from_extract(test_db, pid, ents,
                                        chapter_characters=["李长生"], chapter_no=5)
    assert st_i["linked"] == 1 and st_s["linked"] == 1
    edges = _edges_of(test_db, pid)
    pairs = {(e.relation_type, e.a_type, e.b_type) for e in edges}
    assert ("持有物品", "character", "item") in pairs
    assert ("掌握技能", "character", "skill") in pairs
    assert all(e.note == "第5章抽取" for e in edges)


def test_a7_owner_rel_links_existing_entity(test_db):
    """重名跳过分支也连边 → 重摄取可为存量孤儿实体补边（幂等不重复）。"""
    pid = "p-a7b"
    _make_project_with_chars(test_db, pid, ["李长生"])
    ents = [{"kind": "item", "name": "青云剑", "brief": "本命飞剑",
             "category": "武器", "owner": "李长生"}]
    # 第一次：无 owner（模拟存量摄取），只落库不连边
    item_crud.sync_from_extract(test_db, pid, ents[:0] + [
        {"kind": "item", "name": "青云剑", "brief": "本命飞剑", "category": "武器"}])
    assert len(_edges_of(test_db, pid)) == 0
    # 第二次：带 owner 重摄取（幂等重跑）
    st = item_crud.sync_from_extract(test_db, pid, ents,
                                     chapter_characters=["李长生"], chapter_no=6)
    assert st == {"created": 0, "skipped": 1, "normalized": 0, "linked": 1}
    assert len(_edges_of(test_db, pid)) == 1


def test_a7_owner_hallucination_and_unknown_skipped(test_db):
    """双防线：owner 不在本章名单 / 解析不到库内角色 → 都不连边，不报错。"""
    pid = "p-a7c"
    _make_project_with_chars(test_db, pid, ["李长生"])
    ents = [
        {"kind": "item", "name": "幻主丹", "category": "丹药",
         "owner": "王麻子"},          # 防线1：不在本章 characters → 拒
        {"kind": "item", "name": "未入库剑", "category": "武器",
         "owner": "张三丰"},          # 防线2：本章在名单但库里没有 → 拒
        {"kind": "item", "name": "无主物", "category": "材料"},  # 无 owner → 不连
    ]
    st = item_crud.sync_from_extract(test_db, pid, ents,
                                     chapter_characters=["李长生", "张三丰"], chapter_no=7)
    assert st["created"] == 3 and st["linked"] == 0
    assert len(_edges_of(test_db, pid)) == 0


def test_a7_normalize_keeps_owner():
    """normalize_extract 保留 owner 字段（洗掉则连边链断供）。"""
    d = ing.normalize_extract({"summary": "s", "characters": ["李长生"], "new_entities": [
        {"kind": "item", "name": "青云剑", "brief": "x", "category": "武器", "owner": "李长生"},
        {"kind": "skill", "name": "青元剑诀", "brief": "x", "category": "剑术"},
    ]})
    ne = d["new_entities"]
    assert ne[0]["owner"] == "李长生"
    assert ne[1]["owner"] is None
