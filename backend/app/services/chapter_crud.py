"""章节服务（4 级结构：小说 → 卷 → 篇 → 章）中的「章」持久化层。

完整 CRUD：create / list（按 project or article）/ get / update / delete。
所有变更同步更新 ProjectORM.chapter_count（按需）。
"""
import logging
import uuid

from sqlalchemy.orm import Session

from app.models.orm import ChapterMemoryORM, ChapterORM, ProjectORM
from app.schemas.chapter import ChapterCreate, ChapterUpdate
from app.services import eval_crud
from app.services.discussion_crud import clear_messages

logger = logging.getLogger(__name__)


def _now():
    from datetime import datetime
    return datetime.utcnow()


def find_existing_chapter(db: Session, project_id: str, article_id: str | None,
                          chapter_no: int | None, title: str | None):
    """**幂等生成保护**（2026-09-17，治"同名章生成两次"）：

    批量/计划驱动生成时，前端可能带不上 `chapter_id` → 落库走"新建"分支 →
    同一篇里出现两章同名（实测：废脉异变×2）。生成落库前先查：
    ① 同篇同章号；② 同篇同标题。命中 → **覆盖那一章**而不是新建。
    只在**同一篇内**查（跨篇同名是正常现象，比如两卷各有一章"夜谈"）。
    """
    q = db.query(ChapterORM).filter_by(project_id=project_id)
    if article_id:
        q = q.filter_by(article_id=article_id)
    if chapter_no:
        hit = q.filter_by(chapter_no=chapter_no).first()
        if hit is not None:
            return hit
    if title and str(title).strip():
        hit = q.filter_by(title=str(title).strip()).first()
        if hit is not None:
            return hit
    return None


def create_chapter(db: Session, project_id: str, data: ChapterCreate) -> ChapterORM:
    now = _now()
    o = ChapterORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        article_id=data.article_id,
        chapter_no=data.chapter_no,
        title=data.title,
        content=data.content,
        note=data.note,
        word_count=data.word_count,
        created_at=now,
        updated_at=now,
    )
    db.add(o)
    proj = db.query(ProjectORM).filter_by(id=project_id).first()
    if proj:
        proj.chapter_count = (proj.chapter_count or 0) + 1
        proj.updated_at = now
    db.commit()
    db.refresh(o)
    return o


def get_chapter(db: Session, project_id: str, chapter_id: str) -> ChapterORM | None:
    return db.query(ChapterORM).filter_by(project_id=project_id, id=chapter_id).first()


def list_chapters(db: Session, project_id: str, article_id: str | None = None) -> list[ChapterORM]:
    """列章：默认按 project 列出；如有 article_id 则仅列出该篇下的章。"""
    q = db.query(ChapterORM).filter_by(project_id=project_id)
    if article_id:
        q = q.filter_by(article_id=article_id)
    return q.order_by(ChapterORM.chapter_no.asc(), ChapterORM.created_at.asc()).all()


def update_chapter(db: Session, project_id: str, chapter_id: str, data: ChapterUpdate) -> ChapterORM | None:
    o = get_chapter(db, project_id, chapter_id)
    if not o:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(o, k, v)
    o.updated_at = _now()
    db.commit()
    db.refresh(o)
    return o


def delete_chapter(db: Session, project_id: str, chapter_id: str) -> bool:
    o = get_chapter(db, project_id, chapter_id)
    if not o:
        return False
    # 级联清理该章节的商讨线程（避免孤儿对话残留）
    clear_messages(db, project_id, chapter_id=chapter_id)
    # 级联清理该章节的「章级记忆」（Phase 2.4）：实测原实现会留下孤儿记忆行，
    # 而记忆会被后续章节的上下文注入读到 → 删了章却还能"回忆"到它。
    db.query(ChapterMemoryORM).filter_by(project_id=project_id, chapter_id=chapter_id).delete(
        synchronize_session=False)
    # 级联清理该章的「生成版本与评分」（Phase 4.1 最小 eval）：版本自带正文快照，
    # 不清理会留下孤儿并污染"版本对比"列表。commit=False → 与删章同一事务提交。
    eval_crud.delete_by_chapter(db, chapter_id, commit=False)
    db.delete(o)
    proj = db.query(ProjectORM).filter_by(id=project_id).first()
    if proj and (proj.chapter_count or 0) > 0:
        proj.chapter_count -= 1
    db.commit()
    return True
