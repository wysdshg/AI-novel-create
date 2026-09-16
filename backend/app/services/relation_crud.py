"""关系网 CRUD 服务（模块1：分作品资料库）。

分作品隔离通过 project_id 实现：所有查询/写入均按 project_id 过滤。
关系是有向边：subject_id -> object_id，携带 relation_type / strength / note。
此前 routers/database.py 中 relations 端点是占位桩，这里落地真实持久化。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.orm import CharacterORM, RelationORM
from app.schemas.database import Relation, RelationCreate, RelationUpdate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_schema(o: RelationORM) -> Relation:
    return Relation(
        id=o.id,
        subject_id=o.subject_id,
        object_id=o.object_id,
        relation_type=o.relation_type,
        strength=o.strength,
        note=o.note,
    )


def list_relations(
    db: Session, project_id: str, character_id: str | None = None
) -> list[Relation]:
    q = db.query(RelationORM).filter_by(project_id=project_id)
    if character_id:
        q = q.filter(
            (RelationORM.subject_id == character_id)
            | (RelationORM.object_id == character_id)
        )
    return [_to_schema(r) for r in q.all()]


def create_relation(db: Session, project_id: str, data: RelationCreate) -> Relation:
    o = RelationORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        subject_id=data.subject_id,
        object_id=data.object_id,
        relation_type=data.relation_type,
        strength=data.strength,
        note=data.note,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return _to_schema(o)


def get_relation(db: Session, project_id: str, relation_id: str):
    return (
        db.query(RelationORM)
        .filter_by(project_id=project_id, id=relation_id)
        .first()
    )


def update_relation(
    db: Session, project_id: str, relation_id: str, data: RelationUpdate
) -> Relation | None:
    o = get_relation(db, project_id, relation_id)
    if o is None:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(o, field, value)
    db.commit()
    db.refresh(o)
    return _to_schema(o)


def delete_relation(db: Session, project_id: str, relation_id: str) -> bool:
    o = get_relation(db, project_id, relation_id)
    if o is None:
        return False
    db.delete(o)
    db.commit()
    return True


def sync_from_extract(
    db: Session, project_id: str, relations: list | None,
    chapter_characters: list | None, chapter_no: int,
) -> dict:
    """把一章抽取到的关系自动回注到 relations 表（任务③批次2，幂等）。

    双重防线防幻觉/脏边（抽取 prompt 只约束了第一层，这里是第二层）：
      1. subject/object 必须出现在本章 characters 里——本章没同框的两人关系 = 幻觉；
      2. 名字必须能解析到库内角色——RelationORM 两端存角色 id，解析不到（如新配角
         尚未人工确认入库）先跳过，等实体入库后重摄取即可补上。

    幂等：project + subject + object + type 四元组查重。已存在则把 note 覆盖为
    最近同现章号（同一对人同一种称呼再次出现 = 关系延续，留最近痕迹即可）；
    称呼变了（如未婚妻→宿敌）会落一条新边，旧边保留——关系演变历史有价值。
    统计类（strength 打分）绝不交给 LLM，统一 default=50。

    注意：只 add + flush，不 commit——commit 由调用方（摄取流程/请求边界）统一做，
    中途 commit 会把半成品固化（照 foreshadow_crud.sync_from_actions 范式）。
    返回统计 {created, updated, skipped}。
    """
    stats = {"created": 0, "updated": 0, "skipped": 0}
    relations = relations or []
    if not chapter_characters:
        return stats
    chapter_names = {str(x).strip() for x in chapter_characters if str(x).strip()}

    # 库内角色名 → id。摄取是低频后台任务，项目角色量级百人，全量查可接受。
    # 只做精确匹配（docs/04 C15：模糊匹配有长度门槛坑，不碰）。
    name_map = {
        (c.name or "").strip(): c.id
        for c in db.query(CharacterORM).filter_by(project_id=project_id).all()
        if (c.name or "").strip()
    }

    for item in relations:
        if not isinstance(item, dict):
            stats["skipped"] += 1
            continue
        subj = str(item.get("subject") or "").strip()
        obj = str(item.get("object") or "").strip()
        rtype = str(item.get("type") or item.get("relation_type") or "").strip()[:20]
        if not subj or not obj or not rtype:
            stats["skipped"] += 1
            continue
        if subj not in chapter_names or obj not in chapter_names:
            stats["skipped"] += 1  # 防线1：本章没出场的人之间的关系，视为幻觉
            continue
        subj_id, obj_id = name_map.get(subj), name_map.get(obj)
        if not subj_id or not obj_id:
            stats["skipped"] += 1  # 防线2：库内解析不到（角色还没入库）
            continue
        if subj_id == obj_id:
            stats["skipped"] += 1  # 自指边（A对A的称呼）是模型犯傻形态
            continue

        note = f"第{chapter_no}章互动"
        dup = db.query(RelationORM).filter_by(
            project_id=project_id, subject_id=subj_id, object_id=obj_id,
            relation_type=rtype,
        ).first()
        if dup is not None:
            dup.note = note
            stats["updated"] += 1
            continue
        db.add(RelationORM(
            id=uuid.uuid4().hex, project_id=project_id,
            subject_id=subj_id, object_id=obj_id,
            relation_type=rtype, strength=50, note=note,
        ))
        db.flush()  # session autoflush=False：不 flush 则同批后续查重看不到刚加的行
        stats["created"] += 1
    return stats
