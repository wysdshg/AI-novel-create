"""篇（article）CRUD 服务。"""
import uuid

from sqlalchemy.orm import Session

from app.models.orm import (
    ArticleORM, ArticlePlanORM, ChapterMemoryORM, ChapterORM, DiscussionMessageORM,
    PlanCastingORM, PlannedCharORM, ReferenceDocORM, VolumeORM,
)
from app.schemas.article import ArticleCreate, ArticleUpdate


def _now():
    from datetime import datetime
    return datetime.utcnow()


def create_article(db: Session, project_id: str, data: ArticleCreate) -> ArticleORM | None:
    """创建篇：必须校验所属卷存在且属于同一 project。"""
    vol = db.query(VolumeORM).filter_by(id=data.volume_id, project_id=project_id).first()
    if not vol:
        return None
    now = _now()
    o = ArticleORM(
        id=uuid.uuid4().hex,
        volume_id=data.volume_id,
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


def list_articles(db: Session, project_id: str, volume_id: str | None = None) -> list[ArticleORM]:
    """列篇：默认按 project 列出；如有 volume_id 则仅列出该卷下的篇。"""
    q = db.query(ArticleORM).filter_by(project_id=project_id)
    if volume_id:
        q = q.filter_by(volume_id=volume_id)
    return q.order_by(ArticleORM.sort_order.asc(), ArticleORM.created_at.asc()).all()


def get_article(db: Session, project_id: str, article_id: str) -> ArticleORM | None:
    return db.query(ArticleORM).filter_by(project_id=project_id, id=article_id).first()


def update_article(db: Session, project_id: str, article_id: str, data: ArticleUpdate) -> ArticleORM | None:
    o = get_article(db, project_id, article_id)
    if not o:
        return None
    payload = data.model_dump(exclude_unset=True)
    # 若要换卷，必须校验目标卷存在且属于同一 project
    if "volume_id" in payload and payload["volume_id"] and payload["volume_id"] != o.volume_id:
        vol = db.query(VolumeORM).filter_by(id=payload["volume_id"], project_id=project_id).first()
        if not vol:
            return None
    for k, v in payload.items():
        setattr(o, k, v)
    o.updated_at = _now()
    db.commit()
    db.refresh(o)
    return o


def delete_article(db: Session, project_id: str, article_id: str) -> bool:
    """删除篇，并级联清理其下章节**及其全部派生数据**（Phase 2.4）。

    实测（2026-09-10）原实现只删 ChapterORM，会留下：
      - `chapter_memories`：被删章节的章级记忆（后续章节还会把它注入上下文）
      - `discussion_messages`：这些章的商讨线程
      - `reference_docs`：`article_id` 指向该篇的篇章摘要文档
    """
    o = get_article(db, project_id, article_id)
    if not o:
        return False

    chapter_ids = [r[0] for r in db.query(ChapterORM.id).filter_by(article_id=article_id).all()]
    if chapter_ids:
        db.query(ChapterMemoryORM).filter(
            ChapterMemoryORM.chapter_id.in_(chapter_ids)).delete(synchronize_session=False)
        db.query(DiscussionMessageORM).filter(
            DiscussionMessageORM.project_id == project_id,
            DiscussionMessageORM.chapter_id.in_(chapter_ids),
        ).delete(synchronize_session=False)
    db.query(ChapterORM).filter_by(article_id=article_id).delete(synchronize_session=False)
    # 篇章维度的参考文档（每篇一份的「篇章参考」）
    db.query(ReferenceDocORM).filter_by(project_id=project_id, article_id=article_id).delete(
        synchronize_session=False)
    # 篇规划与选角（Phase 7.3 ③ / 7.3.5）：`article_plans` / `plan_castings` /
    # `plan_chars` 都挂在篇上，篇没了它们就是孤儿。⚠️ 必须显式删 —— SQLite 外键
    # 约束默认不开（PRAGMA foreign_keys=OFF），指望 ON DELETE CASCADE 会静默失效、
    # 留一堆指向不存在篇的孤儿行。
    db.query(PlanCastingORM).filter_by(project_id=project_id, article_id=article_id).delete(
        synchronize_session=False)
    db.query(PlannedCharORM).filter_by(project_id=project_id, article_id=article_id).delete(
        synchronize_session=False)
    db.query(ArticlePlanORM).filter_by(project_id=project_id, article_id=article_id).delete(
        synchronize_session=False)
    db.delete(o)
    db.commit()
    return True
