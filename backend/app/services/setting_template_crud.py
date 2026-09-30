"""设定模板 CRUD（2026-09-26：设定库从「单条设定」改为「按题材一套一套」）。

一套模板 = 一个 Markdown 单文档（content），内含该题材全部世界观分节：
境界 / 货币 / 体系 / 规则 / 物价。供新建小说按题材挑选、注入时整篇取用。
旧 SettingORM（settings 表）保留只读、不再供挑选；两者 id 同一 UUID 空间。
"""
import uuid
from datetime import datetime

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.models.orm import SettingTemplateORM


def _now() -> datetime:
    return datetime.utcnow()


def _norm_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    if len(name) > 120:
        raise ValueError("name 超过 120 字")
    return name


def _to_dict(o: SettingTemplateORM, *, with_content: bool = True) -> dict:
    d = {
        "id": o.id,
        "name": o.name,
        "genre": o.genre or "通用",
        "summary": o.summary,
        "tags": o.tags or [],
        "content_chars": len(o.content or ""),
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }
    if with_content:
        d["content"] = o.content or ""
    return d


def list_templates(db: Session, *, genre: str | None = None,
                   q: str | None = None, limit: int = 200) -> list[dict]:
    """模板列表（默认不带 content 正文，避免列表页拉全文档）。"""
    qy = db.query(SettingTemplateORM)
    if genre:
        qy = qy.filter(SettingTemplateORM.genre == genre)
    if q:
        like = f"%{q.strip()}%"
        qy = qy.filter(sa_func.coalesce(SettingTemplateORM.name, "").like(like)
                       | sa_func.coalesce(SettingTemplateORM.summary, "").like(like))
    rows = (qy.order_by(SettingTemplateORM.genre, SettingTemplateORM.name)
            .limit(max(1, min(limit, 500))).all())
    return [_to_dict(o, with_content=False) for o in rows]


def get_template(db: Session, template_id: str) -> dict | None:
    o = db.get(SettingTemplateORM, template_id)
    return _to_dict(o) if o else None


def create_template(db: Session, *, name: str, content: str,
                    genre: str = "通用", summary: str | None = None,
                    tags: list[str] | None = None) -> dict:
    o = SettingTemplateORM(
        id=uuid.uuid4().hex, name=_norm_name(name),
        genre=(genre or "通用").strip() or "通用",
        summary=(summary or "").strip() or None,
        content=(content or "").strip(),
        tags=[t.strip() for t in (tags or []) if t and t.strip()],
        created_at=_now(), updated_at=_now())
    if not o.content:
        raise ValueError("content 不能为空")
    db.add(o)
    db.commit()
    return _to_dict(o)


def update_template(db: Session, template_id: str, *, name=None, genre=None,
                    summary=None, content=None, tags=None) -> dict | None:
    o = db.get(SettingTemplateORM, template_id)
    if o is None:
        return None
    if name is not None:
        o.name = _norm_name(name)
    if genre is not None:
        o.genre = (genre or "").strip() or "通用"
    if summary is not None:
        o.summary = (summary or "").strip() or None
    if content is not None:
        if not (content or "").strip():
            raise ValueError("content 不能为空")
        o.content = content.strip()
    if tags is not None:
        o.tags = [t.strip() for t in tags if t and t.strip()]
    o.updated_at = _now()
    db.commit()
    return _to_dict(o)


def delete_template(db: Session, template_id: str) -> bool:
    o = db.get(SettingTemplateORM, template_id)
    if o is None:
        return False
    db.delete(o)
    db.commit()
    return True
