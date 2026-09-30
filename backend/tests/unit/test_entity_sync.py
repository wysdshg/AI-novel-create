"""任务③批次2：抽取 relations/factions 自动落表 + R5 修复（幽灵复活）的单元测试。

三个被测点：
1. relation_crud.sync_from_extract —— 双重防线（本章 characters + 库内解析）+ 四元组幂等
2. faction_crud.sync_from_extract  —— kind=faction 自动落表，重名跳过
3. character_crud.delete_character —— 补清 chapter_memories.characters（R5）

sync 函数只 add+flush 不 commit（调用方统一提交），测试里显式 commit 后断言。
"""
import pytest

from app.models.orm import ChapterMemoryORM, CharacterORM, EntityRelationORM, FactionORM
from app.services import faction_crud, ingestion, relation_crud


# ===========================================================================
# fixtures
# ===========================================================================

@pytest.fixture()
def proj(test_db):
    from app.services import project_crud
    p = project_crud.create_project(
        test_db, {"name": "实体同步测试", "genre": "测试", "summary": "s"})
    return p["id"] if isinstance(p, dict) else p.id


@pytest.fixture()
def two_chars(test_db, proj):
    """建两个库内角色：陈砚（主角）/ 赵烈。返回 {name: id}。"""
    from app.schemas.database import CharacterCreate
    from app.services import character_crud

    ids = {}
    for name, role in (("陈砚", "主角"), ("赵烈", "配角")):
        o = character_crud.create_character(test_db, proj, CharacterCreate(
            name=name, role_type=role))
        ids[o.name] = o.id
    return ids


def _rels(db, pid):
    # A6 遗留清理：sync_from_extract 已写新表 entity_relations
    return db.query(EntityRelationORM).filter_by(
        project_id=pid, a_type="character", b_type="character").all()


# ===========================================================================
# relation_crud.sync_from_extract
# ===========================================================================

def test_sync_relations_basic(test_db, proj, two_chars):
    rels = [{"subject": "陈砚", "object": "赵烈", "type": "师徒"}]
    stats = relation_crud.sync_from_extract(
        test_db, proj, rels, chapter_characters=["陈砚", "赵烈"], chapter_no=3)
    test_db.commit()

    assert stats == {"created": 1, "updated": 0, "skipped": 0}
    rows = _rels(test_db, proj)
    assert len(rows) == 1
    r = rows[0]
    assert r.a_id == two_chars["陈砚"]
    assert r.b_id == two_chars["赵烈"]
    assert r.relation_type == "师徒"
    assert r.meta["strength"] == 50, "统计类字段不交给 LLM，统一 default=50"
    assert "3" in (r.note or "")


def test_sync_relations_idempotent(test_db, proj, two_chars):
    rels = [{"subject": "陈砚", "object": "赵烈", "type": "师徒"}]
    relation_crud.sync_from_extract(
        test_db, proj, rels, chapter_characters=["陈砚", "赵烈"], chapter_no=3)
    test_db.commit()

    # 重跑（同章重摄取）：不重复建，note 更新为最近同现章
    stats = relation_crud.sync_from_extract(
        test_db, proj, rels, chapter_characters=["陈砚", "赵烈"], chapter_no=7)
    test_db.commit()

    assert stats["created"] == 0
    assert stats["updated"] == 1
    rows = _rels(test_db, proj)
    assert len(rows) == 1
    assert "7" in (rows[0].note or "")


def test_sync_relations_not_in_chapter_skipped(test_db, proj, two_chars):
    """防线1：subject 不在本章 characters 里 → 幻觉关系，丢弃。"""
    rels = [{"subject": "陈砚", "object": "赵烈", "type": "师徒"}]
    stats = relation_crud.sync_from_extract(
        test_db, proj, rels, chapter_characters=["陈砚"], chapter_no=1)
    assert stats["skipped"] == 1
    assert _rels(test_db, proj) == []


