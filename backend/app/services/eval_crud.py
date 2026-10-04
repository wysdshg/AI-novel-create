"""评估服务（Phase 4.1 最小 eval，2026-09-10）。

职责：**章节生成版本留档 + 人工打分 + 对比数据**。
这是 Phase 4「Agent 闭环」的第一环 —— 没有它，后面任何改动（换模型 / 调温度 / 改检索策略）
都只能凭感觉判断是变好还是变坏。

设计原则：
- **留档是旁路**：任何异常都不得冒泡到生成主链路，一律吞掉并记日志（Phase 3.5 铁律）。
- 纯数据层：不依赖 router / 全局状态，便于单测。

分数语义（1~5）：1=不能用，2=勉强，3=及格，4=好，5=可以直接用。
"""
import logging
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.orm import ChapterVariantORM, EvalRecordORM

logger = logging.getLogger(__name__)

SCORE_MIN = 1
SCORE_MAX = 5


# ---------------------------------------------------------------------------
# 留档
# ---------------------------------------------------------------------------
def record_variant(db: Session, project_id: str, chapter_id: str, *,
                   article_id: str | None = None,
                   title: str | None = None,
                   content: str = "",
                   config_snapshot: dict | None = None,
                   metrics: dict | None = None) -> ChapterVariantORM | None:
    """留档一个生成版本。**失败返回 None**（留档是旁路，绝不能影响生成）。"""
    try:
        o = ChapterVariantORM(
            id=uuid.uuid4().hex,
            project_id=project_id,
            chapter_id=chapter_id,
            article_id=article_id,
            title=title,
            content=content or "",
            word_count=len(content or ""),
            config_snapshot=config_snapshot or {},
            metrics=metrics or {},
            created_at=datetime.utcnow(),
        )
        db.add(o)
        db.commit()
        db.refresh(o)
        return o
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[eval] 版本留档失败（不影响生成）chapter={str(chapter_id)[:8]}: {type(e).__name__}: {e}"
        )
        try:
            db.rollback()
        except Exception as e2:  # noqa: BLE001
            logger.debug(f"[eval] rollback 失败: {type(e2).__name__}: {e2}")
        return None


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _latest_scores(db: Session, variant_ids: list[str]) -> dict[str, EvalRecordORM]:
    """取每个版本**最新**的一条评分（同版本可重复打分，保留历史）。"""
    if not variant_ids:
        return {}
    rows = (
        db.query(EvalRecordORM)
        .filter(EvalRecordORM.variant_id.in_(variant_ids))
        # created_at 同 tick 打分两次时，"最新"必须取后写入者：rowid 是 SQLite 单调插入序，
        # 不加它则平手取决于扫描顺序（test_eval 偶发翻车根因，docs/04 B3 同类）
        .order_by(EvalRecordORM.created_at.asc(), text("rowid ASC"))
        .all()
    )
    latest: dict[str, EvalRecordORM] = {}
    for r in rows:
        latest[r.variant_id] = r  # asc 遍历 → 最后写入的即最新
    return latest


def variant_to_dict(v: ChapterVariantORM, ev: EvalRecordORM | None = None) -> dict:
    """版本 dict（不含正文，列表接口用；正文按需另取）。"""
    return {
        "id": v.id,
        "project_id": v.project_id,
        "chapter_id": v.chapter_id,
        "article_id": v.article_id,
        "title": v.title,
        "word_count": v.word_count,
        "config_snapshot": v.config_snapshot or {},
        "metrics": v.metrics or {},
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "score": ev.score if ev else None,
        "comment": ev.comment if ev else None,
        "scored_at": ev.created_at.isoformat() if ev and ev.created_at else None,
    }


def list_variants(db: Session, chapter_id: str, *, with_content: bool = False) -> list[dict]:
    """列出某章全部版本（含最新评分），按时间**倒序**（最新在前）。"""
    vs = (
        db.query(ChapterVariantORM)
        .filter_by(chapter_id=chapter_id)
        # 同 tick 插入的两个版本，"最新在前"必须按插入序：rowid 兜底（同 _latest_scores 注）
        .order_by(ChapterVariantORM.created_at.desc(), text("rowid DESC"))
        .all()
    )
    latest = _latest_scores(db, [v.id for v in vs])
    out = []
    for v in vs:
        d = variant_to_dict(v, latest.get(v.id))
        if with_content:
            d["content"] = v.content
        out.append(d)
    return out


