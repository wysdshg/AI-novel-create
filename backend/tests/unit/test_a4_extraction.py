# -*- coding: utf-8 -*-
"""A4 抽取分级单测：normalize kind 白名单 / item+skill 同步（ai_generated、重名 skip）。"""
import uuid

import pytest

from app.models.orm import ProjectORM
from app.services import ingestion as ing
from app.services import item_crud, skill_crud, faction_crud


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
    assert st_i == {"created": 1, "skipped": 1}
    assert st_s == {"created": 1, "skipped": 0}
    assert st_f == {"created": 1, "skipped": 0}
    it = item_crud.list_items(test_db, pid)[0]
    assert it.name == "淬体丹" and it.category == "丹药" and it.ai_generated is True
    sk = skill_crud.sync_from_extract and test_db.query(
        __import__("app.models.orm", fromlist=["SkillORM"]).SkillORM).filter_by(project_id=pid).all()
    assert sk[0].name == "焚天掌" and sk[0].ai_generated is True
    # faction 链不受影响（原逻辑照常）
    fs = faction_crud.list_factions(test_db, pid)
    assert [f.name for f in fs] == ["青云宗"]
