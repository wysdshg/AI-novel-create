"""全局物品/技能库 CRUD（阶段 E2，docs/09 §7 双库设计 + E1 口径 2026-09-25 拍板）。

四张表：
- 条目库 `global_items` / `global_skills`：类型惯例词（筑基丹/储物袋/御剑术…），
  让 AI 优先用通用词、抽取可归一化，不硬造「伐骨丹」「青云术」；
- 尺度库 `global_item_scales` / `global_skill_scales`：类别级设计尺度（功能位/品阶轴/
  强度/代价/叙事位置），让 AI 会**造新的**而不千篇一律。

三条硬纪律在本模块的落点：
- ① 条目 brief ≤50 字、不存原文片段 → `add_item/add_skill` 入库时校验；
- ② `reference_only=True` 的行**禁止注入** → `inject_items/inject_skills` 硬过滤，
  调用方拿不到（不靠自觉）；
- ③ 尺度库只存结构不存成品描述 → 由 seed/录入流程保证（本模块不做语义校验）。

幂等：add 按 (name, genre) 判重——同主名同题材池只留一条，重复 add 返回已有行。
"""
import logging
import uuid
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.orm import (
    GlobalItemORM,
    GlobalItemScaleORM,
    GlobalSkillORM,
    GlobalSkillScaleORM,
)

logger = logging.getLogger(__name__)

MAX_BRIEF = 50  # 硬纪律①：一句话通用说明上限（含标点）

_ITEM_CATEGORIES = {"丹药", "武器", "防具", "法器法宝", "材料", "灵石货币", "符箓", "信物", "境界"}
_SKILL_CATEGORIES = {"攻击技", "身法遁术", "幻术", "阵法", "生活职业", "功法"}
_GENRES = {"仙侠", "历史", "高武", "通用"}


def _now() -> datetime:
    return datetime.utcnow()


def _norm_brief(brief: str) -> str:
    """硬纪律①的落点：截断超长 brief 并告警（调用方多为 seed 脚本，截断比抛错友好）。"""
    b = (brief or "").strip()
    if len(b) > MAX_BRIEF:
        logger.warning(f"[global_ref] brief 超 {MAX_BRIEF} 字已截断（{len(b)}→{MAX_BRIEF}）: {b[:30]}…")
        b = b[:MAX_BRIEF]
    return b


def _check_category(category: str, allowed: set[str], label: str) -> None:
    if category not in allowed:
        raise ValueError(f"{label} category 非法: {category!r}（允许值: {sorted(allowed)}）")


# ---------------------------------------------------------------------------
# 条目库
# ---------------------------------------------------------------------------
def add_item(db: Session, *, name: str, category: str, brief: str,
             genre: str = "通用", aliases: list[str] | None = None,
             public_domain: bool = False, reference_only: bool = False,
             full_desc: str | None = None) -> GlobalItemORM:
    """新增/获取物品条目（幂等：name+genre 命中即返回已有行，不重复建）。"""
    _check_category(category, _ITEM_CATEGORIES, "global_items")
    name = (name or "").strip()
    if not name:
        raise ValueError("global_items.name 不能为空")
    existing = (db.query(GlobalItemORM)
                .filter(GlobalItemORM.name == name, GlobalItemORM.genre == genre)
                .first())
    if existing:
        return existing
    row = GlobalItemORM(
        id=uuid.uuid4().hex, name=name, category=category, genre=genre,
        brief=_norm_brief(brief),
        full_desc=(full_desc or "").strip() or None,
        aliases=[a.strip() for a in (aliases or []) if a.strip()],
        public_domain=bool(public_domain), reference_only=bool(reference_only),
        status="active", created_at=_now(), updated_at=_now())
    db.add(row)
    db.commit()
    return row


def add_skill(db: Session, *, name: str, category: str, brief: str,
              genre: str = "通用", aliases: list[str] | None = None,
              public_domain: bool = False, reference_only: bool = False,
              full_desc: str | None = None) -> GlobalSkillORM:
    """新增/获取技能条目（幂等同 add_item）。"""
    _check_category(category, _SKILL_CATEGORIES, "global_skills")
    name = (name or "").strip()
    if not name:
        raise ValueError("global_skills.name 不能为空")
    existing = (db.query(GlobalSkillORM)
                .filter(GlobalSkillORM.name == name, GlobalSkillORM.genre == genre)
                .first())
    if existing:
        return existing
    row = GlobalSkillORM(
        id=uuid.uuid4().hex, name=name, category=category, genre=genre,
        brief=_norm_brief(brief),
        full_desc=(full_desc or "").strip() or None,
        aliases=[a.strip() for a in (aliases or []) if a.strip()],
        public_domain=bool(public_domain), reference_only=bool(reference_only),
        status="active", created_at=_now(), updated_at=_now())
    db.add(row)
    db.commit()
    return row