def get_variant(db: Session, variant_id: str) -> dict | None:
    v = db.query(ChapterVariantORM).filter_by(id=variant_id).first()
    if v is None:
        return None
    ev = _latest_scores(db, [v.id]).get(v.id)
    d = variant_to_dict(v, ev)
    d["content"] = v.content
    return d


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def score_variant(db: Session, variant_id: str, score: int,
                  comment: str | None = None, dimensions: dict | None = None) -> dict | None:
    """给版本打分（**追加**一条记录，同版本可重复打分）。

    返回 None 表示：版本不存在，或分数越界（越界会被日志记下，不静默）。
    """
    if not isinstance(score, int) or isinstance(score, bool) or not (SCORE_MIN <= score <= SCORE_MAX):
        logger.warning(f"[eval] 分数越界，拒绝写入: {score!r}（合法 {SCORE_MIN}~{SCORE_MAX}）")
        return None
    v = db.query(ChapterVariantORM).filter_by(id=variant_id).first()
    if v is None:
        return None
    try:
        r = EvalRecordORM(
            id=uuid.uuid4().hex,
            variant_id=variant_id,
            score=score,
            comment=comment,
            dimensions=dimensions,
            created_at=datetime.utcnow(),
        )
        db.add(r)
        db.commit()
        db.refresh(r)
        return {
            "id": r.id,
            "variant_id": r.variant_id,
            "score": r.score,
            "comment": r.comment,
            "scored_at": r.created_at.isoformat() if r.created_at else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[eval] 打分落库失败 variant={str(variant_id)[:8]}: {type(e).__name__}: {e}")
        try:
            db.rollback()
        except Exception as e2:  # noqa: BLE001
            logger.debug(f"[eval] rollback 失败: {type(e2).__name__}: {e2}")
        return None


# ---------------------------------------------------------------------------
# 删除 / 级联
# ---------------------------------------------------------------------------
def delete_variant(db: Session, variant_id: str) -> bool:
    """删除版本（连同其全部评分）。"""
    try:
        v = db.query(ChapterVariantORM).filter_by(id=variant_id).first()
        if v is None:
            return False
        db.query(EvalRecordORM).filter_by(variant_id=variant_id).delete(synchronize_session=False)
        db.delete(v)
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[eval] 删除版本失败 variant={str(variant_id)[:8]}: {type(e).__name__}: {e}")
        try:
            db.rollback()
        except Exception as e2:  # noqa: BLE001
            logger.debug(f"[eval] rollback 失败: {type(e2).__name__}: {e2}")
        return False


def delete_by_chapter(db: Session, chapter_id: str, *, commit: bool = True) -> int:
    """删除某章的全部版本与评分（**章被删时级联调用**，避免留下孤儿版本）。

    `commit=False` 供调用方在自身事务内复用（如 `chapter_crud.delete_chapter`，
    那里要跟章的删除一起提交，不能提前 commit）。
    """
    try:
        ids = [r[0] for r in db.query(ChapterVariantORM.id).filter_by(chapter_id=chapter_id).all()]
        if not ids:
            return 0
        db.query(EvalRecordORM).filter(EvalRecordORM.variant_id.in_(ids)).delete(synchronize_session=False)
        n = db.query(ChapterVariantORM).filter_by(chapter_id=chapter_id).delete(synchronize_session=False)
        if commit:
            db.commit()
        return int(n or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[eval] 级联清理版本失败 chapter={str(chapter_id)[:8]}: {type(e).__name__}: {e}")
        try:
            db.rollback()
        except Exception as e2:  # noqa: BLE001
            logger.debug(f"[eval] rollback 失败: {type(e2).__name__}: {e2}")
        return 0


# ---------------------------------------------------------------------------
# 对比
# ---------------------------------------------------------------------------
def summarize(db: Session, chapter_id: str) -> dict:
    """对比视图数据：已评分版本按分数降序，并标出最高分与其配置。"""
    items = list_variants(db, chapter_id)
    scored = [x for x in items if x.get("score") is not None]
    scored.sort(key=lambda x: (-x["score"], x.get("created_at") or ""))
    best = scored[0] if scored else None
    avg = round(sum(x["score"] for x in scored) / len(scored), 2) if scored else None
    return {
        "total": len(items),
        "scored_count": len(scored),
        "avg_score": avg,
        "best": best,
        "variants": items,
    }
