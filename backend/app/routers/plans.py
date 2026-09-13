"""篇规划 API（Phase 7.2，路径前缀在 main.py 拼 /api/v1）。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/projects/{pid}/articles/{aid}/plan/generate` | 生成本篇章计划（body: hint/n_chapters/force_free） |
| GET | `/projects/{pid}/articles/{aid}/plan` | 读当前计划（没有返回 null） |
| PUT | `/projects/{pid}/articles/{aid}/plan` | 保存行级编辑（表格直接改） |
| POST | `…/plan/refine-line` | **AI 只改一行**（其余行原样） |
| POST | `…/plan/confirm` | 作者拍板（draft → confirmed，此后生成注入本章任务） |
| GET | `…/planned-chars` | 新角色引入单列表（7.3.5） |
| PUT | `…/planned-chars/{pc_id}` | 调整引入单（绑槽位/改首登场章/忽略） |
| POST | `…/planned-chars/{pc_id}/confirm` | 确认建卡进角色库（回链 character_id） |

计划行字段：`{no, beat, summary, new_chars[], recall_chars[], target_words, hook, template_ref}`。
防幻觉：召回角色经后端校验（查无此人剔除）；新角色落 `plan_chars` 引入单（≤3、首登场行号自动填），
作者确认后才建卡 —— 补上"计划新角色 → 角色库"断掉的一环（7.3.5）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.response import ok
from app.services import plan_crud

router = APIRouter(tags=["篇规划"])


class GenerateBody(BaseModel):
    hint: str = Field("", description="作者口述（最高优先级）")
    n_chapters: int = Field(8, ge=2, le=40, description="本篇章数")
    force_free: bool = Field(False, description="跳过模板检索，强制自由规划")


class RefineLineBody(BaseModel):
    line_no: int = Field(..., ge=1)
    instruction: str = Field(..., min_length=2, description="修改要求")


class SaveLinesBody(BaseModel):
    lines: list[dict]
    notes: str | None = None


class PlannedCharUpdateBody(BaseModel):
    """引入单调整（只改作者给的字段）。"""
    slot: str | None = None
    slot_desc: str | None = None
    first_appearance: int | None = Field(None, ge=1)
    status: str | None = Field(None, description="pending|dismissed（confirmed 走 confirm 端点）")


class PlannedCharConfirmBody(BaseModel):
    """确认建卡时补充的角色属性（全可选，空了给兜底）。"""
    role_type: str = ""
    personality: str = ""
    background: str = ""
    talent: str = ""
    current_level: str = ""
    brief: str = ""


@router.post("/projects/{project_id}/articles/{article_id}/plan/generate",
             summary="生成本篇章计划（模板 + 上下文 + 口述）")
def generate_plan(project_id: str, article_id: str, body: GenerateBody,
                  db: Session = Depends(get_session)):
    try:
        return ok(plan_crud.generate_plan(db, project_id, article_id, hint=body.hint,
                                          n_chapters=body.n_chapters,
                                          force_free=body.force_free))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/projects/{project_id}/articles/{article_id}/plan", summary="读当前计划")
def get_plan(project_id: str, article_id: str, db: Session = Depends(get_session)):
    return ok(plan_crud.get_plan(db, project_id, article_id))     # None = 还没规划


@router.put("/projects/{project_id}/articles/{article_id}/plan", summary="保存行级编辑")
def save_plan(project_id: str, article_id: str, body: SaveLinesBody,
              db: Session = Depends(get_session)):
    try:
        return ok(plan_crud.save_lines(db, project_id, article_id,
                                       lines=body.lines, notes=body.notes))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/projects/{project_id}/articles/{article_id}/plan/refine-line",
             summary="AI 只改一行（其余行原样）")
def refine_line(project_id: str, article_id: str, body: RefineLineBody,
                db: Session = Depends(get_session)):
    try:
        return ok(plan_crud.refine_line(db, project_id, article_id,
                                        line_no=body.line_no, instruction=body.instruction))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/projects/{project_id}/articles/{article_id}/plan/confirm", summary="拍板确认")
def confirm_plan(project_id: str, article_id: str, db: Session = Depends(get_session)):
    try:
        return ok(plan_crud.confirm_plan(db, project_id, article_id))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# 新角色引入单（Phase 7.3.5 B 档，2026-09-13）
# ---------------------------------------------------------------------------
@router.get("/projects/{project_id}/articles/{article_id}/planned-chars",
            summary="新角色引入单列表")
def list_planned_chars(project_id: str, article_id: str,
                       db: Session = Depends(get_session)):
    return ok(plan_crud.list_planned_chars(db, project_id, article_id))


@router.put("/projects/{project_id}/articles/{article_id}/planned-chars/{pc_id}",
            summary="调整引入单（绑槽位/改首登场章/忽略）")
def update_planned_char(project_id: str, article_id: str, pc_id: str,
                        body: PlannedCharUpdateBody,
                        db: Session = Depends(get_session)):
    try:
        return ok(plan_crud.update_planned_char(
            db, project_id, article_id, pc_id,
            slot=body.slot, slot_desc=body.slot_desc,
            first_appearance=body.first_appearance, status=body.status))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/projects/{project_id}/articles/{article_id}/planned-chars/{pc_id}/confirm",
             summary="确认引入单 → 建卡进角色库")
def confirm_planned_char(project_id: str, article_id: str, pc_id: str,
                         body: PlannedCharConfirmBody,
                         db: Session = Depends(get_session)):
    try:
        return ok(plan_crud.confirm_planned_char(
            db, project_id, article_id, pc_id,
            attrs=body.model_dump(exclude_none=True)))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
