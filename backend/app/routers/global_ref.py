"""全局物品/技能库 API（2026-09-26，前端管理页「物品库/技能库」用）。

端点（prefix /global-ref，main.py 统一拼 /api/v1）：
| 方法 | 路径 | 用途 |
|---|---|---|
| GET  | `/meta` | 类目/题材池常量（前端下拉用） |
| GET  | `/items` | 物品列表（category/q/status/genre 过滤） |
| GET  | `/skills` | 技能列表（同上） |
| POST | `/items` | 新增物品（走 add_item 幂等） |
| POST | `/skills` | 新增技能 |
| PUT  | `/items/{item_id}` | 更新物品（未给字段不动） |
| PUT  | `/skills/{item_id}` | 更新技能 |

纪律：brief ≤50 字由 crud 硬截；不提供 DELETE（下架走 status=disabled，
防误删；normalize_lookup/inject 只认 active，禁用即全链路不可见）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.response import ok
from app.services import global_ref_crud as svc

router = APIRouter(prefix="/global-ref", tags=["物品技能库"])


class EntryCreate(BaseModel):
    kind: str = Field(..., pattern="^(item|skill)$",
                      description="item=物品 / skill=技能")
    name: str = Field(..., min_length=1, max_length=60)
    category: str = Field(..., min_length=1, max_length=20)
    brief: str = Field(..., min_length=1, max_length=200)
    genre: str = Field("通用", max_length=10)
    aliases: list[str] = []
    full_desc: Optional[str] = Field(None, max_length=500,
                                     description="详细描述（管理页展示，可选）")


class EntryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    category: Optional[str] = Field(None, min_length=1, max_length=20)
    brief: Optional[str] = Field(None, max_length=200)
    genre: Optional[str] = Field(None, max_length=10)
    aliases: Optional[list[str]] = None
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")
    reference_only: Optional[bool] = None
    full_desc: Optional[str] = Field(None, max_length=500)


def _fields(body: EntryUpdate) -> dict:
    return {k: v for k, v in body.model_dump().items() if v is not None}


@router.get("/meta", summary="类目/题材池常量")
def get_meta():
    return ok({
        "item_categories": sorted(svc._ITEM_CATEGORIES),   # noqa: SLF001
        "skill_categories": sorted(svc._SKILL_CATEGORIES),  # noqa: SLF001
        "genres": sorted(svc._GENRES),                      # noqa: SLF001
        "max_brief": svc.MAX_BRIEF,
        "statuses": sorted(svc._STATUSES),                  # noqa: SLF001
    })


@router.get("/items", summary="物品列表")
def list_items(category: Optional[str] = None, q: Optional[str] = None,
               status: Optional[str] = None, genre: Optional[str] = None,
               limit: int = 1000, db: Session = Depends(get_session)):
    return ok(svc.list_items(db, category=category, q=q, status=status,
                             genre=genre, limit=limit))


@router.get("/skills", summary="技能列表")
def list_skills(category: Optional[str] = None, q: Optional[str] = None,
                status: Optional[str] = None, genre: Optional[str] = None,
                limit: int = 1000, db: Session = Depends(get_session)):
    return ok(svc.list_skills(db, category=category, q=q, status=status,
                              genre=genre, limit=limit))


@router.post("/items", summary="新增物品（幂等：name+genre 命中即返回已有行）")
def create_item(body: EntryCreate, db: Session = Depends(get_session)):
    if body.kind != "item":
        raise HTTPException(400, "kind 必须为 item")
    try:
        row = svc.add_item(db, name=body.name, category=body.category,
                           brief=body.brief, genre=body.genre,
                           aliases=body.aliases, full_desc=body.full_desc)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return ok(svc._entry_dict(row))  # noqa: SLF001


@router.post("/skills", summary="新增技能（幂等：name+genre 命中即返回已有行）")
def create_skill(body: EntryCreate, db: Session = Depends(get_session)):
    if body.kind != "skill":
        raise HTTPException(400, "kind 必须为 skill")
    try:
        row = svc.add_skill(db, name=body.name, category=body.category,
                            brief=body.brief, genre=body.genre,
                            aliases=body.aliases, full_desc=body.full_desc)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return ok(svc._entry_dict(row))  # noqa: SLF001


@router.put("/items/{item_id}", summary="更新物品")
def update_item(item_id: str, body: EntryUpdate,
                db: Session = Depends(get_session)):
    try:
        row = svc.update_item(db, item_id, **_fields(body))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if row is None:
        raise HTTPException(404, "物品不存在")
    return ok(row)


@router.put("/skills/{item_id}", summary="更新技能")
def update_skill(item_id: str, body: EntryUpdate,
                 db: Session = Depends(get_session)):
    try:
        row = svc.update_skill(db, item_id, **_fields(body))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if row is None:
        raise HTTPException(404, "技能不存在")
    return ok(row)
