"""角色模板 CRUD（P-CHARSPLIT 拆表，2026-10-12）—— scale=character 的专用读写层。

拆表背景见 `orm.CharacterTemplateORM`。设计要点：

- **向量索引零重嵌**：沿用原 id，且**存量不主动触发重嵌**——只有 create/update/delete
  时才动向量（与拆表前行为一致）。索引/清块直接复用 `plot_template_crud` 的
  `index_template` 与三路 `remove_source`（本 ORM duck-type 暴露 structure/scale 等）。
- **原文列同步**：update/create 时把展开列现值**回写进 structure.cast[0]**（保留该列
  其他键）——保证"原文列 == 列现值"，块生成（读原文列）与展示（读列）永不漂移。
- 独立门牌 `/character-templates`（用户拍板）：门牌本身防错，角色模板与剧情弧物理分查。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.orm import CharacterTemplateORM
from app.services import plot_template_crud as tpl_crud
from app.services.plot_template_crud import (
    SOURCE_TYPE, SOURCE_TYPE_CAST, SOURCE_TYPE_ARCHETYPE,
)
try:
    from app.services.reference_crud import GLOBAL_PROJECT_ID as GLOBAL
except ImportError:                                   # pragma: no cover
    GLOBAL = "__global__"
import logging

logger = logging.getLogger("char_tpl")

# 展开列 ⇄ 槽位键 的映射（slot_desc ⇄ desc 是唯一的列名差异）
_SLOT_COLS = {
    "slot": "slot", "desc": "slot_desc", "mode": "mode", "slot_kind": "slot_kind",
    "ranks": "ranks", "traits": "traits", "trait_basis": "trait_basis",
    "beats": "beats", "srcs": "srcs", "voice": "voice",
    "behavior_patterns": "behavior_patterns", "relation_patterns": "relation_patterns",
}


def _sync_structure(o: CharacterTemplateORM) -> None:
    """把展开列现值回写进 structure.cast[0]（保留该列其他键）——防原文列与列现值漂移。"""
    st = o.structure if isinstance(o.structure, dict) else {}
    cast = st.get("cast") or [{}]
    slot = cast[0] if cast and isinstance(cast[0], dict) else {}
    cur = o.cast_slot()
    for k, col in _SLOT_COLS.items():
        slot[k] = cur.get(col)
    st["cast"] = [slot]
    # 🔴 deepcopy 语义：整体换新 dict，避免 JSON 原地改不触发变更（04-F2）
    o.structure = st


def _to_dict(t: CharacterTemplateORM) -> dict:
    """序列化。**structure 从列现值合成**（cast[0]=展开列现值，保留原文列其他键）——
    前端（CharTemplateView 消费 structure.cast[0]）与拆表前同构，数据处理零改动。"""
    st = t.structure if isinstance(t.structure, dict) else {}
    st = dict(st)
    st["cast"] = [t.cast_slot()]
    return {
        "id": t.id,
        "name": t.name,
        "scale": "character",
        "genre_tags": t.genre_tags or [],
        "logline": t.logline,
        "structure": st,
        "pitfalls": t.pitfalls or [],
        "rhythm": t.rhythm,
        "source_stats": t.source_stats or {},
        "status": t.status,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def get(db: Session, template_id: str) -> CharacterTemplateORM | None:
    return db.query(CharacterTemplateORM).filter_by(id=template_id).first()


def list_templates(db: Session, *, status: str | None = None) -> list[dict]:
    q = db.query(CharacterTemplateORM)
    if status:
        q = q.filter(CharacterTemplateORM.status == status)
    rows = q.order_by(CharacterTemplateORM.updated_at.desc()).all()
    return [_to_dict(t) for t in rows]


def create(db: Session, data: dict) -> CharacterTemplateORM:
    """从**槽位 dict**（data["cast_slot"]，前端 structure.cast[0] 同构）建行；兼容整包 structure。"""
    slot_in = data.get("cast_slot") or ((data.get("structure") or {}).get("cast") or [{}])[0]
    if not isinstance(slot_in, dict):
        slot_in = {}
    o = CharacterTemplateORM(
        id=uuid.uuid4().hex,
        name=data.get("name") or "未命名角色模板",
        genre_tags=data.get("genre_tags") or [],
        logline=data.get("logline"),
        status=data.get("status") or "draft",
        source_stats=data.get("source_stats") or {},
        pitfalls=data.get("pitfalls") or [],
        rhythm=data.get("rhythm"),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    for k, col in _SLOT_COLS.items():
        setattr(o, col, slot_in.get(k))
    o.structure = data.get("structure") if isinstance(data.get("structure"), dict) else {}
    _sync_structure(o)
    db.add(o)
    db.commit()
    db.refresh(o)
    tpl_crud.index_template(db, o)
    return o


def update(db: Session, template_id: str, data: dict) -> CharacterTemplateORM | None:
    o = get(db, template_id)
    if o is None:
        return None
    for k in ("name", "genre_tags", "logline", "pitfalls", "rhythm", "status", "source_stats"):
        if k in data and data[k] is not None:
            setattr(o, k, data[k])
    slot_in = None
    if "cast_slot" in data and isinstance(data["cast_slot"], dict):
        slot_in = data["cast_slot"]
    elif "structure" in data and isinstance(data["structure"], dict):
        # 前端整包回传 structure 的兼容路：抽 cast[0]
        slot_in = ((data["structure"].get("cast") or [{}])[0]) or {}
    if slot_in:
        for k, col in _SLOT_COLS.items():
            if k in slot_in and slot_in[k] is not None:
                setattr(o, col, slot_in[k])
    o.updated_at = datetime.utcnow()
    _sync_structure(o)          # 列现值 → 原文列，二者永不漂移
    db.commit()
    db.refresh(o)
    tpl_crud.index_template(db, o)   # 结构变了 → 重建向量（与拆表前行为一致）
    return o


def review(db: Session, template_id: str, status: str) -> CharacterTemplateORM | None:
    """B16 同款最小审态：只改状态不动结构。"""
    o = get(db, template_id)
    if o is None:
        return None
    o.status = status
    o.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(o)
    return o


def delete(db: Session, template_id: str) -> bool:
    o = get(db, template_id)
    if o is None:
        return False
    # 三个 source_type 都要清（同 plot_template_crud.delete 的注释——漏清=幽灵槽位参与检索）
    from app.services import vector_index
    vector_index.remove_source(db, GLOBAL, SOURCE_TYPE, template_id)
    vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_CAST, template_id)
    vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_ARCHETYPE, template_id)
    db.delete(o)
    db.commit()
    return True
