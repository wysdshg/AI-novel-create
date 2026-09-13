"""角色选角 API（Phase 7.3 ③，路径前缀在 main.py 拼 /api/v1）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/projects/{pid}/articles/{aid}/casting` | 读该篇的选角结果（唯一真相源） |
| POST | `…/casting/recompute` | 重算选角（手动触发，如补了角色人设之后） |
| PUT | `…/casting` | 作者手改某槽位的选角（标 manual，重算不覆盖） |
| POST | `/projects/{pid}/characters/refresh-appearances` | 派生刷新角色出场台账 |
| GET | `/projects/{pid}/characters/{cid}/reentry-material` | 回归理由材料包（7.3.5，确定性预取） |

⚠️ 手改**只动 `plan_castings` 表，不碰 `plan.json`** —— 见 `casting_crud.set_casting`。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.response import ok
from app.services import casting_crud

router = APIRouter(tags=["角色选角"])


class SetCastingBody(BaseModel):
    slot: str = Field(..., min_length=1, description="功能槽位名")
    character_id: str | None = Field(None, description="本书角色 id；null = 清空该槽位")


@router.get("/projects/{project_id}/articles/{article_id}/casting",
            summary="读该篇选角结果")
def list_castings(project_id: str, article_id: str, db: Session = Depends(get_session)):
    return ok(casting_crud.list_castings(db, project_id, article_id))


@router.post("/projects/{project_id}/articles/{article_id}/casting/recompute",
             summary="重算选角（向量匹配）")
def recompute(project_id: str, article_id: str, db: Session = Depends(get_session)):
    try:
        return ok(casting_crud.cast_slots_for_plan(db, project_id, article_id))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/projects/{project_id}/articles/{article_id}/casting",
            summary="作者手改某槽位选角（manual，重算不覆盖）")
def set_casting(project_id: str, article_id: str, body: SetCastingBody,
                db: Session = Depends(get_session)):
    try:
        return ok(casting_crud.set_casting(db, project_id, article_id,
                                           slot=body.slot, character_id=body.character_id))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/projects/{project_id}/characters/refresh-appearances",
             summary="派生刷新角色出场台账（last_seen / appearance_count）")
def refresh_appearances(project_id: str, db: Session = Depends(get_session)):
    return ok(casting_crud.refresh_character_appearances(db, project_id))


@router.get("/projects/{project_id}/characters/{character_id}/reentry-material",
            summary="回归理由材料包（未回收伏笔 → 缺席期事件 → 兜底）")
def reentry_material(project_id: str, character_id: str,
                     db: Session = Depends(get_session)):
    """给作者/前端单独查某个角色的回归材料（计划生成时也会自动预取进 plan JSON）。"""
    m = casting_crud.reentry_material(db, project_id, character_id)
    if not m:
        raise HTTPException(status_code=404, detail="角色不存在")
    return ok(m)
