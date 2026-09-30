"""级联删除（B 方案：最易崩的第 3 处，2026-09-09 e2e 三轮实测为 0 残留）。

用 test_db fixture（独立临时 SQLite）固化整链级联：
project → volume → article → chapter → 章级记忆（摄取生成）+ 角色库
+ 对话区 + 参考文档，删 project 后全部清空；同时验证删除只波及自己。

注：临时库没有模型配置，摄取走 fallback_extract 规则抽取（不走 LLM），
正好把「无模型可用」这条降级路径也一起测了。
"""
import pytest

from app.schemas.article import ArticleCreate
from app.schemas.chapter import ChapterCreate
from app.schemas.database import CharacterCreate
from app.schemas.volume import VolumeCreate


@pytest.fixture()
def populated_db(test_db):
    """建好一条完整数据链：project → volume → article → chapter(+摄取) → 角色/对话。"""
    from app.services import (
        article_crud, chapter_crud, character_crud, discussion_crud,
        ingestion, project_crud, volume_crud,
    )

    proj = project_crud.create_project(
        test_db, {"name": "级联测试作品", "genre": "测试", "summary": "s"})
    pid = proj["id"] if isinstance(proj, dict) else proj.id

    character_crud.create_character(
        test_db, pid, CharacterCreate(name="陈砚", role_type="主角"))
    discussion_crud.add_message(test_db, pid, "user", "下一章让主角出门")

    vol = volume_crud.create_volume(test_db, pid, VolumeCreate(name="卷一", summary=""))
    art = article_crud.create_article(
        test_db, pid, ArticleCreate(volume_id=vol.id, name="篇一", summary=""))
    ch = chapter_crud.create_chapter(
        test_db, pid,
        ChapterCreate(chapter_no=1, title="第一章", content="雨落在青石板上。" * 20,
                      word_count=140, article_id=art.id))
    # 触发写后摄取（fallback 路径）→ 章级记忆 + 参考文档摘要（级联面里最隐蔽的两张表）
    ingestion.ingest_chapter(test_db, pid, ch)
    test_db.commit()
    return pid


def _counts(test_db, pid):
    from app.models.orm import (
        ArticleORM, ChapterMemoryORM, ChapterORM, CharacterORM,
        DiscussionMessageORM, ReferenceDocORM, VolumeORM,
    )
    return {
        "volumes": test_db.query(VolumeORM).filter_by(project_id=pid).count(),
        "articles": test_db.query(ArticleORM).filter_by(project_id=pid).count(),
        "chapters": test_db.query(ChapterORM).filter_by(project_id=pid).count(),
        "memories": test_db.query(ChapterMemoryORM).filter_by(project_id=pid).count(),
        "characters": test_db.query(CharacterORM).filter_by(project_id=pid).count(),
        "discussion": test_db.query(DiscussionMessageORM).filter_by(project_id=pid).count(),
        "reference_docs": test_db.query(ReferenceDocORM).filter_by(project_id=pid).count(),
    }


def test_populated_chain_exists(populated_db, test_db):
    counts = _counts(test_db, populated_db)
    assert counts["volumes"] == 1
    assert counts["articles"] == 1
    assert counts["chapters"] == 1
    # 摄取产物：章级记忆至少 1 条；参考文档≥1（章摘要，fallback 路径也落库）
    assert counts["memories"] >= 1
    assert counts["reference_docs"] >= 1
    assert counts["characters"] == 1
    assert counts["discussion"] == 1


def test_cascade_delete_clears_everything(populated_db, test_db):
    from app.services import project_crud
    from app.models.orm import ProjectORM

    pid = populated_db
    before = _counts(test_db, pid)
    assert sum(before.values()) >= 6  # 链是真的存在过

    assert project_crud.delete_project(test_db, pid) is True
    test_db.commit()

    after = _counts(test_db, pid)
    assert all(v == 0 for v in after.values()), f"残留: {after}"
    assert test_db.query(ProjectORM).filter_by(id=pid).count() == 0


def test_delete_is_scoped_to_one_project(test_db):
    from app.services import project_crud
    from app.models.orm import ProjectORM

    p1 = project_crud.create_project(test_db, {"name": "甲", "genre": "g", "summary": ""})
    p2 = project_crud.create_project(test_db, {"name": "乙", "genre": "g", "summary": ""})
    id1 = p1["id"] if isinstance(p1, dict) else p1.id
    id2 = p2["id"] if isinstance(p2, dict) else p2.id

    project_crud.delete_project(test_db, id1)
    test_db.commit()

    assert test_db.query(ProjectORM).filter_by(id=id1).count() == 0
    assert test_db.query(ProjectORM).filter_by(id=id2).count() == 1


def test_delete_missing_project_returns_false(test_db):
    from app.services import project_crud
    assert project_crud.delete_project(test_db, "nonexistent-id") is False


# ===========================================================================
# Phase 2.4：**下级**删除的级联（2026-09-10 实测补漏前会留孤儿）
# ===========================================================================