def test_sync_relations_unknown_character_skipped(test_db, proj, two_chars):
    """防线2：名字解析不到库内角色（新配角未入库）→ 跳过，不落脏边。"""
    rels = [{"subject": "陈砚", "object": "路过的神秘人", "type": "对峙"}]
    stats = relation_crud.sync_from_extract(
        test_db, proj, rels, chapter_characters=["陈砚", "路过的神秘人"], chapter_no=1)
    assert stats["skipped"] == 1
    assert _rels(test_db, proj) == []


def test_sync_relations_self_loop_skipped(test_db, proj, two_chars):
    rels = [{"subject": "陈砚", "object": "陈砚", "type": "独白"}]
    stats = relation_crud.sync_from_extract(
        test_db, proj, rels, chapter_characters=["陈砚"], chapter_no=1)
    assert stats["skipped"] == 1
    assert _rels(test_db, proj) == []


def test_sync_relations_type_change_creates_new_edge(test_db, proj, two_chars):
    """同一对人换了称呼（未婚妻→宿敌）→ 新边，旧边保留（关系演变历史有价值）。"""
    cc = ["陈砚", "赵烈"]
    relation_crud.sync_from_extract(
        test_db, proj,
        [{"subject": "陈砚", "object": "赵烈", "type": "未婚妻"}],
        chapter_characters=cc, chapter_no=1)
    relation_crud.sync_from_extract(
        test_db, proj,
        [{"subject": "陈砚", "object": "赵烈", "type": "宿敌"}],
        chapter_characters=cc, chapter_no=5)
    test_db.commit()

    rows = _rels(test_db, proj)
    assert len(rows) == 2
    assert {r.relation_type for r in rows} == {"未婚妻", "宿敌"}


def test_sync_relations_empty_inputs(test_db, proj, two_chars):
    """空输入 / 无本章角色 → 静默返回零统计，不抛异常。"""
    assert relation_crud.sync_from_extract(test_db, proj, None, None, 1) == {
        "created": 0, "updated": 0, "skipped": 0}
    assert relation_crud.sync_from_extract(
        test_db, proj,
        [{"subject": "陈砚", "object": "赵烈", "type": "师徒"}],
        chapter_characters=[], chapter_no=1)["skipped"] == 0


# ===========================================================================
# faction_crud.sync_from_extract
# ===========================================================================

def _factions(db, pid):
    return db.query(FactionORM).filter_by(project_id=pid).all()


def test_sync_factions_basic(test_db, proj):
    ne = [{"kind": "faction", "name": "玄天宗", "brief": "北境第一大宗"}]
    stats = faction_crud.sync_from_extract(test_db, proj, ne)
    test_db.commit()

    assert stats == {"created": 1, "skipped": 0}
    rows = _factions(test_db, proj)
    assert len(rows) == 1
    assert rows[0].name == "玄天宗"
    assert rows[0].description == "北境第一大宗"


def test_sync_factions_dedup_by_name(test_db, proj):
    ne = [{"kind": "faction", "name": "玄天宗", "brief": "北境第一大宗"}]
    faction_crud.sync_from_extract(test_db, proj, ne)
    test_db.commit()

    # 重跑/后续章再抽到同一势力 → 重名跳过（不覆盖 description）
    stats = faction_crud.sync_from_extract(
        test_db, proj, [{"kind": "faction", "name": "玄天宗", "brief": "另一种说法"}])
    test_db.commit()

    assert stats == {"created": 0, "skipped": 1}
    rows = _factions(test_db, proj)
    assert len(rows) == 1
    assert rows[0].description == "北境第一大宗", "重名跳过时不应被新 brief 覆盖"


def test_sync_factions_ignores_other_kinds(test_db, proj):
    """character/location 仍走人工确认，不自动落 faction 表。"""
    ne = [
        {"kind": "character", "name": "某角色", "brief": "b"},
        {"kind": "location", "name": "某地点", "brief": "b"},
        {"kind": "faction", "name": "铁血盟", "brief": "b"},
    ]
    stats = faction_crud.sync_from_extract(test_db, proj, ne)
    test_db.commit()
    assert stats == {"created": 1, "skipped": 0}
    assert [f.name for f in _factions(test_db, proj)] == ["铁血盟"]


