# -*- coding: utf-8 -*-
"""S3（03 §8.7）：角色修订表 CRUD —— 角色的**版本历史 + AI 改动审批 + 回滚**。

两档分工（治漂移的核心，2026-09-17 定稿）：
- **客观项**（出场章、互动关系、势力归属）→ 继续由既有的自动更新链处理（不进本表）；
- **主观项**（性格/信念/地位）→ AI 只能提 **pending** 修订（本模块 `propose`），
  作者确认（`decide approve`）才真正回写角色卡；回滚（`rollback`）也留痕。

快照字段：`SNAPSHOT_FIELDS` = 角色卡的**设定字段**（主观+设定），刻意**不含**
`last_seen_chapter / appearance_count`（客观派生项，自动更新、与版本无关）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.orm import CharacterORM, CharacterRevisionORM

logger = logging.getLogger(__name__)

SNAPSHOT_FIELDS = ("name", "role_type", "gender", "age", "personality",
                   "background", "talent", "current_level", "skills",
                   "relationship_network", "brief")

SOURCE_MANUAL = "manual"
SOURCE_AI = "ai_extract"
SOURCE_ROLLBACK = "rollback"


def snapshot_of(o: CharacterORM) -> dict:
    """把角色卡拍成快照（只取设定字段）。"""
    out = {}
    for f in SNAPSHOT_FIELDS:
        v = getattr(o, f, None)
        out[f] = v
    return out


def _orm_to_dict(r: CharacterRevisionORM) -> dict:
    return {
        "id": r.id, "character_id": r.character_id, "snapshot": r.snapshot,
        "source": r.source, "status": r.status, "chapter_no": r.chapter_no,
        "note": r.note, "created_at": r.created_at, "decided_at": r.decided_at,
    }


def _diff(a: dict, b: dict) -> dict:
    """浅 diff：返回 {字段: (旧, 新)}（只列有变化的）。"""
    out = {}
    for f in SNAPSHOT_FIELDS:
        if (a.get(f) or None) != (b.get(f) or None):
            out[f] = [a.get(f), b.get(f)]
    return out


def record(db: Session, project_id: str, character_id: str, *, source: str,
           status: str, chapter_no: int | None = None, note: str | None = None,
           snapshot: dict | None = None) -> CharacterRevisionORM | None:
    """写一条修订记录。`snapshot` 缺省 = 对当前角色卡拍照。角色不存在返回 None。"""
    o = db.query(CharacterORM).filter_by(project_id=project_id, id=character_id).first()
    if o is None:
        return None
    snap = snapshot or snapshot_of(o)
    r = CharacterRevisionORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        character_id=character_id,
        snapshot=snap,
        source=source,
        status=status,
        chapter_no=chapter_no,
        note=note,
        created_at=datetime.utcnow(),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def on_character_created(db: Session, o: CharacterORM) -> None:
    """建卡 → v1（approved，manual）。调用方 commit 后再调（快照要读最终态）。"""
    try:
        record(db, o.project_id, o.id, source=SOURCE_MANUAL, status="approved",
               note="建卡初版")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning(f"[char_rev] 建卡初版记录失败（不影响建卡）: {type(e).__name__}: {e}")


def on_character_updated(db: Session, project_id: str, character_id: str) -> None:
    """作者改卡 → 记一版（approved，manual）。与上一版**完全相同则跳过**（防噪声）。"""
    try:
        o = db.query(CharacterORM).filter_by(project_id=project_id, id=character_id).first()
        if o is None:
            return
        snap = snapshot_of(o)
        last = (db.query(CharacterRevisionORM)
                .filter_by(character_id=character_id, status="approved")
                .order_by(CharacterRevisionORM.created_at.desc()).first())
        if last is not None and last.snapshot == snap:
            return
        record(db, project_id, character_id, source=SOURCE_MANUAL,
               status="approved", snapshot=snap, note="作者修改")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning(f"[char_rev] 改卡记录失败（不影响改卡）: {type(e).__name__}: {e}")


def propose(db: Session, project_id: str, character_id: str, changes: dict, *,
            chapter_no: int | None = None, note: str | None = None) -> dict | None:
    """**AI 改动入口（S3 核心）**：对主观项提出修订 → 只写 **pending**，绝不覆盖角色卡。

    `changes` 只接受 `SNAPSHOT_FIELDS` 里的键（多余的忽略）；
    快照 = 当前卡 + changes（作者在审批页能看到"改完长什么样"）。
    返回 pending 修订的 dict；角色不存在返回 None。
    """
    o = db.query(CharacterORM).filter_by(project_id=project_id, id=character_id).first()
    if o is None:
        return None
    cur = snapshot_of(o)
    safe = {k: v for k, v in (changes or {}).items() if k in SNAPSHOT_FIELDS}
    if not safe:
        return _orm_to_dict_empty(project_id, character_id)
    proposed = {**cur, **safe}
    r = CharacterRevisionORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        character_id=character_id,
        snapshot=proposed,
        changed_fields=list(safe.keys()),   # 记住"打算改哪些"→ 采纳时只动这些字段
        source=SOURCE_AI,
        status="pending",
        chapter_no=chapter_no,
        note=note,
        created_at=datetime.utcnow(),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    d = _orm_to_dict(r)
    d["diff"] = _diff(cur, proposed)
    return d


def _orm_to_dict_empty(project_id: str, character_id: str) -> dict:
    return {"id": None, "project_id": project_id, "character_id": character_id,
            "empty": True, "diff": {}}


def list_revisions(db: Session, project_id: str, character_id: str) -> list[dict]:
    """角色详情页的历史版本列表（新→旧），每条带与**当前卡**的 diff。"""
    o = db.query(CharacterORM).filter_by(project_id=project_id, id=character_id).first()
    cur = snapshot_of(o) if o else {}
    rows = (db.query(CharacterRevisionORM)
            .filter_by(project_id=project_id, character_id=character_id)
            .order_by(CharacterRevisionORM.created_at.desc()).all())
    out = []
    for r in rows:
        d = _orm_to_dict(r)
        d["diff_vs_current"] = _diff(cur, r.snapshot)
        out.append(d)
    return out


def _intended_fields(db: Session, r: CharacterRevisionORM) -> list[str] | None:
    """这次修订**打算改的字段**。

    · 新行：`changed_fields`（propose 时记的）；
    · 老行（无该列值）：拿「提案时点的上一个已生效版本」当基线反推差异 —— 这样 2026-09-17 之前
      产生的 pending 修订也能被正确采纳（不会用整份旧快照擦掉别的字段）。
    返回 None 表示**无从判断** → 调用方退回「整份快照应用」。
    """
    cf = getattr(r, "changed_fields", None)
    if cf:
        return list(cf)
    base = (db.query(CharacterRevisionORM)
            .filter(CharacterRevisionORM.character_id == r.character_id,
                    CharacterRevisionORM.status == "approved",
                    CharacterRevisionORM.created_at <= r.created_at)
            .order_by(CharacterRevisionORM.created_at.desc()).first())
    if base is None:
        return None
    b, s = base.snapshot or {}, r.snapshot or {}
    return [f for f in SNAPSHOT_FIELDS if (b.get(f) or None) != (s.get(f) or None)]


def decide(db: Session, project_id: str, revision_id: str, approve: bool) -> dict | None:
    """审批一条修订：approve = 回写角色卡；reject = 仅标记。

    🔴 （2026-09-17 修）approve **只应用该修订打算改的字段**（`_intended_fields`），
    不再整份快照回写 —— 否则旧快照会把后来批准的其它字段擦回旧值
    （实测：采纳「第6章·性格」把刚采纳的「第4章·境界」擦空）。
    """
    r = db.query(CharacterRevisionORM).filter_by(project_id=project_id, id=revision_id).first()
    if r is None:
        return None
    r.status = "approved" if approve else "rejected"
    r.decided_at = datetime.utcnow()
    if approve:
        o = db.query(CharacterORM).filter_by(project_id=project_id, id=r.character_id).first()
        if o is not None:
            snap = r.snapshot or {}
            fields = _intended_fields(db, r)
            targets = fields if fields is not None else list(SNAPSHOT_FIELDS)
            applied = []
            for f in targets:
                if f in snap and f != "name":     # name 是身份主键语义，审批改卡不改名
                    if (getattr(o, f, None) or None) != (snap[f] or None):
                        setattr(o, f, snap[f])
                        applied.append(f)
            r.note = (r.note or "") + (f"｜已应用: {','.join(applied)}" if applied else "｜无字段变化")
            o.updated_at = datetime.utcnow()
    db.commit()
    d = _orm_to_dict(r)
    d["applied"] = bool(approve)
    return d


def rollback(db: Session, project_id: str, character_id: str, revision_id: str) -> dict | None:
    """一键回滚到指定版本：把该版快照回写角色卡，并**写一条 rollback 留痕**（approved）。"""
    r = (db.query(CharacterRevisionORM)
         .filter_by(project_id=project_id, id=revision_id, character_id=character_id).first())
    if r is None or not r.snapshot:
        return None
    new = record(db, project_id, character_id, source=SOURCE_ROLLBACK, status="approved",
                 snapshot=r.snapshot,
                 note=f"回滚到 {r.created_at:%m-%d %H:%M} 的版本")
    o = db.query(CharacterORM).filter_by(project_id=project_id, id=character_id).first()
    if o is not None:
        for f in SNAPSHOT_FIELDS:
            if f in (r.snapshot or {}) and f != "name":
                setattr(o, f, r.snapshot[f])
        o.updated_at = datetime.utcnow()
        db.commit()
    d = _orm_to_dict(new) if new else {"rolled_back": True}
    d["rolled_back_to"] = revision_id
    return d


def delete_all_for_character(db: Session, character_id: str) -> int:
    """删角色时的级联清理（A5 教训：不许留孤儿）。"""
    n = (db.query(CharacterRevisionORM)
         .filter_by(character_id=character_id).delete())
    return n
