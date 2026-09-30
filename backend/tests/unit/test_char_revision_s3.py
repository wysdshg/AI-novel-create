# -*- coding: utf-8 -*-
"""S3（03 §8.7）角色修订表单测 —— 验收口径：
造一次 AI 改动 → **角色卡不变** + 出现 pending → 确认后生效 → **能回滚** → 删角色无孤儿。
"""
import uuid

import pytest

from app.models.orm import CharacterORM, CharacterRevisionORM, ProjectORM
from app.schemas.database import CharacterCreate, CharacterUpdate
from app.services import character_crud
from app.services import character_revision_crud as rev


@pytest.fixture()
def env(test_db):
    pid = "p1"
    test_db.add(ProjectORM(id=pid, name="S3 测试"))
    test_db.commit()
    character_crud.create_character(
        test_db, pid, CharacterCreate(name="林砚", role_type="主角", personality="沉稳"))
    o = test_db.query(CharacterORM).filter_by(project_id=pid, name="林砚").first()
    return test_db, pid, o


def _count_revs(db, character_id):
    return db.query(CharacterRevisionORM).filter_by(character_id=character_id).count()


def test_create_writes_v1(env):
    db, pid, o = env
    rows = rev.list_revisions(db, pid, o.id)
    assert len(rows) == 1
    assert rows[0]["status"] == "approved"
    assert rows[0]["source"] == "manual"
    assert "建卡初版" in (rows[0]["note"] or "")
    assert rows[0]["snapshot"]["personality"] == "沉稳"


def test_ai_propose_does_not_touch_card(env):
    db, pid, o = env
    r = rev.propose(db, pid, o.id, {"personality": "沉稳，但多了一分杀伐"},
                    chapter_no=12, note="第 12 章显示 X 有转变迹象")
    assert r["status"] == "pending" and r["source"] == "ai_extract"
    assert r["diff"]["personality"] == ["沉稳", "沉稳，但多了一分杀伐"]
    # 🔴 角色卡**不变**（AI 改动纪律）
    db.refresh(o)
    assert o.personality == "沉稳"


def test_approve_applies_snapshot(env):
    db, pid, o = env
    r = rev.propose(db, pid, o.id, {"personality": "沉稳，但多了一分杀伐"}, chapter_no=12)
    out = rev.decide(db, pid, r["id"], approve=True)
    assert out["applied"] is True
    db.refresh(o)
    assert o.personality == "沉稳，但多了一分杀伐"


def test_reject_keeps_card(env):
    db, pid, o = env
    r = rev.propose(db, pid, o.id, {"personality": "暴躁"})
    rev.decide(db, pid, r["id"], approve=False)
    db.refresh(o)
    assert o.personality == "沉稳"


def test_rollback_restores_earlier_version(env):
    db, pid, o = env
    r1 = rev.list_revisions(db, pid, o.id)[0]           # 建卡初版
    r2 = rev.propose(db, pid, o.id, {"personality": "杀伐果决"}, chapter_no=20)
    rev.decide(db, pid, r2["id"], approve=True)
    db.refresh(o)
    assert o.personality == "杀伐果决"
    out = rev.rollback(db, pid, o.id, r1["id"])
    db.refresh(o)
    assert o.personality == "沉稳"                       # 回到初版
    assert out["rolled_back_to"] == r1["id"]
    assert out["source"] == "rollback"                   # 回滚本身留痕


def test_update_noise_skip(env):
    db, pid, o = env
    before = _count_revs(db, o.id)
    character_crud.update_character(db, pid, o.id, CharacterUpdate(personality=o.personality))
    assert _count_revs(db, o.id) == before               # 无实质变化 → 不写新版本


def test_delete_cascades_revisions(env):
    db, pid, o = env
    character_crud.delete_character(db, pid, o.id)
    assert (db.query(CharacterRevisionORM)
            .filter_by(character_id=o.id).count()) == 0


def test_approve_does_not_clobber_other_fields(env):
    """🔴 回归（2026-09-17 用户实测 bug）：采纳后提案不得把先采纳的字段擦回旧值。

    场景复刻：第 4 章提「境界」、第 6 章提「性格」——**两条都在境界为空时拍的快照**；
    先采纳境界、再采纳性格 → 旧实现会用第 6 章整份快照把境界擦空。
    """
    db, pid, o = env
    r1 = rev.propose(db, pid, o.id, {"current_level": "凝出九道雷纹"}, chapter_no=4)
    r2 = rev.propose(db, pid, o.id, {"personality": "坚韧不屈"}, chapter_no=6)  # 快照里境界仍为空
    rev.decide(db, pid, r1["id"], approve=True)
    db.refresh(o)
    assert o.current_level == "凝出九道雷纹"
    rev.decide(db, pid, r2["id"], approve=True)
    db.refresh(o)
    assert o.personality == "坚韧不屈"
    assert o.current_level == "凝出九道雷纹", "境界被旧快照擦掉了（bug 复现）"


def test_intended_fields_derived_for_legacy_rows(env):
    """老行（无 changed_fields）也能反推出「打算改哪些字段」。"""
    db, pid, o = env
    from app.models.orm import CharacterRevisionORM
    import uuid as _uuid
    from datetime import datetime as _dt
    base = rev.list_revisions(db, pid, o.id)[0]           # 建卡初版
    legacy = CharacterRevisionORM(
        id=_uuid.uuid4().hex, project_id=pid, character_id=o.id,
        snapshot={**(base["snapshot"] or {}), "current_level": "金丹"},
        source="ai_extract", status="pending", chapter_no=9,
        created_at=_dt.utcnow())
    db.add(legacy)
    db.commit()
    fields = rev._intended_fields(db, legacy)
    assert fields == ["current_level"], fields


def test_approve_records_applied_fields(env):
    db, pid, o = env
    r = rev.propose(db, pid, o.id, {"personality": "狠辣"})
    out = rev.decide(db, pid, r["id"], approve=True)
    assert out["applied"] is True
    db.refresh(o)
    assert o.personality == "狠辣"
    row = (db.query(CharacterRevisionORM)
           .filter_by(id=r["id"]).first())
    assert "已应用: personality" in (row.note or "")
