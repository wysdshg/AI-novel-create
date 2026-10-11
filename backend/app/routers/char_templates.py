"""角色模板库 API（P-CHARSPLIT 拆表，路径前缀在 main.py 拼 /api/v1）。

独立门牌（用户 2026-10-12 拍板）：scale=character 从 /plot-templates 分家，
**门牌本身防错**——从本路由物理上拿不到剧情弧模板，反之亦然。

端点（与 /plot-templates 同风格）：
| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/character-templates` | 列表（可按 status 过滤） |
| GET | `/character-templates/{tid}` | 详情（structure.cast 同构合成） |
| POST | `/character-templates` | 创建（cast_slot 或 structure.cast[0] 二选一入参） |
| PUT | `/character-templates/{tid}` | 更新（槽位/状态变更自动重建向量） |
| POST | `/character-templates/{tid}/review` | 只改状态（B16 同款） |
| DELETE | `/character-templates/{tid}` | 删除（三路向量连清） |
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional

from app.core.database import get_session
from app.core.response import ok
from app.services import char_template_crud as svc

router = APIRouter(prefix="/character-templates", tags=["角色模板库"])


class CharUpsert(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    genre_tags: list[str] = []
    logline: Optional[str] = None
    status: str = Field("draft", pattern="^(active|draft|reviewed|archived)$")
    # 槽位（structure.cast[0] 同构）；整包 structure 兼容二选一
    cast_slot: Optional[dict] = None
    structure: Optional[dict] = None
    source_stats: dict = {}
    pitfalls: list[str] = []
    rhythm: Optional[str] = None


class ReviewBody(BaseModel):
    status: str = Field(..., pattern="^(active|draft|reviewed|archived)$")


@router.get("", summary="列出角色模板")
def list_templates(status: Optional[str] = None, db: Session = Depends(get_session)):
    return ok(svc.list_templates(db, status=status))


@router.get("/{tid}", summary="角色模板详情")
def get_template(tid: str, db: Session = Depends(get_session)):
    o = svc.get(db, tid)
    if o is None:
        raise HTTPException(404, "角色模板不存在")
    return ok(svc._to_dict(o))


@router.post("", summary="创建角色模板")
def create_template(body: CharUpsert, db: Session = Depends(get_session)):
    return ok(svc._to_dict(svc.create(db, body.model_dump())))


@router.put("/{tid}", summary="更新角色模板")
def update_template(tid: str, body: CharUpsert, db: Session = Depends(get_session)):
    o = svc.update(db, tid, body.model_dump())
    if o is None:
        raise HTTPException(404, "角色模板不存在")
    return ok(svc._to_dict(o))


@router.post("/{tid}/review", summary="只改状态")
def review_template(tid: str, body: ReviewBody, db: Session = Depends(get_session)):
    o = svc.review(db, tid, body.status)
    if o is None:
        raise HTTPException(404, "角色模板不存在")
    return ok(svc._to_dict(o))


@router.delete("/{tid}", summary="删除角色模板（三路向量连清）")
def delete_template(tid: str, db: Session = Depends(get_session)):
    if not svc.delete(db, tid):
        raise HTTPException(404, "角色模板不存在")
    return ok({"deleted": tid})