def normalize_lookup(db: Session, name: str) -> tuple[str, GlobalItemORM | GlobalSkillORM] | None:
    """E3 抽取归一化的查找入口：name 精确匹配主名或别名（active 才算命中）。

    返回 ("item"|"skill", 行) 或 None。命中即表示「类型惯例词」——
    抽取侧应指向该条目，**不重复建本地条目**。

    🔴 别名匹配用 Python 过滤而不是 JSON 列 `.contains()`：SQLite 的 JSON 序列化
    默认 ensure_ascii 转义中文，LIKE 匹配不到（实测踩坑）；条目量 ≤60/池（G6），
    全量过滤无性能顾虑。
    """
    n = (name or "").strip()
    if not n:
        return None
    row = (db.query(GlobalItemORM)
           .filter(GlobalItemORM.status == "active", GlobalItemORM.name == n)
           .first())
    if row:
        return "item", row
    for row in db.query(GlobalItemORM).filter(GlobalItemORM.status == "active").all():
        if n in (row.aliases or []):
            return "item", row
    row = (db.query(GlobalSkillORM)
           .filter(GlobalSkillORM.status == "active", GlobalSkillORM.name == n)
           .first())
    if row:
        return "skill", row
    for row in db.query(GlobalSkillORM).filter(GlobalSkillORM.status == "active").all():
        if n in (row.aliases or []):
            return "skill", row
    return None


# ---------------------------------------------------------------------------
# 注入层（硬纪律②：reference_only / disabled 一律不可见）
# ---------------------------------------------------------------------------
def inject_items(db: Session, *, category: str | None = None,
                 genre: str | None = None, limit: int = 30) -> list[GlobalItemORM]:
    """写作/建物注入的**唯一合法入口**：只回 active 且非 reference_only 的条目。"""
    q = db.query(GlobalItemORM).filter(GlobalItemORM.status == "active",
                                       GlobalItemORM.reference_only == False)  # noqa: E712
    if category:
        q = q.filter(GlobalItemORM.category == category)
    if genre:
        q = q.filter(GlobalItemORM.genre.in_([genre, "通用"]))
    return q.order_by(GlobalItemORM.category, GlobalItemORM.name).limit(max(1, limit)).all()


def inject_skills(db: Session, *, category: str | None = None,
                  genre: str | None = None, limit: int = 30) -> list[GlobalSkillORM]:
    q = db.query(GlobalSkillORM).filter(GlobalSkillORM.status == "active",
                                        GlobalSkillORM.reference_only == False)  # noqa: E712
    if category:
        q = q.filter(GlobalSkillORM.category == category)
    if genre:
        q = q.filter(GlobalSkillORM.genre.in_([genre, "通用"]))
    return q.order_by(GlobalSkillORM.category, GlobalSkillORM.name).limit(max(1, limit)).all()


# ---------------------------------------------------------------------------
# 尺度库
# ---------------------------------------------------------------------------
def add_item_scale(db: Session, *, category: str, function_pos: str,
                   genre: str = "通用", grade_axis: str | None = None,
                   intensity_scale: str | None = None, cost_scale: str | None = None,
                   rarity: str = "常见", notes: str | None = None) -> GlobalItemScaleORM:
    """新增/获取物品尺度（幂等：category+genre+rarity 命中即返回）。"""
    _check_category(category, _ITEM_CATEGORIES, "global_item_scales")
    existing = (db.query(GlobalItemScaleORM)
                .filter(GlobalItemScaleORM.category == category,
                        GlobalItemScaleORM.genre == genre,
                        GlobalItemScaleORM.rarity == rarity)
                .first())
    if existing:
        return existing
    row = GlobalItemScaleORM(
        id=uuid.uuid4().hex, category=category, genre=genre,
        function_pos=function_pos, grade_axis=grade_axis,
        intensity_scale=intensity_scale, cost_scale=cost_scale,
        rarity=rarity, notes=notes, created_at=_now())
    db.add(row)
    db.commit()
    return row


def add_skill_scale(db: Session, *, category: str, function_pos: str,
                    genre: str = "通用", grade_axis: str | None = None,
                    intensity_scale: str | None = None, cost_scale: str | None = None,
                    rarity: str = "常见", notes: str | None = None) -> GlobalSkillScaleORM:
    _check_category(category, _SKILL_CATEGORIES, "global_skill_scales")
    existing = (db.query(GlobalSkillScaleORM)
                .filter(GlobalSkillScaleORM.category == category,
                        GlobalSkillScaleORM.genre == genre,
                        GlobalSkillScaleORM.rarity == rarity)
                .first())
    if existing:
        return existing
    row = GlobalSkillScaleORM(
        id=uuid.uuid4().hex, category=category, genre=genre,
        function_pos=function_pos, grade_axis=grade_axis,
        intensity_scale=intensity_scale, cost_scale=cost_scale,
        rarity=rarity, notes=notes, created_at=_now())
    db.add(row)
    db.commit()
    return row


