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
import logging

from app.core.database import get_session
from app.core.response import ok
from app.services import plan_crud
from app.services import vector_index
from app.services import plot_template_crud as tpl_crud
from app.models.orm import PlotTemplateORM

logger = logging.getLogger(__name__)

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


@router.get("/projects/{project_id}/articles/{article_id}/planned-chars/{pc_id}/archetypes",
            summary="S1：建卡弹窗的「功能位参考」（top-3 原型 + 自动起草数据，03 §8.5）")
def get_planned_char_archetypes(project_id: str, article_id: str, pc_id: str,
                                db: Session = Depends(get_session)):
    """建卡弹窗顶部的「功能位参考」卡片数据源。

    查询串退化（2026-09-17 放宽）：`slot_desc` → `slot` → **首登场行剧情摘要+名字**
    （未绑功能位的引入单也能拿到参考——这正是最常见的状态）→ 皆无才空。
    每个命中带回**结构化槽位**（slot/mode/ranks/traits/desc）——前端用它自动起草四个框。
    任何检索失败都返回空列表 —— 参考卡是增强项，**绝不阻断建卡**。
    """
    from app.models.orm import ArticlePlanORM
    pc = (db.query(plan_crud.PlannedCharORM)
          .filter_by(id=pc_id, project_id=project_id, article_id=article_id).first())
    if pc is None:
        raise HTTPException(404, "引入单不存在")

    query = (pc.slot_desc or "").strip() or (pc.slot or "").strip()
    if not query:
        # 未绑功能位：用首登场行的剧情摘要当查询（比拿名字瞎搜有意义——那行写着他要干什么）
        plan = db.query(ArticlePlanORM).filter_by(id=pc.plan_id).first()
        row = None
        if plan is not None:
            for ln in ((plan.plan or {}).get("lines") or []):
                if int(ln.get("no") or 0) == (pc.first_appearance or -1):
                    row = ln
                    break
        if row and (row.get("summary") or "").strip():
            query = f"{(pc.name or '').strip()}：{(row.get('summary') or '').strip()[:120]}"
        elif (pc.name or "").strip():
            query = pc.name.strip()
        else:
            return ok({"archetypes": [], "query": ""})

    items: list[dict] = []
    try:
        # 🔴 归档过滤（2026-10-02）：search_similar 是底层 KNN，不看模板状态——
        # 池子里躺着已归档旧模板的块（P3 换血后尤其明显），建卡弹窗会召回
        # 「没有位阶/性格的鬼卡」。多取再按状态过滤，凑满 3 张有效卡为止。
        hits = vector_index.search_similar(
            db, tpl_crud.GLOBAL, tpl_crud.SOURCE_TYPE_ARCHETYPE, query, top_k=12)
        for h in hits:
            if len(items) >= 3:
                break
            text = getattr(h, "chunk_text", None) or (h.get("chunk_text") if isinstance(h, dict) else "")
            score = getattr(h, "score", None) or (h.get("score") if isinstance(h, dict) else None)
            src_id = getattr(h, "source_id", None) or (h.get("source_id") if isinstance(h, dict) else None)
            if not text:
                continue
            trow = db.query(PlotTemplateORM).filter_by(id=src_id).first() if src_id else None
            if trow is None or (trow.status or "") == "archived":
                continue  # 孤儿块 / 已归档模板（旧向量残留）不进参考卡
            # chunk_text == archetype_text(cate)（确定性）→ 反查结构化槽位
            ref = None
            for c in tpl_crud.structure_casts(trow):
                if tpl_crud.archetype_text(c) == text:
                    ref = {"slot": c.get("slot"),
                           "desc": (c.get("desc") or "")[:60],
                           "mode": c.get("mode"),
                           "ranks": c.get("ranks") or [],
                           "traits": c.get("traits") or {}}
                    break
            items.append({"text": text[:80],
                          "score": round(float(score), 3) if score else None,
                          "template": trow.name if trow else None,
                          "template_id": src_id,
                          "ref": ref})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plans] 原型参考检索失败（返回空，不阻断建卡）: "
                       f"{type(e).__name__}: {str(e)[:120]}")
    return ok({"archetypes": items, "query": query})


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
