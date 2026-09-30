"""模块1：分作品资料库系统（需求 1、4、10）。路径前缀见 main.py 的 /api/v1。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.schemas.database import (
    CharacterCreate, CharacterUpdate, SkillCreate, SkillUpdate,
    RelationCreate, RelationUpdate, FactionCreate, FactionUpdate,
    LocationCreate, LocationUpdate,
    ForeshadowCreate, ForeshadowUpdate, ValidateRequest, CommandRequest,
)
from app.core.response import ok
from app.core.database import get_session
from app.services import (
    character_crud, faction_crud, location_crud, relation_crud,
    skill_crud,                              # 本轮升为真实持久化（替换原占位）
    item_crud,                               # 2026-09-21：A5 批次③补齐（表在 A1 已建、AI 抽取已落库）
    config_command,                          # 已无 stubs 依赖（validate 下架后）
)

router = APIRouter(tags=["资料库"])


# ---------- 角色（已落地真实持久化） ----------
@router.post("/projects/{project_id}/characters")
def create_character(
    project_id: str,
    body: CharacterCreate,
    db: Session = Depends(get_session),
):
    return ok(character_crud.create_character(db, project_id, body).model_dump(mode="json"))


@router.get("/projects/{project_id}/characters")
def list_characters(
    project_id: str,
    db: Session = Depends(get_session),
):
    return ok([c.model_dump(mode="json") for c in character_crud.list_characters(db, project_id)])


@router.get("/projects/{project_id}/characters/{character_id}")
def get_character(project_id: str, character_id: str, db: Session = Depends(get_session)):
    o = character_crud.get_character(db, project_id, character_id)
    if o is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return ok(character_crud._to_schema(o).model_dump(mode="json"))


@router.put("/projects/{project_id}/characters/{character_id}")
def update_character(
    project_id: str,
    character_id: str,
    body: CharacterUpdate,
    db: Session = Depends(get_session),
):
    updated = character_crud.update_character(db, project_id, character_id, body)
    if updated is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return ok(updated.model_dump(mode="json"))


@router.delete("/projects/{project_id}/characters/{character_id}")
def delete_character(project_id: str, character_id: str, db: Session = Depends(get_session)):
    if not character_crud.delete_character(db, project_id, character_id):
        raise HTTPException(status_code=404, detail="角色不存在")
    return ok({"deleted": character_id})


# ---------- S3 角色修订（03 §8.7：版本历史 + AI 改动审批 + 回滚） ----------
@router.get("/projects/{project_id}/characters/{character_id}/revisions")
def list_character_revisions(project_id: str, character_id: str,
                             db: Session = Depends(get_session)):
    if character_crud.get_character(db, project_id, character_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    from app.services import character_revision_crud as rev
    return ok(rev.list_revisions(db, project_id, character_id))


class RevisionProposeBody(BaseModel):
    changes: dict                                    # 只接受 SNAPSHOT_FIELDS 里的键
    chapter_no: int | None = Field(None, ge=1)
    note: str | None = None


@router.post("/projects/{project_id}/characters/{character_id}/revisions")
def propose_character_revision(project_id: str, character_id: str,
                               body: RevisionProposeBody,
                               db: Session = Depends(get_session)):
    """提出一次修订（AI 或作者起草）：**只写 pending，绝不直接覆盖角色卡**。"""
    from app.services import character_revision_crud as rev
    r = rev.propose(db, project_id, character_id, body.changes,
                    chapter_no=body.chapter_no, note=body.note)
    if r is None or r.get("id") is None:
        raise HTTPException(status_code=404, detail="角色不存在或没有可应用的变更")
    return ok(r)


@router.post("/projects/{project_id}/characters/{character_id}/revisions/{revision_id}/approve")
def approve_character_revision(project_id: str, character_id: str, revision_id: str,
                               db: Session = Depends(get_session)):
    from app.services import character_revision_crud as rev
    r = rev.decide(db, project_id, revision_id, approve=True)
    if r is None:
        raise HTTPException(status_code=404, detail="修订不存在")
    return ok(r)


@router.post("/projects/{project_id}/characters/{character_id}/revisions/{revision_id}/reject")
def reject_character_revision(project_id: str, character_id: str, revision_id: str,
                              db: Session = Depends(get_session)):
    from app.services import character_revision_crud as rev
    r = rev.decide(db, project_id, revision_id, approve=False)
    if r is None:
        raise HTTPException(status_code=404, detail="修订不存在")
    return ok(r)


@router.post("/projects/{project_id}/characters/{character_id}/revisions/{revision_id}/rollback")
def rollback_character_revision(project_id: str, character_id: str, revision_id: str,
                                db: Session = Depends(get_session)):
    from app.services import character_revision_crud as rev
    r = rev.rollback(db, project_id, character_id, revision_id)
    if r is None:
        raise HTTPException(status_code=404, detail="修订不存在或无快照")
    return ok(r)


# ---------- 技能（§2.2，本轮升级为真实持久化） ----------
@router.post("/projects/{project_id}/skills")
def create_skill(
    project_id: str,
    body: SkillCreate,
    db: Session = Depends(get_session),
):
    return ok(skill_crud.create_skill(db, project_id, body).model_dump(mode="json"))


@router.get("/projects/{project_id}/skills")
def list_skills(project_id: str, db: Session = Depends(get_session)):
    return ok([s.model_dump(mode="json") for s in skill_crud.list_skills(db, project_id)])


@router.get("/projects/{project_id}/skills/{skill_id}")
def get_skill(project_id: str, skill_id: str, db: Session = Depends(get_session)):
    s = skill_crud.get_skill(db, project_id, skill_id)
    if s is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    return ok(s.model_dump(mode="json"))


@router.put("/projects/{project_id}/skills/{skill_id}")
def update_skill(
    project_id: str,
    skill_id: str,
    body: SkillUpdate,
    db: Session = Depends(get_session),
):
    s = skill_crud.update_skill(db, project_id, skill_id, body)
    if s is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    return ok(s.model_dump(mode="json"))


@router.delete("/projects/{project_id}/skills/{skill_id}")
def delete_skill(project_id: str, skill_id: str, db: Session = Depends(get_session)):
    if not skill_crud.delete_skill(db, project_id, skill_id):
        raise HTTPException(status_code=404, detail="技能不存在")
    return ok({"deleted": skill_id})


# ---------- 关系（已落地真实持久化） ----------
@router.post("/projects/{project_id}/relations")
def create_relation(
    project_id: str,
    body: RelationCreate,
    db: Session = Depends(get_session),
):
    return ok(relation_crud.create_relation(db, project_id, body).model_dump(mode="json"))


@router.get("/projects/{project_id}/relations")
def list_relations(
    project_id: str,
    character_id: str | None = None,
    db: Session = Depends(get_session),
):
    return ok([r.model_dump(mode="json") for r in relation_crud.list_relations(db, project_id, character_id)])


@router.put("/projects/{project_id}/relations/{relation_id}")
def update_relation(
    project_id: str,
    relation_id: str,
    body: RelationUpdate,
    db: Session = Depends(get_session),
):
    updated = relation_crud.update_relation(db, project_id, relation_id, body)
    if updated is None:
        raise HTTPException(status_code=404, detail="关系不存在")
    return ok(updated.model_dump(mode="json"))


@router.delete("/projects/{project_id}/relations/{relation_id}")
def delete_relation(
    project_id: str,
    relation_id: str,
    db: Session = Depends(get_session),
):
    if not relation_crud.delete_relation(db, project_id, relation_id):
        raise HTTPException(status_code=404, detail="关系不存在")
    return ok({"deleted": relation_id})


# ---------- 物品（2026-09-21：A5 批次③补齐——表在 A1 已建、AI 抽取已落库，补 REST 端点 + 前端） ----------
def _item_to_dict(o) -> dict:
    return {
        "id": o.id, "name": o.name, "category": o.category,
        "summary": o.summary, "full_desc": o.full_desc,
        "is_unique": bool(o.is_unique), "tags": o.tags or [],
        "status": o.status, "ai_generated": bool(o.ai_generated),
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


@router.post("/projects/{project_id}/items")
def create_item(project_id: str, body: dict, db: Session = Depends(get_session)):
    if not (body.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="物品名不能为空")
    o = item_crud.create_item(db, project_id, body)
    return ok(_item_to_dict(o))


@router.get("/projects/{project_id}/items")
def list_items(project_id: str, db: Session = Depends(get_session)):
    return ok([_item_to_dict(o) for o in item_crud.list_items(db, project_id)])


@router.get("/projects/{project_id}/items/{item_id}")
def get_item(project_id: str, item_id: str, db: Session = Depends(get_session)):
    o = item_crud.get_item(db, project_id, item_id)
    if o is None:
        raise HTTPException(status_code=404, detail="物品不存在")
    return ok(_item_to_dict(o))


@router.put("/projects/{project_id}/items/{item_id}")
def update_item(project_id: str, item_id: str, body: dict, db: Session = Depends(get_session)):
    o = item_crud.update_item(db, project_id, item_id, body)
    if o is None:
        raise HTTPException(status_code=404, detail="物品不存在")
    return ok(_item_to_dict(o))


@router.delete("/projects/{project_id}/items/{item_id}")
def delete_item(project_id: str, item_id: str, db: Session = Depends(get_session)):
    if not item_crud.delete_item(db, project_id, item_id):
        raise HTTPException(status_code=404, detail="物品不存在")
    return ok({"deleted": item_id})


# ---------- 势力（已落地真实持久化） ----------
@router.post("/projects/{project_id}/factions")
def create_faction(
    project_id: str,
    body: FactionCreate,
    db: Session = Depends(get_session),
):
    return ok(faction_crud.create_faction(db, project_id, body).model_dump(mode="json"))


@router.get("/projects/{project_id}/factions")
def list_factions(
    project_id: str,
    db: Session = Depends(get_session),
):
    return ok([f.model_dump(mode="json") for f in faction_crud.list_factions(db, project_id)])


@router.get("/projects/{project_id}/factions/{faction_id}")
def get_faction(project_id: str, faction_id: str, db: Session = Depends(get_session)):
    o = faction_crud.get_faction(db, project_id, faction_id)
    if o is None:
        raise HTTPException(status_code=404, detail="势力不存在")
    return ok(faction_crud._to_schema(o).model_dump(mode="json"))


@router.put("/projects/{project_id}/factions/{faction_id}")
def update_faction(
    project_id: str,
    faction_id: str,
    body: FactionUpdate,
    db: Session = Depends(get_session),
):
    updated = faction_crud.update_faction(db, project_id, faction_id, body)
    if updated is None:
        raise HTTPException(status_code=404, detail="势力不存在")
    return ok(updated.model_dump(mode="json"))


@router.delete("/projects/{project_id}/factions/{faction_id}")
def delete_faction(project_id: str, faction_id: str, db: Session = Depends(get_session)):
    if not faction_crud.delete_faction(db, project_id, faction_id):
        raise HTTPException(status_code=404, detail="势力不存在")
    return ok({"deleted": faction_id})


# ---------- 地点（已落地真实持久化） ----------
@router.post("/projects/{project_id}/locations")
def create_location(
    project_id: str,
    body: LocationCreate,
    db: Session = Depends(get_session),
):
    return ok(location_crud.create_location(db, project_id, body).model_dump(mode="json"))


@router.get("/projects/{project_id}/locations")
def list_locations(
    project_id: str,
    db: Session = Depends(get_session),
):
    return ok([l.model_dump(mode="json") for l in location_crud.list_locations(db, project_id)])


@router.get("/projects/{project_id}/locations/{location_id}")
def get_location(project_id: str, location_id: str, db: Session = Depends(get_session)):
    o = location_crud.get_location(db, project_id, location_id)
    if o is None:
        raise HTTPException(status_code=404, detail="地点不存在")
    return ok(location_crud._to_schema(o).model_dump(mode="json"))


@router.put("/projects/{project_id}/locations/{location_id}")
def update_location(
    project_id: str,
    location_id: str,
    body: LocationUpdate,
    db: Session = Depends(get_session),
):
    updated = location_crud.update_location(db, project_id, location_id, body)
    if updated is None:
        raise HTTPException(status_code=404, detail="地点不存在")
    return ok(updated.model_dump(mode="json"))


@router.delete("/projects/{project_id}/locations/{location_id}")
def delete_location(project_id: str, location_id: str, db: Session = Depends(get_session)):
    if not location_crud.delete_location(db, project_id, location_id):
        raise HTTPException(status_code=404, detail="地点不存在")
    return ok({"deleted": location_id})


@router.get("/projects/{project_id}/locations/{location_id}/geo-relations")
def location_geo_relations(project_id: str, location_id: str, db: Session = Depends(get_session)):
    """同位面内其他地点的方位与距离（坐标自动推导，无需手填）。"""
    res = location_crud.geo_relations(db, project_id, location_id)
    if res is None:
        raise HTTPException(status_code=404, detail="地点不存在")
    return ok(res)


# ---------- 设定校验（需求 4） ----------
@router.post("/projects/{project_id}/validate")
def validate_settings(project_id: str, body: ValidateRequest):
    """设定校验：**尚未实现**，显式返回 501。

    ⚠️ 这里刻意不是「返回空 issues」。原实现走 `stubs.validate_settings` 恒返回
    `{"constraint_list": [], "issues": []}`——界面上表现为「校验通过、零问题」，
    是**假绿灯**：比没有这个功能更危险（作者会以为设定已被检查过）。

    📌 与生成链路里的 `validate` **SSE 事件**无关——那个是活的（去 AI 味 + 字数等
    确定性扫描，见 chapter.py），本端点下架不影响它。
    """
    raise HTTPException(
        status_code=501,
        detail="设定校验尚未实现（原实现恒返回空 issues，属假绿灯，已下架）。"
               "生成时的 AI 味扫描不受影响。",
    )


# ---------- 配置对话：自然语言自动整理资料库（需求 10） ----------
@router.post("/projects/{project_id}/command")
def run_command(project_id: str, body: CommandRequest, db: Session = Depends(get_session)):
    return config_command.run(db, project_id, body.text, body.dry_run, entity_type=body.entity_type)