def _seed_four_levels(db):
    """建一条含「派生数据」的完整链：卷/篇/章(+记忆+商讨)/角色(+关系+技能+地点关联)。"""
    import uuid as _uuid

    from app.models.orm import (
        CharacterORM, ChapterMemoryORM, DiscussionMessageORM, FactionORM,
        LocationORM, ProjectORM, ReferenceDocORM, EntityRelationORM, RelationORM, SkillORM,
    )
    from app.schemas.chapter import ChapterCreate
    from app.schemas.article import ArticleCreate
    from app.schemas.volume import VolumeCreate
    from app.services import article_crud, chapter_crud, volume_crud

    pid = _uuid.uuid4().hex
    db.add(ProjectORM(id=pid, name="2.4级联", genre="测试"))
    db.commit()

    vol = volume_crud.create_volume(db, pid, VolumeCreate(name="卷一", summary=""))
    art = article_crud.create_article(db, pid, ArticleCreate(volume_id=vol.id, name="篇一", summary=""))

    c1, c2 = _uuid.uuid4().hex, _uuid.uuid4().hex
    db.add(CharacterORM(id=c1, project_id=pid, name="角色甲"))
    db.add(CharacterORM(id=c2, project_id=pid, name="角色乙"))
    # 旧表 relations 已退役（A6 遗留清理）；级联清理测新表通用边（1.9 remove_edges_of）
    db.add(EntityRelationORM(id=_uuid.uuid4().hex, project_id=pid,
                             a_id=c1, a_type="character", b_id=c2, b_type="character",
                             relation_type="师徒"))
    db.add(SkillORM(id=_uuid.uuid4().hex, project_id=pid, name="技能", owner_id=c1))
    loc = LocationORM(id=_uuid.uuid4().hex, project_id=pid, name="某地", related_ids=[c1, c2])
    db.add(loc)
    fac = FactionORM(id=_uuid.uuid4().hex, project_id=pid, name="某宗",
                     members=["角色甲"], leader_id=c1)
    db.add(fac)

    ch = chapter_crud.create_chapter(
        db, pid, ChapterCreate(chapter_no=1, title="第一章", content="雨落在青石板上。" * 20,
                               word_count=140, article_id=art.id))
    db.add(ChapterMemoryORM(id=_uuid.uuid4().hex, project_id=pid, chapter_id=ch.id,
                            chapter_no=1, summary="摘要"))
    db.add(DiscussionMessageORM(id=_uuid.uuid4().hex, project_id=pid, chapter_id=ch.id,
                                role="user", content="这章的走向"))
    db.add(ReferenceDocORM(id=_uuid.uuid4().hex, project_id=pid, article_id=art.id,
                           filename="篇章参考", content_text="t", source="auto"))
    db.commit()
    return pid, vol.id, art.id, ch.id, c1


def test_delete_character_clears_references(test_db):
    """删角色必须同时清掉：关系（两端任一）、技能 owner、地点关联、势力成员/掌门。"""
    from app.models.orm import (
        EntityRelationORM, FactionORM, LocationORM, SkillORM,
    )
    from app.services import character_crud

    pid, _vid, _aid, _cid, c1 = _seed_four_levels(test_db)
    character_crud.delete_character(test_db, pid, c1)

    for m in (EntityRelationORM, SkillORM):
        assert test_db.query(m).filter_by(project_id=pid).count() == 0, \
            f"{m.__tablename__} 残留孤儿（指向已删角色）"
    loc = test_db.query(LocationORM).filter_by(project_id=pid).first()
    assert c1 not in (loc.related_ids or []), "地点 related_ids 未清理"
    fac = test_db.query(FactionORM).filter_by(project_id=pid).first()
    assert fac.leader_id is None, "势力 leader_id 未清理"
    assert "角色甲" not in (fac.members or []), "势力 members 未清理"


def test_delete_chapter_clears_memory(test_db):
    """删章必须同时清掉该章的「章级记忆」（否则后续章节还会把它注入上下文）。"""
    from app.models.orm import ChapterMemoryORM
    from app.services import chapter_crud

    pid, _vid, _aid, cid, _c1 = _seed_four_levels(test_db)
    chapter_crud.delete_chapter(test_db, pid, cid)
    assert test_db.query(ChapterMemoryORM).filter_by(project_id=pid).count() == 0


def test_delete_article_clears_memory_and_digest(test_db):
    """删篇必须级联清掉：章级记忆 + 章商讨线程 + 篇章参考文档。"""
    from app.models.orm import ChapterMemoryORM, DiscussionMessageORM, ReferenceDocORM
    from app.services import article_crud

    pid, _vid, aid, _cid, _c1 = _seed_four_levels(test_db)
    article_crud.delete_article(test_db, pid, aid)
    for m in (ChapterMemoryORM, ReferenceDocORM):
        assert test_db.query(m).filter_by(project_id=pid).count() == 0, \
            f"{m.__tablename__} 残留孤儿"
    assert test_db.query(DiscussionMessageORM).filter_by(project_id=pid).count() == 0


def test_delete_volume_clears_all_derived(test_db):
    """删卷 → 篇/章及其派生数据（记忆/商讨/篇章参考）全部清空，角色等无关数据保留。"""
    from app.models.orm import (
        ChapterMemoryORM, ChapterORM, DiscussionMessageORM,
        ReferenceDocORM,
    )
    from app.services import volume_crud

    pid, vid, _aid, _cid, _c1 = _seed_four_levels(test_db)
    volume_crud.delete_volume(test_db, pid, vid)

    for m in (ChapterORM, ChapterMemoryORM, ReferenceDocORM, DiscussionMessageORM):
        assert test_db.query(m).filter_by(project_id=pid).count() == 0, \
            f"{m.__tablename__} 残留孤儿"
    # 角色/地点/势力属作品级，不应被卷删除波及
    from app.models.orm import CharacterORM
    assert test_db.query(CharacterORM).filter_by(project_id=pid).count() == 2


# ===========================================================================