def inject_scales(db: Session, *, kind: str, category: str | None = None,
                  genre: str | None = None) -> list:
    """尺度注入（kind='item'|'skill'）。尺度库无 reference_only 语义（只存结构）。"""
    if kind == "item":
        q = db.query(GlobalItemScaleORM)
    elif kind == "skill":
        q = db.query(GlobalSkillScaleORM)
    else:
        raise ValueError(f"kind 非法: {kind!r}（item/skill）")
    if category:
        q = q.filter_by(category=category)
    if genre:
        q = q.filter(GlobalItemScaleORM.genre.in_([genre, "通用"])) if kind == "item" else \
            q.filter(GlobalSkillScaleORM.genre.in_([genre, "通用"]))
    return q.all()


# ---------------------------------------------------------------------------
# E4 写作注入（docs/03 阶段E：尺度 + 通用词候选，builder.py 拼 system 时调用）
# ---------------------------------------------------------------------------
# 项目 genre 取值来自前端新建对话框（玄幻/都市/悬疑/历史/科幻/言情/武侠/其他），
# 条目库 genre 池是（仙侠/历史/高武/通用）——两套词表不一致，这里做家族映射：
# 修真家族（玄幻/仙侠/武侠）→ 仙侠池；历史 → 历史池；其余题材只吃通用池。
# inject_* 本就会把「通用」并入（genre.in_([genre, "通用"])），此处只补家族归属。
_GENRE_FAMILY = {"玄幻": "仙侠", "仙侠": "仙侠", "武侠": "仙侠", "高武": "高武", "历史": "历史"}


def _resolve_pool_genre(genre: str | None) -> str:
    return _GENRE_FAMILY.get((genre or "").strip(), "通用")


def _scale_line(r) -> str:
    parts = [f"功能位={r.function_pos}"]
    if (r.grade_axis or "").strip():
        parts.append(f"品阶={r.grade_axis}")
    if (r.intensity_scale or "").strip():
        parts.append(f"强度={r.intensity_scale}")
    if (r.cost_scale or "").strip():
        parts.append(f"代价={r.cost_scale}")
    head = f"{r.category}（{r.rarity}）"
    return f"  {head}：{'；'.join(parts)}"


def build_injection_block(db: Session, *, genre: str | None,
                          per_category: int = 8) -> str | None:
    """E4 写作注入块：通用词候选（按类别只给名字）+ 类别尺度 + 使用要求。

    - 只走 inject_*（硬纪律②继承：reference_only / disabled 天然不可见）；
    - 候选只给名字不重复 brief（块要进 system，控体积；口径以条目库为准）；
    - 条目全空返回 None，调用方不拼空块；
    - 题材家族映射见 _GENRE_FAMILY（玄幻项目吃仙侠池，都市等只吃通用池）。
    """
    pool = _resolve_pool_genre(genre)
    by_cat: dict[str, list[str]] = {}
    for cat in sorted(_ITEM_CATEGORIES):
        rows = inject_items(db, category=cat, genre=pool, limit=per_category)
        if rows:
            by_cat[f"物品·{cat}"] = [r.name for r in rows]
    for cat in sorted(_SKILL_CATEGORIES):
        rows = inject_skills(db, category=cat, genre=pool, limit=per_category)
        if rows:
            by_cat[f"技能·{cat}"] = [r.name for r in rows]
    scales = inject_scales(db, kind="item", genre=pool) + inject_scales(db, kind="skill", genre=pool)
    if not by_cat and not scales:
        return None
    lines = ["【物品/技能·通用词参考——有候选就别硬造】"]
    if by_cat:
        lines.append("■ 写到的物品/技能若在下面名单里，**直接用这个名字**，别另起名：")
        for key in sorted(by_cat):
            lines.append(f"  {key}：" + "、".join(by_cat[key]))
    if scales:
        lines.append("■ 确需造新东西时，按这类别的尺度来（避免千篇一律）：")
        lines.extend(_scale_line(r) for r in scales)
    lines.append(
        "■ 要求：①候选名单里的通用词优先使用；②独创时名字要像这个类别"
        "（丹药以「丹」收尾、剑术含「剑」），能力按上面尺度设计；"
        "③本块是取名/造物的参考，不必在正文里提及或罗列，按剧情自然取用。")
    return "\n".join(lines)


