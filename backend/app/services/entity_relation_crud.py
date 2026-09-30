# -*- coding: utf-8 -*-
"""通用关系层（docs/09 §2）：entity_relations CRUD + 字典种子 + 旧数据迁移。

A1 范围（2026-09-18）：表结构 + 幂等迁移 + 查询。双写过渡（A2）在相关 crud 里接入。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.orm import (
    EntityRelationORM, FactionORM, RelationTypeORM, SkillORM,
)

# ---------------------------------------------------------------------------
# 关系类型字典（种子）
# ---------------------------------------------------------------------------
SEED_RELATION_TYPES = [
    # name, symmetric, inverse, weight, applies_to, note
    ("师徒", False, "弟子", 88, [["character", "character"]], None),
    ("弟子", False, "师徒", 85, [["character", "character"]], None),
    ("亲属", True, None, 90, [["character", "character"]], "父子/兄弟/夫妻等具体称谓建议放 note 或 meta"),
    ("仇敌", True, None, 88, [["character", "character"]], None),
    ("盟友", True, None, 62, [["character", "character"], ["faction", "faction"]], None),
    ("同门", True, None, 58, [["character", "character"]], None),
    ("掌握技能", False, "被掌握", 76, [["character", "skill"]], "meta.proficiency = 熟练度"),
    ("被掌握", False, "掌握技能", 40, [["skill", "character"]], None),
    ("持有物品", False, "被持有", 72, [["character", "item"], ["faction", "item"]], "meta.quantity / holding"),
    ("被持有", False, "持有物品", 40, [["item", "character"], ["item", "faction"]], None),
    ("隶属势力", False, "辖有成员", 70, [["character", "faction"]], None),
    ("辖有成员", False, "隶属势力", 45, [["faction", "character"]], None),
    ("领袖", False, "效力门派", 82, [["character", "faction"]], "宗主/家主/会长"),
    ("效力门派", False, "领袖", 50, [["faction", "character"]], None),
    ("附属势力", False, "母势力", 66, [["faction", "faction"]], None),
    ("母势力", False, "附属势力", 40, [["faction", "faction"]], None),
    ("位于", False, "辖区内", 55, [["character", "location"], ["faction", "location"]], None),
    ("需要物品", False, "用于", 60, [["skill", "item"], ["character", "item"]], "修炼青云诀需服用青云丹"),
    ("用于", False, "需要物品", 40, [["item", "skill"], ["item", "character"]], None),
    ("镇宗之物", False, None, 78, [["item", "faction"], ["faction", "item"]], "青云宗镇宗之剑"),
]


def seed_relation_types(db: Session) -> int:
    """补齐缺失的关系类型（幂等）。返回新增条数。"""
    existing = {r.name for r in db.query(RelationTypeORM).all()}
    added = 0
    for name, sym, inv, w, applies, note in SEED_RELATION_TYPES:
        if name in existing:
            continue
        db.add(RelationTypeORM(name=name, symmetric=sym, inverse=inv,
                               weight=w, applies_to=applies, note=note))
        added += 1
    if added:
        db.commit()
    return added


def upsert_edge(db: Session, project_id: str, a_id: str, a_type: str,
                b_id: str, b_type: str, relation_type: str,
                meta: dict | None = None, note: str | None = None,
                commit: bool = True) -> tuple[EntityRelationORM, bool]:
    """加一条边（幂等）：同 (a_id,b_id,relation_type) 已存在 → 更新 meta/note。返回 (边, 是否新建)。

    `commit=False`：嵌入调用方事务（如摄取流程——中途 commit 会固化半成品，照 relation_crud 范式）。
    """
    row = (db.query(EntityRelationORM)
           .filter_by(a_id=a_id, b_id=b_id, relation_type=relation_type).first())
    if row is not None:
        changed = False
        if meta is not None and meta != row.meta:
            row.meta = meta
            changed = True
        if note is not None and note != row.note:
            row.note = note
            changed = True
        if changed and commit:
            db.commit()
            db.refresh(row)
        return row, False
    row = EntityRelationORM(
        id=uuid.uuid4().hex, project_id=project_id,
        a_id=a_id, a_type=a_type, b_id=b_id, b_type=b_type,
        relation_type=relation_type, meta=meta, note=note,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    return row, True


def remove_edge(db: Session, project_id: str, a_id: str, b_id: str, relation_type: str) -> int:
    """精确删除一条边（关系删除/改类型时同步用）。"""
    n = (db.query(EntityRelationORM)
         .filter_by(a_id=a_id, b_id=b_id, relation_type=relation_type)
         .delete(synchronize_session=False))
    return n


def sync_faction_edges(db, o: FactionORM, commit: bool = True) -> None:
    """按 faction 当前 leader_id/members 重写 领袖/隶属 边（先删后建，幂等）。"""
    db.query(EntityRelationORM).filter_by(b_id=o.id, relation_type="领袖") \
        .delete(synchronize_session=False)
    db.query(EntityRelationORM).filter_by(b_id=o.id, relation_type="隶属势力") \
        .delete(synchronize_session=False)
    if o.leader_id:
        upsert_edge(db, o.project_id, o.leader_id, "character", o.id, "faction",
                    "领袖", commit=False)
    for mid in (o.members or []):
        if str(mid).strip():
            upsert_edge(db, o.project_id, str(mid), "character", o.id, "faction",
                        "隶属势力", commit=False)
    if commit:
        db.commit()


def neighbors(db: Session, project_id: str, entity_id: str) -> list[EntityRelationORM]:
    """一跳：与该实体相关的所有边（a 或 b）。"""
    return (db.query(EntityRelationORM)
            .filter_by(project_id=project_id)
            .filter((EntityRelationORM.a_id == entity_id) | (EntityRelationORM.b_id == entity_id))
            .all())


def remove_edges_of(db: Session, entity_id: str) -> int:
    """实体删除时清其所有边（孤儿纪律）。"""
    n = (db.query(EntityRelationORM)
         .filter((EntityRelationORM.a_id == entity_id) | (EntityRelationORM.b_id == entity_id))
         .delete(synchronize_session=False))
    db.commit()
    return n


# ---------------------------------------------------------------------------
# 旧数据迁移（M2，幂等）
# ---------------------------------------------------------------------------
def migrate_legacy(db: Session, project_id: str | None = None) -> dict:
    """把旧引用迁成 entity_relations 边（幂等，可反复跑）：

    · relations（角色-角色）→ character—character 边（保留 relation_type 与 strength→meta）
    · skills.owner_id       → character—[掌握技能]—skill
    · factions.leader_id    → character—[领袖]—faction
    · factions.members      → character—[隶属势力]—faction（逐个 id）
    """
    seed_relation_types(db)
    q = db.query(FactionORM)
    if project_id:
        q = q.filter_by(project_id=project_id)
    stats = {"relations": 0, "skills": 0, "leader": 0, "members": 0, "skipped": 0}

    # 1) 角色-角色
    from app.models.orm import RelationORM
    rq = db.query(RelationORM)
    if project_id:
        rq = rq.filter_by(project_id=project_id)
    for rel in rq.all():
        if not rel.subject_id or not rel.object_id:
            stats["skipped"] += 1
            continue
        meta = {"strength": rel.strength} if getattr(rel, "strength", None) is not None else None
        _, created = upsert_edge(db, rel.project_id, rel.subject_id, "character",
                                 rel.object_id, "character", rel.relation_type or "同门",
                                 meta=meta, note=(rel.note or None) if hasattr(rel, "note") else None)
        stats["relations"] += 1 if created else 0

    # 2) 技能归属
    sq = db.query(SkillORM)
    if project_id:
        sq = sq.filter_by(project_id=project_id)
    for sk in sq.all():
        if not sk.owner_id:
            stats["skipped"] += 1
            continue
        _, created = upsert_edge(db, sk.project_id, sk.owner_id, "character",
                                 sk.id, "skill", "掌握技能")
        stats["skills"] += 1 if created else 0

    # 3) 势力领袖 + 成员
    fq = db.query(FactionORM)
    if project_id:
        fq = fq.filter_by(project_id=project_id)
    for fa in fq.all():
        if fa.leader_id:
            _, created = upsert_edge(db, fa.project_id, fa.leader_id, "character",
                                     fa.id, "faction", "领袖")
            stats["leader"] += 1 if created else 0
        for mid in (fa.members or []):
            if not str(mid).strip():
                continue
            _, created = upsert_edge(db, fa.project_id, str(mid), "character",
                                     fa.id, "faction", "隶属势力")
            stats["members"] += 1 if created else 0

    db.commit()
    return stats
