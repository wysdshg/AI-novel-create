# -*- coding: utf-8 -*-
"""GraphRAG 注入装配端点（docs/09 §3，阶段 A3）。

POST /projects/{pid}/graph-rag/assemble   body: {names?: [角色名…], seed_ids?: [{id,type}], max_edges?}
返回 {blocks, injection, edges, dead} —— 供商讨链/写作链调用方拼 prompt（injection 是成品文本）。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.response import ok
from app.services import graph_rag

router = APIRouter(prefix="/projects", tags=["GraphRAG"])


class AssembleBody(BaseModel):
    names: list[str] | None = None          # 种子实体名（五表精确匹配）
    seed_ids: list[dict] | None = None      # 或直接给 [{id, type}]
    max_edges: int | None = None


@router.post("/{project_id}/graph-rag/assemble", summary="GraphRAG：按种子实体组装一跳注入块")
def assemble(project_id: str, body: AssembleBody, db: Session = Depends(get_session)):
    seeds = body.seed_ids or graph_rag.resolve_seeds(db, project_id, body.names or [])
    result = graph_rag.assemble(db, project_id, seeds,
                                max_edges=body.max_edges or 80)
    return ok(result)
