"""作品（项目）真实 CRUD（§1 分作品隔离）。

- 新建作品：生成 uuid 主键，落库 projects 表；
- 列表：分页返回，附 chapter_count（已写章节数）；
- 详情 / 修改 / 删除（级联清空该作品下所有业务数据）。
所有操作按 project_id 隔离，保证不同小说数据互不干扰。
"""
import logging
import uuid
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.orm import (
    ProjectORM, CharacterORM, SkillORM, RelationORM, FactionORM,
    ForeshadowORM, ChapterORM, DiscussionMessageORM, OutlineORM,
    VolumeORM, ArticleORM, ReferenceDocORM, LocationORM,
    ChapterMemoryORM, StageSummaryORM, DiscussionLoadLogORM,
    ArticlePlanORM, PlanCastingORM, PlannedCharORM,
)


logger = logging.getLogger(__name__)


def _to_dict(orm: ProjectORM, chapter_count: int = 0) -> dict:
    return {
        "id": orm.id,
        "name": orm.name,
        "genre": orm.genre,
        "summary": orm.summary,
        "status": orm.status,
        "db_backend": orm.db_backend,
        "chapter_count": chapter_count,
        "setting_ids": orm.setting_ids,
    }


def _count_chapters(db: Session, project_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(ChapterORM).where(ChapterORM.project_id == project_id)
    ) or 0


def create_project(db: Session, data: dict) -> dict:
    orm = ProjectORM(
        id=str(uuid.uuid4()),
        name=data["name"],
        genre=data.get("genre"),
        summary=data.get("summary"),
        status=data.get("status", "draft"),
        db_backend=data.get("db_backend", "sqlite"),
        setting_ids=data.get("setting_ids"),
    )
    db.add(orm)
    db.commit()
    db.refresh(orm)
    return _to_dict(orm, chapter_count=0)


def list_projects(db: Session, page: int = 1, page_size: int = 20) -> tuple[list, int]:
    total = db.scalar(select(func.count()).select_from(ProjectORM)) or 0
    stmt = select(ProjectORM).order_by(ProjectORM.created_at.desc()).limit(page_size).offset((page - 1) * page_size)
    rows = db.scalars(stmt).all()
    items = [
        _to_dict(r, chapter_count=_count_chapters(db, r.id)) for r in rows
    ]
    return items, total


def get_project(db: Session, project_id: str) -> dict | None:
    orm = db.get(ProjectORM, project_id)
    if orm is None:
        return None
    return _to_dict(orm, chapter_count=_count_chapters(db, project_id))


def update_project(db: Session, project_id: str, data: dict) -> dict | None:
    orm = db.get(ProjectORM, project_id)
    if orm is None:
        return None
    for k, v in data.items():
        if v is not None and hasattr(orm, k):
            setattr(orm, k, v)
    db.commit()
    db.refresh(orm)
    return _to_dict(orm, chapter_count=_count_chapters(db, project_id))


# 删除作品时一并清空的关联表（按 project_id 过滤，互不影响其他作品）
# 4 级结构相关（卷/篇/章）与参考、记忆、阶段摘要、加载日志均在列，避免孤儿行。
_RELATED = [
    CharacterORM, SkillORM, RelationORM, FactionORM, LocationORM,
    ForeshadowORM, ChapterORM, DiscussionMessageORM, OutlineORM,
    VolumeORM, ArticleORM, ReferenceDocORM,
    ChapterMemoryORM, StageSummaryORM, DiscussionLoadLogORM,
    ArticlePlanORM, PlanCastingORM, PlannedCharORM,
]


def delete_project(db: Session, project_id: str) -> bool:
    orm = db.get(ProjectORM, project_id)
    if orm is None:
        return False
    # 向量块（vector_chunks + vec_index）没有外键，必须显式清，否则成为孤儿
    # （2026-09-10 实测：删作品后残留 10 块）。
    try:
        from app.services import vector_store
        vector_store.remove_project_index(db, project_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[project_crud] 清理向量块跳过: {type(e).__name__}: {e}")
    for table in _RELATED:
        db.query(table).filter(table.project_id == project_id).delete()
    db.delete(orm)
    db.commit()
    return True