# ===========================================================================
# ingestion：normalize 清洗 + fallback 形状
# ===========================================================================

def test_normalize_relations_shapes():
    out = ingestion.normalize_extract({
        "summary": "s",
        "relations": [
            {"subject": "陈砚", "object": "赵烈", "type": "师徒"},
            {"subject": "陈砚", "object": "王五", "relation_type": "对头"},  # 备用键名
            "陈砚对赵烈：师父",                     # 字符串形态 → 丢弃
            {"subject": "", "object": "赵烈", "type": "x"},   # 缺字段 → 丢弃
            {"subject": "陈砚", "object": "赵烈", "type": "超长" * 15},  # 截到 20
        ],
    })
    rels = out["relations"]
    assert len(rels) == 3
    assert rels[0] == {"subject": "陈砚", "object": "赵烈", "type": "师徒"}
    assert rels[1]["type"] == "对头", "relation_type 备用键名要兼容"
    assert rels[2]["type"] == "超长" * 10, "relation_type 列宽 String(20)，必须截断"


def test_normalize_relations_cap_12():
    out = ingestion.normalize_extract({
        "summary": "s",
        "relations": [{"subject": "甲", "object": "乙", "type": "识"} for _ in range(20)],
    })
    assert len(out["relations"]) == 12


def test_normalize_relations_absent_key_defaults_empty():
    out = ingestion.normalize_extract({"summary": "旧输出没有 relations 键"})
    assert out["relations"] == []


def test_fallback_extract_has_relations_key(test_db, proj):
    """fallback dict 必须带 relations 键：摄取落库直接吃这个 dict，形状不一致会炸。"""
    fb = ingestion.fallback_extract(test_db, proj, "第一段。\n第二段。")
    assert fb["relations"] == []


# ===========================================================================
# R5：delete_character 补清 chapter_memories.characters（幽灵复活）
# ===========================================================================

def test_delete_character_clears_chapter_memories(test_db, proj, two_chars):
    from app.schemas.database import CharacterCreate
    from app.services import character_crud

    # 第三号角色，出现在两条章记忆里
    o = character_crud.create_character(test_db, proj, CharacterCreate(name="老怪物"))
    test_db.add(ChapterMemoryORM(
        id="mem1", project_id=proj, chapter_id="c1", chapter_no=1,
        summary="s", characters=["陈砚", "老怪物", "赵烈"]))
    test_db.add(ChapterMemoryORM(
        id="mem2", project_id=proj, chapter_id="c2", chapter_no=2,
        summary="s", characters=["老怪物"]))
    test_db.commit()

    assert character_crud.delete_character(test_db, proj, o.id) is True
    test_db.commit()

    mems = {m.id: list(m.characters or [])
            for m in test_db.query(ChapterMemoryORM).filter_by(project_id=proj).all()}
    assert mems["mem1"] == ["陈砚", "赵烈"], "其余名字必须原样保留"
    assert mems["mem2"] == []


def test_delete_character_keeps_other_projects_memory(test_db, proj, two_chars):
    """作用域正确：同名角色在别的作品，其章记忆不受影响。"""
    from app.services import character_crud, project_crud

    p2 = project_crud.create_project(
        test_db, {"name": "另一部作品", "genre": "测试", "summary": "s"})
    pid2 = p2["id"] if isinstance(p2, dict) else p2.id
    test_db.add(ChapterMemoryORM(
        id="mem_other", project_id=pid2, chapter_id="c9", chapter_no=1,
        summary="s", characters=["陈砚"]))
    test_db.commit()

    chen = test_db.query(CharacterORM).filter_by(project_id=proj, name="陈砚").first()
    character_crud.delete_character(test_db, proj, chen.id)
    test_db.commit()

    row = test_db.query(ChapterMemoryORM).filter_by(id="mem_other").first()
    assert list(row.characters or []) == ["陈砚"], "删除只作用于本项目章记忆"
