"""设定模板 API（2026-09-26 新增：按题材一套一套的单文档模板）。

端点（prefix /setting-templates，main.py 统一拼 /api/v1）：
| 方法 | 路径 | 用途 |
|---|---|---|
| GET    | ``            | 模板列表（genre/q 过滤；列表不含正文） |
| POST   | ``            | 新建模板 |
| GET    | /{id}         | 模板详情（含 content 全文） |
| PUT    | /{id}         | 更新模板 |
| DELETE | /{id}         | 删除模板（确认机制在前端） |

与旧 /settings（单条设定，保留只读）并行；ProjectORM.setting_ids 可同时
引用两类 id，注入端统一兼容。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.response import ok
from app.services import setting_template_crud as svc

router = APIRouter(prefix="/setting-templates", tags=["设定模板（全局）"])


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    genre: str = Field("通用", max_length=40)
    summary: Optional[str] = Field(None, max_length=500)
    content: str = Field(..., min_length=1)
    tags: list[str] = []


class TemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    genre: Optional[str] = Field(None, max_length=40)
    summary: Optional[str] = Field(None, max_length=500)
    content: Optional[str] = None
    tags: Optional[list[str]] = None


def _fields(body) -> dict:
    return {k: v for k, v in body.model_dump().items() if v is not None}


@router.get("", summary="模板列表（不含正文）")
def list_templates(genre: Optional[str] = None, q: Optional[str] = None,
                   db: Session = Depends(get_session)):
    return ok(svc.list_templates(db, genre=genre, q=q))


@router.post("", summary="新建模板")
def create_template(body: TemplateCreate, db: Session = Depends(get_session)):
    try:
        return ok(svc.create_template(
            db, name=body.name, content=body.content, genre=body.genre,
            summary=body.summary, tags=body.tags))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{template_id}", summary="模板详情（含正文）")
def get_template(template_id: str, db: Session = Depends(get_session)):
    t = svc.get_template(db, template_id)
    if t is None:
        raise HTTPException(404, "设定模板不存在")
    return ok(t)


@router.put("/{template_id}", summary="更新模板")
def update_template(template_id: str, body: TemplateUpdate,
                    db: Session = Depends(get_session)):
    try:
        t = svc.update_template(db, template_id, **_fields(body))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if t is None:
        raise HTTPException(404, "设定模板不存在")
    return ok(t)


@router.delete("/{template_id}", summary="删除模板")
def delete_template(template_id: str, db: Session = Depends(get_session)):
    if not svc.delete_template(db, template_id):
        raise HTTPException(404, "设定模板不存在")
    return ok({"deleted": template_id})
