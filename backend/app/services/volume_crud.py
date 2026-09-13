"""卷（volume）CRUD 服务。"""
import uuid

from sqlalchemy.orm import Session

from app.models.orm import (
    ArticleORM, ArticlePlanORM, ChapterMemoryORM, ChapterORM, DiscussionMessageORM,
    PlanCastingORM, PlannedCharORM, ReferenceDocORM, VolumeORM,
)
from app.schemas.volume import VolumeCreate, VolumeUpdate


def _now():
    from datetime import datetime
    return datetime.utcnow()


def create_volume(db: Session, project_id: str, data: VolumeCreate) -> VolumeORM:
    now = _now()
    o = VolumeORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        name=data.name,
        summary=data.summary,
        sort_order=data.sort_order,
        created_at=now,
        updated_at=now,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def list_volumes(db: Session, project_id: str) -> list[VolumeORM]:
    return db.query(VolumeORM).filter_by(project_id=project_id) \
        .order_by(VolumeORM.sort_order.asc(), VolumeORM.created_at.asc()).all()


def get_volume(db: Session, project_id: str, volume_id: str) -> VolumeORM | None:
    return db.query(VolumeORM).filter_by(project_id=project_id, id=volume_id).first()


def update_volume(db: Session, project_id: str, volume_id: str, data: VolumeUpdate) -> VolumeORM | None:
    o = get_volume(db, project_id, volume_id)
    if not o:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(o, k, v)
    o.updated_at = _now()
    db.commit()
    db.refresh(o)
    return o


def delete_volume(db: Session, project_id: str, volume_id: str) -> bool:
    """删除卷，并级联清理其下篇/章**及其全部派生数据**（Phase 2.4）。

    原实现只级联到 Article/Chapter，会留下被删章节的 `chapter_memories`、
    以及篇维度的 `reference_docs`（篇章摘要）。这里一并对齐。
    """
    o = get_volume(db, project_id, volume_id)
    if not o:
        return False
    article_ids = [r[0] for r in db.query(ArticleORM.id).filter_by(volume_id=volume_id).all()]
    if article_ids:
        chapter_ids = [
            r[0] for r in db.query(ChapterORM.id).filter(
                ChapterORM.article_id.in_(article_ids)).all()
        ]
        if chapter_ids:
            db.query(ChapterMemoryORM).filter(
                ChapterMemoryORM.chapter_id.in_(chapter_ids)).delete(synchronize_session=False)
            db.query(DiscussionMessageORM).filter(
                DiscussionMessageORM.project_id == project_id,
                DiscussionMessageORM.chapter_id.in_(chapter_ids),
            ).delete(synchronize_session=False)
        db.query(ChapterORM).filter(ChapterORM.article_id.in_(article_ids)).delete(
            synchronize_session=False)
        db.query(ReferenceDocORM).filter(
            ReferenceDocORM.project_id == project_id,
            ReferenceDocORM.article_id.in_(article_ids),
        ).delete(synchronize_session=False)
        # 篇规划与选角（Phase 7.3 ③ / 7.3.5）：卷删了 → 其下篇的 plan/casting/引入单也必须清
        db.query(PlanCastingORM).filter(
            PlanCastingORM.project_id == project_id,
            PlanCastingORM.article_id.in_(article_ids),
        ).delete(synchronize_session=False)
        db.query(PlannedCharORM).filter(
            PlannedCharORM.project_id == project_id,
            PlannedCharORM.article_id.in_(article_ids),
        ).delete(synchronize_session=False)
        db.query(ArticlePlanORM).filter(
            ArticlePlanORM.project_id == project_id,
            ArticlePlanORM.article_id.in_(article_ids),
        ).delete(synchronize_session=False)
        db.query(ArticleORM).filter(ArticleORM.id.in_(article_ids)).delete(
            synchronize_session=False)
    db.delete(o)
    db.commit()
    return True
