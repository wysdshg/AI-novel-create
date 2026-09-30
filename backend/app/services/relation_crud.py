"""关系网 CRUD 服务（模块1：分作品资料库）。

分作品隔离通过 project_id 实现：所有查询/写入均按 project_id 过滤。
关系是有向边：subject_id -> object_id，携带 relation_type / strength / note。

2026-10-01（A6 遗留清理）：存储层从旧表 relations 迁到新表 entity_relations
（A2 双写期结束，新表成单一事实源）。API 契约不变：
- subject_id/object_id ↔ 新表 a_id/b_id（a_type=b_type="character"）
- strength ↔ 新表 meta["strength"]
- 旧表 relations 停止读写（表与历史数据保留，仅代码退役）
"""
import logging

logger = logging.getLogger(__name__)

from sqlalchemy.orm import Session

from app.models.orm import CharacterORM, EntityRelationORM
from app.services import entity_relation_crud as er
from app.schemas.database import Relation, RelationCreate, RelationUpdate


def _to_schema(e: EntityRelationORM) -> Relation:
    return Relation(
        id=e.id,
        subject_id=e.a_id,
        object_id=e.b_id,
        relation_type=e.relation_type,
        strength=(e.meta or {}).get("strength"),
        note=e.note,
    )


def _char_edges(db: Session, project_id: str):
    return (
        db.query(EntityRelationORM)
        .filter_by(project_id=project_id, a_type="character", b_type="character")
    )


def list_relations(
    db: Session, project_id: str, character_id: str | None = None
) -> list[Relation]:
    q = _char_edges(db, project_id)
    if character_id:
        q = q.filter(
            (EntityRelationORM.a_id == character_id)
            | (EntityRelationORM.b_id == character_id)
        )
    return [_to_schema(e) for e in q.all()]


def create_relation(db: Session, project_id: str, data: RelationCreate) -> Relation:
    meta = {"strength": data.strength} if data.strength is not None else None
    e, _created = er.upsert_edge(
        db, project_id, data.subject_id, "character", data.object_id, "character",
        data.relation_type, meta=meta, note=data.note,
    )
    return _to_schema(e)


def get_relation(db: Session, project_id: str, relation_id: str):
    return (
        db.query(EntityRelationORM)
        .filter_by(project_id=project_id, id=relation_id)
        .first()
    )


def update_relation(
    db: Session, project_id: str, relation_id: str, data: RelationUpdate
) -> Relation | None:
    e = get_relation(db, project_id, relation_id)
    if e is None:
        return None
    # 原对象改字段：id 保持稳定（前端 PUT 语义），同 (a,b,type) 唯一约束兜底防重
    patch = data.model_dump(exclude_unset=True)
    if "subject_id" in patch:
        e.a_id = patch["subject_id"]
    if "object_id" in patch:
        e.b_id = patch["object_id"]
    if "relation_type" in patch:
        e.relation_type = patch["relation_type"]
    if "note" in patch:
        e.note = patch["note"]
    if "strength" in patch:
        e.meta = {**(e.meta or {}), **({"strength": patch["strength"]})} \
            if patch["strength"] is not None else \
            {k: v for k, v in (e.meta or {}).items() if k != "strength"}
    db.commit()
    db.refresh(e)
    return _to_schema(e)


def delete_relation(db: Session, project_id: str, relation_id: str) -> bool:
    e = get_relation(db, project_id, relation_id)
    if e is None:
        return False
    db.delete(e)
    db.commit()
    return True


def sync_from_extract(
    db: Session, project_id: str, relations: list | None,
    chapter_characters: list | None, chapter_no: int,
) -> dict:
    """把一章抽取到的关系自动回注到关系边表（任务③批次2，幂等）。

    双重防线防幻觉/脏边（抽取 prompt 只约束了第一层，这里是第二层）：
      1. subject/object 必须出现在本章 characters 里——本章没同框的两人关系 = 幻觉；
      2. 名字必须能解析到库内角色——解析不到（如新配角尚未人工确认入库）先跳过，
         等实体入库后重摄取即可补上。

    幂等：upsert_edge 按 project + a_id + b_id + type 四元组去重，已存在则把 note
    覆盖为最近同现章号（同一对人同一种称呼再次出现 = 关系延续）；称呼变了
    （如未婚妻→宿敌）会落一条新边，旧边保留——关系演变历史有价值。
    统计类（strength 打分）绝不交给 LLM，统一 default=50。

    注意：只 add + flush，不 commit——commit 由调用方（摄取流程/请求边界）统一做。
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
        _row, created = er.upsert_edge(
            db, project_id, subj_id, "character", obj_id, "character",
            rtype, meta={"strength": 50}, note=note, commit=False,
        )
        if created:
            stats["created"] += 1
        else:
            stats["updated"] += 1
    return stats
