"""技能（Skill）CRUD —— 资料库 §2.2（按 project_id 隔离的角色技能）。

注意：本模块与全局写作 SKILL（custom_skill.py / routers/custom_skill.py）独立。
- Skill（这里）：与角色绑定的「技能」，按 project_id 隔离 —— 接口 /projects/{project_id}/skills
- CustomSkill（全局）：AI 提示词模板，无 project —— 接口 /global-skills
"""
import logging

logger = logging.getLogger(__name__)
import uuid
from sqlalchemy.orm import Session

from app.models.orm import SkillORM
from app.services import entity_relation_crud as er  # A2 双写
from app.services import global_ref_crud  # E3 惯例词归一
from app.schemas.database import Skill, SkillCreate, SkillUpdate


def _to_schema(o: SkillORM) -> Skill:
    return Skill(
        id=o.id,
        name=o.name,
        level=o.level,
        effect=o.effect,
        limitation=o.limitation,
        owner_id=o.owner_id,
        side_effect=o.side_effect,
        unlock_condition=o.unlock_condition,
    )


def list_skills(db: Session, project_id: str) -> list[Skill]:
    rows = db.query(SkillORM).filter_by(project_id=project_id).order_by(SkillORM.name).all()
    return [_to_schema(r) for r in rows]


def get_skill(db: Session, project_id: str, skill_id: str) -> Skill | None:
    o = db.query(SkillORM).filter_by(project_id=project_id, id=skill_id).first()
    return _to_schema(o) if o else None


def create_skill(db: Session, project_id: str, data: SkillCreate) -> Skill:
    o = SkillORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        name=data.name,
        level=data.level,
        effect=data.effect,
        limitation=data.limitation,
        owner_id=data.owner_id,
        side_effect=data.side_effect,
        unlock_condition=data.unlock_condition,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    # A2 双写：owner → 「掌握技能」边
    try:
        if o.owner_id:
            er.upsert_edge(db, project_id, o.owner_id, "character", o.id, "skill", "掌握技能")
    except Exception as e:  # noqa: BLE001 - 双写失败不影响主流程
        logger.warning(f"[dual-write] 同步 entity_relations 失败: {type(e).__name__}: {e}")
    return _to_schema(o)


def update_skill(
    db: Session, project_id: str, skill_id: str, data: SkillUpdate
) -> Skill | None:
    o = db.query(SkillORM).filter_by(project_id=project_id, id=skill_id).first()
    if o is None:
        return None
    old_owner = o.owner_id
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(o, field, value)
    db.commit()
    db.refresh(o)
    # A2 双写：owner 变了 → 换边
    try:
        if old_owner != o.owner_id:
            if old_owner:
                er.remove_edge(db, project_id, old_owner, o.id, "掌握技能")
            if o.owner_id:
                er.upsert_edge(db, project_id, o.owner_id, "character", o.id, "skill", "掌握技能")
    except Exception as e:  # noqa: BLE001 - 双写失败不影响主流程
        logger.warning(f"[dual-write] 同步 entity_relations 失败: {type(e).__name__}: {e}")
    return _to_schema(o)


def delete_skill(db: Session, project_id: str, skill_id: str) -> bool:
    o = db.query(SkillORM).filter_by(project_id=project_id, id=skill_id).first()
    if o is None:
        return False
    db.delete(o)
    db.commit()
    # A2 双写：技能没了 → 清它的边
    try:
        er.remove_edges_of(db, skill_id)
    except Exception as e:  # noqa: BLE001 - 双写失败不影响主流程
        logger.warning(f"[dual-write] 同步 entity_relations 失败: {type(e).__name__}: {e}")
    return True


def sync_from_extract(db: Session, project_id: str, new_entities: list | None,
                      chapter_characters: list | None = None,
                      chapter_no: int | None = None) -> dict:
    """AI 抽取的 kind=skill 新技能 → 落库（幂等，重名跳过；**标 ai_generated**，docs/09 M8）。

    E3（docs/03 阶段E）：命中全局条目库惯例词（御剑术/火球术…）不建本地条目，归一指向条目库。
    A7（2026-10-01 拍板）：AI 填了 owner（谁掌握）→ 连「掌握技能」边；新建与重名两分支都连
    （重摄取可为存量孤儿技能补边）。命中惯例词归一的全局条目不连边。
    """
    import logging
    _log = logging.getLogger(__name__)
    from app.services import entity_relation_crud as er
    stats = {"created": 0, "skipped": 0, "normalized": 0, "linked": 0}
    chapter_names = {str(x).strip() for x in (chapter_characters or []) if str(x).strip()}
    for item in new_entities or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip().lower() != "skill":
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            stats["skipped"] += 1
            continue
        if global_ref_crud.normalize_lookup(db, name) is not None:
            stats["normalized"] += 1
            continue
        dup = db.query(SkillORM).filter_by(project_id=project_id, name=name).first()
        if dup is not None:
            # 重名不覆盖，但归属边照连（重摄取为存量技能补边）
            if er.link_owner_from_extract(db, project_id, item.get("owner"), chapter_names,
                                          dup.id, "skill", "掌握技能", chapter_no):
                stats["linked"] += 1
            stats["skipped"] += 1
            continue
        brief = str(item.get("brief") or "").strip()
        o = SkillORM(
            id=uuid.uuid4().hex, project_id=project_id,
            name=name,
            skill_type=str(item.get("category") or "").strip() or None,
            summary=brief or None,
            full_desc=brief or None,
            ai_generated=True,
        )
        db.add(o)
        db.flush()
        if er.link_owner_from_extract(db, project_id, item.get("owner"), chapter_names,
                                      o.id, "skill", "掌握技能", chapter_no):
            stats["linked"] += 1
        stats["created"] += 1
    return stats
