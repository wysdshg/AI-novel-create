"""角色库 CRUD 服务（模块1：分作品资料库）。

分作品隔离通过 project_id 实现：所有查询/写入均按 project_id 过滤。
此处仅实现角色实体；技能/关系/势力等沿用 routers/database.py 的占位桩。
"""
import logging

logger = logging.getLogger(__name__)
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.orm import ChapterMemoryORM, CharacterORM, FactionORM, LocationORM, SkillORM
from app.schemas.database import Character, CharacterCreate, CharacterUpdate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_schema(o: CharacterORM) -> Character:
    return Character(
        id=o.id,
        name=o.name,
        role_type=o.role_type,
        age=o.age,
        gender=o.gender,
        personality=o.personality,
        background=o.background,
        talent=o.talent,
        current_level=o.current_level,
        skills=o.skills or [],
        relationship_network=o.relationship_network or [],
        brief=o.brief,
        network_x=o.network_x,
        network_y=o.network_y,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


def list_characters(db: Session, project_id: str) -> list[Character]:
    rows = (
        db.query(CharacterORM)
        .filter_by(project_id=project_id)
        .order_by(CharacterORM.created_at)
        .all()
    )
    return [_to_schema(r) for r in rows]


def create_character(db: Session, project_id: str, data: CharacterCreate) -> Character:
    now = _now()
    o = CharacterORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        name=data.name,
        role_type=data.role_type,
        age=data.age,
        gender=data.gender,
        personality=data.personality,
        background=data.background,
        talent=data.talent,
        current_level=data.current_level,
        skills=data.skills,
        relationship_network=data.relationship_network,
        brief=data.brief,
        created_at=now,
        updated_at=now,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    # S3（03 §8.7）：建卡 → v1 修订（approved，manual）—— 版本历史从第一笔就有
    from app.services import character_revision_crud as _rev
    _rev.on_character_created(db, o)
    return _to_schema(o)


def get_character(db: Session, project_id: str, character_id: str):
    return (
        db.query(CharacterORM)
        .filter_by(project_id=project_id, id=character_id)
        .first()
    )


def update_character(
    db: Session, project_id: str, character_id: str, data: CharacterUpdate
) -> Character | None:
    o = get_character(db, project_id, character_id)
    if o is None:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(o, field, value)
    o.updated_at = _now()
    db.commit()
    db.refresh(o)
    # S3（03 §8.7）：作者改卡 → 记一版（approved，manual）；与上版相同自动跳过
    from app.services import character_revision_crud as _rev
    _rev.on_character_updated(db, project_id, character_id)
    return _to_schema(o)


def delete_character(db: Session, project_id: str, character_id: str) -> bool:
    """删除角色，并**清掉所有指向它的引用**（Phase 2.4）。

    实测（2026-09-10）原实现只 `db.delete(o)`，会留下三类孤儿：
      - `relations`：以该角色为 subject/object 的关系（关系网里指向不存在的人）
      - `skills`：`owner_id` 指向该角色的技能
      - `locations.related_ids` / `factions.members` / `factions.leader_id`：JSON/字段里的引用
    注意 relations/skills 按 `project_id` 一起过滤，避免跨作品误删。

    R5（2026-09-15）：补清 `chapter_memories.characters`（JSON 列存角色**名字符串**）。
    该列是「AI 记得前文」的注入源——角色删了名字还留在历史章记忆里，
    下一章注入前情时幽灵复活（选角候选/一致性上下文都读它）。
    """
    o = get_character(db, project_id, character_id)
    if o is None:
        return False
    name = o.name or ""

    # 1) 关系：旧表 relations 已退役（A6 遗留清理 2026-10-01），通用边清理见 1.9

    # 1.5) 章级记忆 characters 列（按名字匹配，与列内存储形态一致）
    for mem in db.query(ChapterMemoryORM).filter_by(project_id=project_id).all():
        chars = list(mem.characters or [])
        kept = [x for x in chars if str(x).strip() != name]
        if len(kept) != len(chars):
            mem.characters = kept

    # 1.8) S3 修订历史级联清理（A5 教训：删角色不许留孤儿）
    from app.services import character_revision_crud as _rev
    _rev.delete_all_for_character(db, character_id)

    # 1.9) A2：通用关系边清理
    try:
        from app.services import entity_relation_crud as _er
        _er.remove_edges_of(db, character_id)
    except Exception as e:  # noqa: BLE001 - 双写失败不影响主流程
        logger.warning(f"[dual-write] 同步 entity_relations 失败: {type(e).__name__}: {e}")

    # 2) 技能：owner_id 指向该角色
    db.query(SkillORM).filter_by(project_id=project_id, owner_id=character_id).delete(
        synchronize_session=False)

    # 3) 地点 related_ids / 势力 members·leader_id 里的引用（JSON 列，只能读改写）
    for loc in db.query(LocationORM).filter_by(project_id=project_id).all():
        ids = list(loc.related_ids or [])
        if character_id in ids:
            loc.related_ids = [x for x in ids if x != character_id]
    for fac in db.query(FactionORM).filter_by(project_id=project_id).all():
        if fac.leader_id == character_id:
            fac.leader_id = None
        members = list(fac.members or [])
        kept = [m for m in members if str(m).strip() not in (character_id, name)]
        if len(kept) != len(members):
            fac.members = kept

    db.delete(o)
    db.commit()
    return True
