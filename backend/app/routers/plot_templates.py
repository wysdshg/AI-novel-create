"""情节模板库 API（Phase 7.1，路径前缀在 main.py 拼 /api/v1）。

端点：
| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/plot-templates` | 列表（可按 scale/status 过滤） |
| GET | `/plot-templates/{tid}` | 详情（含完整 structure） |
| POST | `/plot-templates` | 创建（自动 beat 级向量化） |
| PUT | `/plot-templates/{tid}` | 更新（结构变更自动重建向量） |
| DELETE | `/plot-templates/{tid}` | 删除（连向量一起清） |
| POST | `/plot-templates/search` | **检索主入口**：`{query, queries?, scale?, tags?, top_k?}` |

检索说明：
- `query` 一句模糊口述即可（"既像学院大比又像秘境寻宝"）；
- `queries` 显式多查询（`["学院大比","秘境寻宝"]`）走 multi-query RRF 融合；
- 向量不可用（未配检索 Key）自动回退关键词匹配，`mode=fallback_tags` 标明；
- 返回模板 + `matched_beats`（命中的节拍及其 variants）—— 卡文场景直接看走法。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional

from app.core.database import get_session
from app.core.response import ok
from app.services import plot_template_crud as svc

router = APIRouter(prefix="/plot-templates", tags=["情节模板库"])


class TemplateUpsert(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    # character（F8 引入的角色模板）与骨架同表同端点：白名单漏了它，模板库页筛出的
    # 角色模板在详情抽屉改状态保存就会 422（DEV-F9c）。
    scale: str = Field("arc", pattern="^(arc|segment|character)$")
    genre_tags: list[str] = []
    logline: Optional[str] = None
    structure: dict = {}
    pitfalls: list[str] = []
    rhythm: Optional[str] = None
    source_stats: dict = {}
    # active（现役）是 F8 换血后的主力状态，DEV-F9b 补进白名单：漏了会让前端把现役模板
    # 存回 active 时 422（详情抽屉的状态下拉正是这个原因）。
    status: str = Field("draft", pattern="^(active|draft|reviewed|archived)$")


class TemplateSearch(BaseModel):
    query: str = Field("", description="一句模糊口述；与 queries 二选一或同时给")
    queries: Optional[list[str]] = Field(None, description="显式多查询（各查一路 RRF 融合）")
    scale: Optional[str] = Field(None, pattern="^(arc|segment|character)$")
    tags: Optional[list[str]] = None
    top_k: int = Field(8, ge=1, le=20)
    # DEV-P3a ①：精确键优先匹配链开关。默认开；关掉 = 纯向量序（对照/排障用）。
    # 🔴 这个字段**必须在这里声明**：Pydantic 会静默丢弃未声明字段，曾导致请求体里的
    #    key_match=false 被丢掉、before/after 对照测不出差别。
    key_match: bool = Field(True, description="按 (大类,子事件) 精确键优先重排；false=纯向量序")


@router.get("", summary="列出模板")
def list_templates(scale: Optional[str] = None, status: Optional[str] = None,
                   db: Session = Depends(get_session)):
    return ok(svc.list_templates(db, scale=scale, status=status))


@router.post("/search", summary="检索模板（模糊口述 / 多查询 RRF / 精确键优先）")
def search_templates(body: TemplateSearch, db: Session = Depends(get_session)):
    return ok(svc.search(db, query=body.query, queries=body.queries,
                         scale=body.scale, tags=body.tags, top_k=body.top_k,
                         key_match=body.key_match))


@router.get("/{template_id}", summary="模板详情")
def get_template(template_id: str, db: Session = Depends(get_session)):
    o = svc.get(db, template_id)
    if o is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return ok(svc._to_dict(o))


@router.post("", summary="创建模板")
def create_template(body: TemplateUpsert, db: Session = Depends(get_session)):
    return ok(svc._to_dict(svc.create(db, body.model_dump())))


@router.put("/{template_id}", summary="更新模板")
def update_template(template_id: str, body: TemplateUpsert,
                    db: Session = Depends(get_session)):
    o = svc.update(db, template_id, body.model_dump())
    if o is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return ok(svc._to_dict(o))


class TemplateReview(BaseModel):
    """B16 最小版：模板审核（只改状态，不动结构）。状态值与前端既有约定一致。"""
    status: str = Field(..., pattern="^(active|draft|reviewed|archived)$")


@router.post("/{template_id}/review", summary="审核模板（通过 / 退回 / 弃用）")
def review_template(template_id: str, body: TemplateReview,
                    db: Session = Depends(get_session)):
    """模板库的人工确认入口（B16 最小版，2026-09-17）：只改 `status`。

    语义：`active` 现役（进检索）/ `draft` 候选 / `reviewed` 已采用 / `archived` 归档（退出检索）。
    """
    o = svc.update(db, template_id, {"status": body.status})
    if o is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return ok(svc._to_dict(o))


@router.delete("/{template_id}", summary="删除模板")
def delete_template(template_id: str, db: Session = Depends(get_session)):
    if not svc.delete(db, template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return ok({"deleted": template_id})