def stats(db: Session) -> dict:
    """库容概览（管理页/报告用）。"""
    return {
        "items": db.query(GlobalItemORM).count(),
        "skills": db.query(GlobalSkillORM).count(),
        "item_scales": db.query(GlobalItemScaleORM).count(),
        "skill_scales": db.query(GlobalSkillScaleORM).count(),
        "items_reference_only": db.query(GlobalItemORM)
            .filter(GlobalItemORM.reference_only == True).count(),  # noqa: E712
        "skills_reference_only": db.query(GlobalSkillORM)
            .filter(GlobalSkillORM.reference_only == True).count(),  # noqa: E712
    }


# ---------------------------------------------------------------------------
# 管理页（2026-09-26 加）：列表 / 更新，供 /global-ref 路由使用
# ---------------------------------------------------------------------------
_STATUSES = {"active", "disabled"}


def _entry_dict(r) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "category": r.category,
        "genre": r.genre,
        "brief": r.brief,
        "full_desc": getattr(r, "full_desc", None),
        "aliases": r.aliases or [],
        "public_domain": bool(r.public_domain),
        "reference_only": bool(r.reference_only),
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def list_items(db: Session, *, category: str | None = None, q: str | None = None,
               status: str | None = None, genre: str | None = None,
               limit: int = 1000) -> list[dict]:
    qy = db.query(GlobalItemORM)
    if category:
        qy = qy.filter(GlobalItemORM.category == category)
    if status:
        qy = qy.filter(GlobalItemORM.status == status)
    if genre:
        qy = qy.filter(GlobalItemORM.genre == genre)
    if q:
        like = f"%{q.strip()}%"
        qy = qy.filter(or_(GlobalItemORM.name.like(like),
                           GlobalItemORM.brief.like(like)))
    rows = (qy.order_by(GlobalItemORM.category, GlobalItemORM.name)
            .limit(max(1, min(limit, 2000))).all())
    return [_entry_dict(r) for r in rows]


def list_skills(db: Session, *, category: str | None = None, q: str | None = None,
                status: str | None = None, genre: str | None = None,
                limit: int = 1000) -> list[dict]:
    qy = db.query(GlobalSkillORM)
    if category:
        qy = qy.filter(GlobalSkillORM.category == category)
    if status:
        qy = qy.filter(GlobalSkillORM.status == status)
    if genre:
        qy = qy.filter(GlobalSkillORM.genre == genre)
    if q:
        like = f"%{q.strip()}%"
        qy = qy.filter(or_(GlobalSkillORM.name.like(like),
                           GlobalSkillORM.brief.like(like)))
    rows = (qy.order_by(GlobalSkillORM.category, GlobalSkillORM.name)
            .limit(max(1, min(limit, 2000))).all())
    return [_entry_dict(r) for r in rows]


def _apply_update(row, *, kind_label: str, allowed_categories: set[str],
                  name=None, category=None, brief=None, genre=None,
                  aliases=None, status=None, reference_only=None,
                  full_desc=None) -> None:
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name 不能为空")
        if len(name) > 60:
            raise ValueError("name 超过 60 字")
        row.name = name
    if category is not None:
        _check_category(category, allowed_categories, kind_label)
        row.category = category
    if brief is not None:
        row.brief = _norm_brief(brief)
    if genre is not None:
        if genre not in _GENRES:
            raise ValueError(f"genre 非法: {genre!r}（允许值: {sorted(_GENRES)}）")
        row.genre = genre
    if aliases is not None:
        row.aliases = [a.strip() for a in aliases if a and a.strip()]
    if status is not None:
        if status not in _STATUSES:
            raise ValueError(f"status 非法: {status!r}（允许值: {sorted(_STATUSES)}）")
        row.status = status
    if reference_only is not None:
        row.reference_only = bool(reference_only)
    if full_desc is not None:
        fd = (full_desc or "").strip()
        if len(fd) > 500:
            raise ValueError("full_desc 超过 500 字")
        row.full_desc = fd or None
    row.updated_at = _now()


def update_item(db: Session, item_id: str, **fields) -> dict | None:
    """按 id 更新物品条目（未给的字段不动）。找不到返回 None。"""
    row = db.get(GlobalItemORM, item_id)
    if row is None:
        return None
    _apply_update(row, kind_label="global_items",
                  allowed_categories=_ITEM_CATEGORIES, **fields)
    db.commit()
    return _entry_dict(row)


def update_skill(db: Session, skill_id: str, **fields) -> dict | None:
    """按 id 更新技能条目（未给的字段不动）。找不到返回 None。"""
    row = db.get(GlobalSkillORM, skill_id)
    if row is None:
        return None
    _apply_update(row, kind_label="global_skills",
                  allowed_categories=_SKILL_CATEGORIES, **fields)
    db.commit()
    return _entry_dict(row)
