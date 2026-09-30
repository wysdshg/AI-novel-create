# -*- coding: utf-8 -*-
"""物品库 CRUD（docs/09 §1.4，阶段 A4 新增）。

谁持有/谁能用 → entity_relation（本表不存引用字段）。
AI 抽取的新物品走 `sync_from_extract`：**标 ai_generated**（M8 分级：长描述允许 AI 生成但可辨识）。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.orm import ItemORM
from app.services import global_ref_crud


def _now():
    return datetime.utcnow()


def list_items(db: Session, project_id: str) -> list[ItemORM]:
    return db.query(ItemORM).filter_by(project_id=project_id).order_by(ItemORM.created_at).all()


def get_item(db: Session, project_id: str, item_id: str) -> ItemORM | None:
    return db.query(ItemORM).filter_by(project_id=project_id, id=item_id).first()


def create_item(db: Session, project_id: str, data: dict) -> ItemORM:
    o = ItemORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        name=data.get("name"),
        category=data.get("category"),
        summary=data.get("summary"),
        full_desc=data.get("full_desc"),
        is_unique=bool(data.get("is_unique") or False),
        tags=data.get("tags") or [],
        status=data.get("status") or "完好",
        ai_generated=bool(data.get("ai_generated") or False),
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def update_item(db: Session, project_id: str, item_id: str, data: dict) -> ItemORM | None:
    o = get_item(db, project_id, item_id)
    if o is None:
        return None
    for k, v in data.items():
        if hasattr(o, k):
            setattr(o, k, v)
    o.updated_at = _now()
    db.commit()
    db.refresh(o)
    return o


def delete_item(db: Session, project_id: str, item_id: str) -> bool:
    from app.services.entity_relation_crud import remove_edges_of
    o = get_item(db, project_id, item_id)
    if o is None:
        return False
    db.delete(o)
    db.commit()
    try:
        remove_edges_of(db, item_id)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(f"[item_crud] 清边失败: {type(e).__name__}: {e}")
    return True


def sync_from_extract(db: Session, project_id: str, new_entities: list | None) -> dict:
    """AI 抽取的 kind=item 新物品 → 落库（幂等，重名跳过；**标 ai_generated**）。

    分级（docs/09 M8）：name/category 必填；summary 由 AI 生成但可辨识（作者可改）。
    已存在的物品**不覆盖**（描述落库即定死——用户 2026-09-18 拍板）。
    E3（docs/03 阶段E）：抽取落库前先查全局条目库（主名/别名），命中「类型惯例词」
    （洗髓丹/灵石/乾坤袋…）即**不建本地条目**——通用词归一指向条目库，
    避免各书重复写描述（描述落库即定死，重复建无法回收）。
    只 add + flush 不 commit（调用方统一提交，照 faction_crud 范式）。
    """
    stats = {"created": 0, "skipped": 0, "normalized": 0}
    for item in new_entities or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip().lower() != "item":
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            stats["skipped"] += 1
            continue
        if global_ref_crud.normalize_lookup(db, name) is not None:
            stats["normalized"] += 1
            continue
        dup = db.query(ItemORM).filter_by(project_id=project_id, name=name).first()
        if dup is not None:
            stats["skipped"] += 1
            continue
        brief = str(item.get("brief") or "").strip()
        now = _now()
        db.add(ItemORM(
            id=uuid.uuid4().hex, project_id=project_id,
            name=name,
            category=str(item.get("category") or "").strip() or None,
            summary=brief or None,
            full_desc=brief or None,
            is_unique=False,
            tags=[],
            status="完好",
            ai_generated=True,
            created_at=now, updated_at=now,
        ))
        db.flush()
        stats["created"] += 1
    return stats
