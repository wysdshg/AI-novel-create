"""篇规划服务（Phase 7.2，2026-09-11）：「篇 = 规划单元」的落地。

流程：作者创建篇时给口述（可空）→ 检索模板 + 装配本书上下文 → LLM 出**章节级计划**
（`chapter_plan`：每章的 beat / 要点 / 新角色 / 召回角色 / 目标字数 / 章尾钩子）
→ 作者**逐行修改或 AI 只改一行**（`refine_line`）→ 拍板（`confirm`）
→ 生成时由 `layers.layer_chapter_plan` 把「本章任务」注入上下文。

三条防幻觉闸门（生成计划时）：
- `recall_chars`（召回老角色）→ 后端到 `characters` 表校验，查无此人即剔除；
- `new_chars`（引新角色）→ 只进 `planned_chars`，拍板后走既有的待确认实体机制，不直接写角色库；
- 未找到模板 → `origin="free"`（自由规划），不硬凑。

Phase 7.3.5（2026-09-13）新增两块，全部**零 LLM 成本**：
- **新角色引入链路**：`_run_planned_chars` 把 `new_chars` 落成 `plan_chars` 表的
  pending 引入单（限额 ≤3、首登场行号自动填），作者确认后建卡进角色库；
- **篇间交接差集**：`carryover_check` 取"上一次写作位置"最后 3 章的角色并集，
  与本篇计划引用做差集 → **警告**（非报错），让作者三选一（交代离场/安排出场/忽略）。

模型：DeepSeek V4.1 Flash（`thinking disabled`）—— 计划是强语义任务（升档），调用次数少。
"""
import copy
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.orm import (
    ArticlePlanORM, ArticleORM, ChapterMemoryORM, ChapterORM,
    CharacterORM, ForeshadowORM, PlannedCharORM, VolumeORM,
)
from app.services.plot_import import _ds_post, ds_key, make_usage_cb, parse_json_loose
from app.services import casting_crud
from app.services import plot_template_crud as tpl_crud

logger = logging.getLogger(__name__)

# 一篇新角色上限（docs/03 §7.3.5 约束②：LLM 没有成本感，不设限会一篇造五个）
PLANNED_CHAR_LIMIT = 3


# ---------------------------------------------------------------------------
# 上下文装配（精简版：口述 + 模板 + 卷/前情/角色/伏笔）
# ---------------------------------------------------------------------------
def _book_context(db: Session, project_id: str, article_id: str) -> dict:
    """收集规划所需的本书上下文（全部容错：缺什么跳什么）。"""
    ctx: dict = {"volume_summary": "", "prev_arc": "", "characters": [], "foreshadows": []}
    try:
        art = db.query(ArticleORM).filter_by(id=article_id).first()
        if art is not None:
            ctx["article_title"] = art.name or ""
            vol = db.query(VolumeORM).filter_by(id=art.volume_id).first() if art.volume_id else None
            if vol is not None:
                ctx["volume_summary"] = (vol.summary or "")[:500]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 卷上下文缺失: {type(e).__name__}: {e}")
    try:
        # ⚠️ 必须排序 + 过滤（Phase 7.3 ③，2026-09-13 修）：
        # 原实现是裸的 `characters.limit(40)` —— **无排序、无筛选**，导致死角色、
        # 失踪角色、三百章没出现过的角色，与主角**平等地**进规划上下文
        # （docs/03 §7.3.5 实测记录：`plan_crud.py:49-50`）。
        # 现在：已死角色剔除；其余按「最近出场」倒序（活跃角色优先占 40 个名额）。
        chars = (db.query(CharacterORM)
                 .filter_by(project_id=project_id)
                 .filter(CharacterORM.status != "dead")
                 .order_by(CharacterORM.last_seen_chapter.desc().nullslast(),
                           CharacterORM.created_at.asc())
                 .limit(40).all())
        ctx["characters"] = [c.name for c in chars if c.name]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 角色上下文缺失: {type(e).__name__}: {e}")
    try:
        fos = (db.query(ForeshadowORM)
               .filter_by(project_id=project_id)
               .limit(15).all())
        ctx["foreshadows"] = [
            f"{f.description}（埋于第{f.buried_chapter or '?'}章，状态 {f.status or '—'}）"
            for f in fos if f.description
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 伏笔上下文缺失: {type(e).__name__}: {e}")
    try:
        prev_ch = (db.query(ChapterORM)
                   .filter_by(project_id=project_id)
                   .order_by(ChapterORM.chapter_no.desc())
                   .first())
        if prev_ch is not None and prev_ch.content:
            ctx["prev_arc"] = (prev_ch.content or "")[-800:]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 前情上下文缺失: {type(e).__name__}: {e}")
    return ctx


def _templates_for_plan(db: Session, hint: str) -> tuple[list[dict], list[str]]:
    """按口述检索模板（top-2）；检索不可用/无结果 → 空列表（自由规划）。"""
    try:
        r = tpl_crud.search(db, query=hint or "", top_k=2)
        return r["items"], [t["id"] for t in r["items"]]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 模板检索失败: {type(e).__name__}: {e}")
        return [], []


def _plan_prompt(ctx: dict, templates: list[dict], hint: str,
                 n_chapters: int) -> str:
    """构造规划 prompt。

    🔴 **模板注入瘦身（2026-09-13，成本优化）**：模板只送「骨架」——
    `name / logline / rhythm` + 各 phase 的 **beat 名**，**不送 variants 明细与 cast 明细**。
    原因（实测）：一个模板的 `structure` JSON 有 4062~6620 字符，variants 占了绝大部分，
    top-2 检索就是 8000+ 字符/次；而规划阶段**只需要知道"有哪些节拍可选"**，
    "某本书在这个节拍上怎么处理的"对规划毫无用处（那是凝练阶段的输入）。
    瘦身后模板块从 8000+ 字符降到数百字符量级，属于纯浪费的减法。

    保留 `beat 名` 是刻意的：模型要靠节拍名去填 `template_ref` 字段，这一层不能砍。
    """
    t_blocks = []
    for t in templates:
        st = t.get("structure") or {}
        phase_lines = []
        for ph in st.get("phases") or []:
            beats = "、".join(b.get("beat") or "—" for b in (ph.get("beats") or []))
            phase_lines.append(f"  · {ph.get('phase')}：{beats}")
        t_blocks.append(
            f"《{t['name']}》—— {t.get('logline') or ''}（节奏 {t.get('rhythm') or '—'}）\n"
            + "\n".join(phase_lines)
        )
    t_text = "\n\n".join(t_blocks) if t_blocks else "（无匹配模板，请根据口述与上下文自由设计本篇结构）"

    ctx_lines = [
        f"篇名：{ctx.get('article_title') or '（未命名）'}",
        f"卷概要：{ctx.get('volume_summary') or '（无）'}",
        f"本书角色（可召回）：{'、'.join(ctx.get('characters') or []) or '（暂无）'}",
        f"未回收伏笔：{'；'.join(ctx.get('foreshadows') or []) or '（无）'}",
        f"最近正文结尾：…{(ctx.get('prev_arc') or '')[-400:]}",
    ]

    return (
        f"你是网文结构师。请为本篇规划**逐章计划**（共 {n_chapters} 章），"
        "每章是一个「场」：有冲突、有推进、有结尾钩子。\n\n"
        "🔴 **反抄袭铁律（最高优先级）**：下面的模板内容**只借鉴节拍结构**（冲突如何升级、"
        "爽点如何释放），**严禁照抄其表层内容**——模板原文中出现的任何人名、地名、宗门/势力名、"
        "功法/法宝/血脉名、称号与具体桥段（如某宗退婚、某玉佩传承），一律不得出现在你的计划里，"
        "必须替换为自创的、符合本书世界观的名称与设定。输出中若出现源书专名视为不合格。\n\n"
        "🎭 **选角规则**：模板中的代称（如 友·配角1、敌·宗门2、主角）是**来源书的槽位编号**，"
        "不代表具体角色，**不要在你的计划中使用这些代称**。请把每个节拍实际需要的角色，"
        "映射到本书真实角色（从下方角色列表中选，如 主角→本书主角名），没有合适的就放 "
        "`new_chars` 标为新角色。计划的 `recall_chars`/`new_chars` 一律写**本书真实角色名**。\n\n"
        f"作者口述（最高优先级，必须尊重）：{hint or '（未提供，参考模板与上下文设计）'}\n\n"
        f"【本书上下文】\n" + "\n".join(ctx_lines) +
        f"\n\n【候选模板（借鉴结构，不要照抄；与口述冲突时以口述为准）】\n{t_text}\n\n"
        "输出 JSON（不要 markdown 代码块、不要任何额外说明）：\n"
        '{"lines":[{"no":1,"beat":"节拍名（4~8字）","summary":"本章剧情要点（60~120字，'
        '说清冲突与结果）","new_chars":["可引新角色"],"recall_chars":["召回老角色"],'
        '"target_words":3000,"hook":"章尾钩子（15字内）","template_ref":"借鉴的模板节拍"}],'
        '"notes":"给作者的整体说明（50字内）"}'
    )


# ---------------------------------------------------------------------------
# 生成 / 修改 / 拍板
# ---------------------------------------------------------------------------
def _valid_lines(lines) -> list[dict]:
    out = []
    for x in lines or []:
        try:
            no = int(x.get("no"))
        except (TypeError, ValueError):
            continue
        if not (x.get("summary") or x.get("beat")):
            continue
        out.append({
            "no": no,
            "beat": str(x.get("beat") or "").strip(),
            "summary": str(x.get("summary") or "").strip(),
            "new_chars": [str(c) for c in (x.get("new_chars") or [])],
            "recall_chars": [str(c) for c in (x.get("recall_chars") or [])],
            "target_words": int(x.get("target_words") or 3000),
            "hook": str(x.get("hook") or "").strip(),
            "template_ref": str(x.get("template_ref") or "").strip(),
        })
    out.sort(key=lambda x: x["no"])
    return out


def generate_plan(db: Session, project_id: str, article_id: str, *,
                  hint: str = "", n_chapters: int = 8,
                  force_free: bool = False) -> dict:
    """生成本篇的章节级计划（覆盖该篇的旧 draft）。"""
    key = ds_key(db)
    if not key:
        raise RuntimeError("未配置 DeepSeek Key（app_configs.llm.deepseek_key）")

    ctx = _book_context(db, project_id, article_id)
    templates, t_ids = ([], []) if force_free else _templates_for_plan(db, hint)
    origin = "template" if templates else "free"

    raw = _ds_post(key, _plan_prompt(ctx, templates, hint, n_chapters),
                   max_tokens=6000, on_usage=make_usage_cb("ds_plan"))
    data = parse_json_loose(raw) or {}
    lines = _valid_lines(data.get("lines"))
    if not lines:
        raise RuntimeError(f"计划生成失败：模型未输出有效行；raw 前 200 字: {raw[:200]!r}")

    # 防幻觉闸门：召回角色必须在 characters 表里真实存在
    valid_names = {c.name for c in db.query(CharacterORM)
                   .filter_by(project_id=project_id).all() if c.name}
    unknown: list[str] = []
    for ln in lines:
        kept = []
        for name in ln["recall_chars"]:
            if name in valid_names:
                kept.append(name)
            else:
                unknown.append(name)
        ln["recall_chars"] = kept
    if unknown:
        logger.warning(f"[plan] 召回角色查无此人（已剔除）: {unknown}")

    # 出场校验（Phase 7.3 ③）：**必须在落库前跑** —— 它会从 recall_chars 里剔除
    # 已死角色；放落库后跑的话，剔除结果只改了本地列表、DB 里仍是脏数据。
    blocked_dead: list[str] = []
    try:
        check = casting_crud.check_appearances(db, project_id, lines)
        blocked_dead = check.get("blocked_dead") or []
        if blocked_dead:
            logger.warning(f"[plan] 已死角色被从召回列表剔除: {blocked_dead}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 出场校验失败（不影响计划）: {type(e).__name__}: {e}")

    # 同一篇只保留一条当前计划：旧 draft 直接覆盖；confirmed 的会归档进 raw_ai 历史
    old = (db.query(ArticlePlanORM)
           .filter_by(project_id=project_id, article_id=article_id)
           .order_by(ArticlePlanORM.updated_at.desc()).first())
    raw_ai = {"last": raw}
    if old is not None and old.status == "confirmed":
        hist = (old.raw_ai or {}).get("history") or []
        hist.append({"plan": old.plan, "archived_at": datetime.utcnow().isoformat()})
        raw_ai["history"] = hist[-5:]

    if old is not None:
        old.template_ids = t_ids
        old.template_names = [t["name"] for t in templates]
        old.plan = {"lines": lines, "notes": str(data.get("notes") or "")}
        old.origin = origin
        old.raw_ai = raw_ai
        old.status = "draft"
        old.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(old)
        return _finalize_plan(db, project_id, article_id, old, lines,
                              unknown, templates, blocked_dead)

    o = ArticlePlanORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        article_id=article_id,
        template_ids=t_ids,
        template_names=[t["name"] for t in templates],
        plan={"lines": lines, "notes": str(data.get("notes") or "")},
        origin=origin,
        raw_ai=raw_ai,
        status="draft",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return _finalize_plan(db, project_id, article_id, o, lines,
                          unknown, templates, blocked_dead)


def _finalize_plan(db: Session, project_id: str, article_id: str,
                   plan: ArticlePlanORM, lines: list[dict],
                   unknown: list[str], templates: list[dict],
                   blocked_dead: list[str]) -> dict:
    """落库后的三连增强（全部容错、绝不拖垮主业务）：
    向量选角（7.3③）→ 新角色落地（7.3.5）→ 篇间交接差集 + 回归材料预取（7.3.5）。
    carryover / reentry_materials 同时**存进 plan JSON**（拍板/前端读计划时可见）。
    """
    casting = _run_casting(db, project_id, article_id, lines)
    planned = _run_planned_chars(db, plan, lines, casting.get("castings"))

    cast_names = [c["character_name"] for c in (casting.get("castings") or [])
                  if c.get("character_name")]
    carry = carryover_check(db, project_id, article_id, lines, cast_names)
    reentry = _reentry_for_plan(db, project_id, casting.get("castings") or [])

    if carry.get("carryover_names") or reentry:
        # ⚠️ 深拷贝断开共享引用（7.2 踩坑：new==old → UPDATE 被静默跳过）
        plan.plan = copy.deepcopy({
            "lines": lines,
            "notes": (plan.plan or {}).get("notes") or "",
            "carryover": carry,
            "reentry_materials": reentry,
        })
        plan.updated_at = datetime.utcnow()
        db.commit()
    return {"plan_id": plan.id, "lines": len(lines), "unknown_chars": unknown,
            "templates": [t["name"] for t in templates],
            "blocked_dead": blocked_dead, "carryover": carry,
            **casting, **planned}


def _reentry_for_plan(db: Session, project_id: str,
                      castings: list[dict]) -> list[dict]:
    """对「蛰伏/离场却被选中」的角色做**回归理由材料包**（7.3.5，确定性预取，零 LLM）。

    谁需要材料 casting 已算（needs_reentry_note）；哪章消失台账已算（last_seen）；
    缺席期材料 = ①未回收伏笔（收伏笔优于凭空编）②缺席期世界线事件（实体命中）。
    三步全是确定性查询 —— 用 FC 等于把确定性任务交给不确定性组件（docs/03 定稿）。
    """
    out: list[dict] = []
    for c in castings:
        if not c.get("needs_reentry_note") or not c.get("character_id"):
            continue
        try:
            m = casting_crud.reentry_material(db, project_id, c["character_id"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[plan] 回归材料预取失败 {c.get('character_name')}: "
                           f"{type(e).__name__}: {e}")
            continue
        if m:
            m["slot"] = c.get("slot")
            out.append(m)
    return out


def _run_casting(db: Session, project_id: str, article_id: str,
                 lines: list[dict]) -> dict:
    """计划落库后跑**向量选角**（Phase 7.3 ③，2026-09-13）。

    出场校验（死角色剔除）已在落库前跑过（见 generate_plan）—— 这里只做选角。
    选角**失败即降级**（无槽位/无角色池/embedding 挂了）→ 返回 `casting_reason`
    让前端能说清"这篇为什么没选角"，而不是静默无输出。
    整个函数**绝不抛异常**：选角是增强能力，不能让它把计划生成（主业务）带崩。
    """
    out: dict = {"casting_reason": None, "castings": []}
    try:
        r = casting_crud.cast_slots_for_plan(db, project_id, article_id)
        out["castings"] = r.get("castings") or []
        out["casting_reason"] = r.get("reason")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 向量选角失败（不影响计划）: {type(e).__name__}: {e}")
        out["casting_reason"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


# ---------------------------------------------------------------------------
# 新角色引入链路 + 篇间交接（Phase 7.3.5 B 档，2026-09-13，全零 LLM 成本）
# ---------------------------------------------------------------------------
def _run_planned_chars(db: Session, plan: ArticlePlanORM, lines: list[dict],
                       castings: list[dict] | None = None) -> dict:
    """把计划里的 `new_chars` 落成 `plan_chars` 表的 **pending 引入单**。

    - 限额 `PLANNED_CHAR_LIMIT`（≤3）：按**首次出现顺序**保留，超出的剔除并告警；
    - `first_appearance` 自动填 = 首次出现的行为篇内章号（约束③的数据基础）；
    - 重生成/行编辑后重落时**只清 pending 行**：作者已确认（confirmed，角色已建卡）
      或已忽略（dismissed）的意志不因重算而蒸发，同名跳过；
    - `unmatched_slots`：casting 里没配上角色的功能位 —— 与新角色是**同一条链**
      （槽位没匹配到 → 作者在计划页"新建角色 / 改选现有角色"），但**不自动猜绑定**。
    整个函数**绝不抛异常**（与 _run_casting 同款纪律：增强能力不拖垮主业务）。
    """
    out: dict = {"planned_chars": [], "new_chars_dropped": [],
                 "unmatched_slots": [], "planned_chars_reason": None}
    try:
        # 1. 收集（按首次出现顺序，去重）
        seen: dict[str, int] = {}
        for ln in lines:
            for nm in (ln.get("new_chars") or []):
                s = str(nm).strip()
                if s and s not in seen:
                    seen[s] = int(ln.get("no") or 0)
        keep = dict(list(seen.items())[:PLANNED_CHAR_LIMIT])
        out["new_chars_dropped"] = list(seen)[PLANNED_CHAR_LIMIT:]

        # 2. 旧行：pending 清掉重落；confirmed/dismissed 保留（同名跳过）
        kept_names: set[str] = set()
        for o in db.query(PlannedCharORM).filter_by(plan_id=plan.id).all():
            if (o.status or "pending") == "pending":
                db.delete(o)
            else:
                kept_names.add(o.name)
        now = datetime.utcnow()
        for name, no in keep.items():
            if name in kept_names:
                continue
            db.add(PlannedCharORM(
                id=uuid.uuid4().hex,
                plan_id=plan.id,
                project_id=plan.project_id,
                article_id=plan.article_id,
                name=name,
                first_appearance=no,
                status="pending",
                created_at=now,
                updated_at=now,
            ))
        db.commit()

        # 3. casting 的 unmatched 槽位（给计划页做"新建 / 改选"的关联提示）
        for c in (castings or []):
            if not c.get("character_id") and c.get("slot"):
                out["unmatched_slots"].append(
                    {"slot": c["slot"], "slot_desc": c.get("slot_desc") or ""})

        out["planned_chars"] = [_planned_to_dict(o) for o in
                                db.query(PlannedCharORM).filter_by(plan_id=plan.id)
                                .order_by(PlannedCharORM.first_appearance.asc()).all()]
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning(f"[plan] 新角色落地失败（不影响计划）: {type(e).__name__}: {e}")
        out["planned_chars_reason"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def _planned_to_dict(o: PlannedCharORM) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "slot": o.slot,
        "slot_desc": o.slot_desc,
        "first_appearance": o.first_appearance,
        "status": o.status,
        "character_id": o.character_id,
    }


def refresh_planned_chars(db: Session, project_id: str, article_id: str) -> dict:
    """行编辑（save_lines/refine_line）后重落 pending 引入单（保持与 lines 同步）。"""
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        return {"planned_chars": [], "new_chars_dropped": [],
                "unmatched_slots": [], "planned_chars_reason": None}
    lines = (plan.plan or {}).get("lines") or []
    return _run_planned_chars(db, plan, lines)


def list_planned_chars(db: Session, project_id: str, article_id: str) -> list[dict]:
    """读该篇当前计划的引入单（含 confirmed/dismissed —— 作者的决策历史要可见）。"""
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        return []
    rows = (db.query(PlannedCharORM)
            .filter_by(plan_id=plan.id)
            .order_by(PlannedCharORM.first_appearance.asc()).all())
    return [_planned_to_dict(o) for o in rows]


def update_planned_char(db: Session, project_id: str, article_id: str, pc_id: str, *,
                        slot: str | None = None,
                        slot_desc: str | None = None,
                        first_appearance: int | None = None,
                        status: str | None = None) -> dict:
    """作者调整引入单：绑槽位 / 改首登场章 / 忽略（dismiss）。"""
    o = db.query(PlannedCharORM).filter_by(id=pc_id, project_id=project_id,
                                           article_id=article_id).first()
    if o is None:
        raise RuntimeError("引入单不存在")
    if slot is not None:
        o.slot = str(slot).strip() or None
    if slot_desc is not None:
        o.slot_desc = str(slot_desc).strip() or None
    if first_appearance is not None:
        o.first_appearance = int(first_appearance)
    if status is not None:
        if status not in ("pending", "dismissed", "confirmed"):
            raise RuntimeError(f"未知状态 {status}")
        o.status = status
    o.updated_at = datetime.utcnow()
    db.commit()
    return _planned_to_dict(o)


def confirm_planned_char(db: Session, project_id: str, article_id: str, pc_id: str, *,
                         attrs: dict | None = None) -> dict:
    """作者确认引入单 → 真正建卡进 `characters`（补上断掉的一环）。

    复用写后摄取确认的同一套写入纪律：**重名跳过**（同名即同人，重复建卡会让
    后续注入出现两份互相矛盾的设定）。建卡成功回链 `character_id`、置 confirmed。
    `first_appearance` 保持篇内行号 —— 全局章号等该章真写完由 last_seen 派生，不猜。
    """
    from app.schemas.database import CharacterCreate
    from app.services import character_crud

    o = db.query(PlannedCharORM).filter_by(id=pc_id, project_id=project_id,
                                           article_id=article_id).first()
    if o is None:
        raise RuntimeError("引入单不存在")
    if o.status == "confirmed" and o.character_id:
        return {"character_id": o.character_id, "name": o.name, "status": "confirmed",
                "skipped": "已确认过（幂等）"}
    a = attrs or {}
    exists = (db.query(CharacterORM)
              .filter_by(project_id=project_id, name=o.name).first())
    if exists is not None:
        # 同名角色已在库 —— 不重复建卡，直接回链确认
        o.status = "confirmed"
        o.character_id = exists.id
        o.updated_at = datetime.utcnow()
        db.commit()
        return {"character_id": exists.id, "name": o.name, "status": "confirmed",
                "skipped": "同名角色已存在（已直接回链）"}
    fields = {
        "name": o.name,
        "role_type": str(a.get("role_type") or "配角"),
        "personality": str(a.get("personality") or ""),
        "background": str(a.get("background") or ""),
        "talent": str(a.get("talent") or ""),
        "current_level": str(a.get("current_level") or ""),
        "brief": str(a.get("brief") or (f"补「{o.slot}」功能位的新角色"
                                        if o.slot else "")),
    }
    ch = character_crud.create_character(db, project_id, CharacterCreate(**fields))
    o.status = "confirmed"
    o.character_id = ch.id
    o.updated_at = datetime.utcnow()
    db.commit()
    return {"character_id": ch.id, "name": o.name, "status": "confirmed"}


def carryover_check(db: Session, project_id: str, article_id: str,
                    lines: list[dict], casting_names: list[str]) -> dict:
    """**篇间交接差集**（7.3.5 B 档，零 LLM）：警告非报错。

    1. 定位"上一次写作位置"：**非本篇**的全局最大 `chapter_no` 章节记忆
       （网文顺序写作，全局最新章 ≈ 上一篇末尾；排除本篇防自指）；
    2. 取其往前共 3 章记忆的 `characters` 并集 = **上场遗留名单**；
    3. 差集 = 遗留名单 − 本篇引用（recall_chars ∪ new_chars ∪ casting 选角）
       → 名单里的人本篇"人间蒸发" → 列出警告，让作者三选一：
       交代离场 / 安排出场 / 忽略。**绝不阻塞流程**。
    """
    out: dict = {"carryover_names": [], "last_chapters": [], "from_article_id": None}
    try:
        mems = (db.query(ChapterMemoryORM)
                .filter_by(project_id=project_id)
                .filter(or_(ChapterMemoryORM.article_id.is_(None),
                            ChapterMemoryORM.article_id != article_id))
                .order_by(ChapterMemoryORM.chapter_no.desc())
                .limit(3).all())
        if not mems:
            return out
        out["last_chapters"] = [m.chapter_no for m in mems]
        out["from_article_id"] = mems[0].article_id
        legacy: list[str] = []
        for m in mems:
            for n in (m.characters or []):
                s = str(n).strip()
                if s and s not in legacy:
                    legacy.append(s)
        referred: set[str] = set(casting_names or [])
        for ln in lines:
            for k in ("recall_chars", "new_chars"):
                for n in (ln.get(k) or []):
                    s = str(n).strip()
                    if s:
                        referred.add(s)
        out["carryover_names"] = [n for n in legacy if n not in referred]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 篇间交接校验失败（不影响计划）: {type(e).__name__}: {e}")
    return out


def refine_line(db: Session, project_id: str, article_id: str, *,
                line_no: int, instruction: str) -> dict:
    """AI 只改一行（行级局部修改）：其余行原样保留。"""
    key = ds_key(db)
    if not key:
        raise RuntimeError("未配置 DeepSeek Key（app_configs.llm.deepseek_key）")
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        raise RuntimeError("该篇还没有计划（先跑 --stage plan 或调生成接口）")
    # ⚠️ 这里必须 deep copy：直接拿 plan.plan["lines"] 会拿到**同一个列表对象**，
    # 后续对 lines 的任何修改都会同步"污染"SQLAlchemy 记住的旧值 ——
    # 赋新值时 new==old，UPDATE 被跳过（2026-09-11 实测踩坑，refine 返回正确但库纹丝不动）。
    lines = copy.deepcopy((plan.plan or {}).get("lines") or [])
    target = next((x for x in lines if int(x.get("no") or 0) == int(line_no)), None)
    if target is None:
        raise RuntimeError(f"计划里没有第 {line_no} 行")
    neighbors = [x for x in lines if x is not target]
    prompt = (
        f"这是本篇的章节计划，需要修改第 {line_no} 行（只改这一行，输出**修改后的完整 JSON 行**）。\n"
        f"修改要求：{instruction}\n\n"
        f"【当前这一行】\n{json.dumps(target, ensure_ascii=False)}\n"
        f"【上下文（只读参考，不要输出）】\n"
        + json.dumps(neighbors[:6], ensure_ascii=False)[:1500]
        + "\n\n输出 JSON（单行对象，字段与输入相同）："
    )
    raw = _ds_post(key, prompt, max_tokens=800, on_usage=make_usage_cb("ds_refine"))
    data = parse_json_loose(raw) or {}
    data.setdefault("no", line_no)
    fixed = _valid_lines([data])
    if not fixed:
        raise RuntimeError(f"改行失败：模型输出无效；raw 前 150 字: {raw[:150]!r}")
    new_line = fixed[0]
    new_line["no"] = line_no
    for i, x in enumerate(lines):
        if int(x.get("no") or 0) == int(line_no):
            lines[i] = new_line
            break
    # ⚠️ 深拷贝：lines 与 plan.plan["lines"] 是**同一个列表对象**，SQLAlchemy 对 JSON 列
    # 用值比较判断是否 dirty —— 不拷贝的话新值==旧值（共享列表已同步变化），UPDATE 会被跳过
    # （2026-09-11 实测踩坑：refine 返回正确但库纹丝不动）。
    plan.plan = copy.deepcopy({"lines": lines, "notes": (plan.plan or {}).get("notes") or ""})
    plan.updated_at = datetime.utcnow()
    db.commit()
    # 7.3.5：行变了，pending 引入单同步重落（confirmed/dismissed 不动）
    planned = refresh_planned_chars(db, project_id, article_id)
    return {"line": new_line, "origin": plan.origin,
            "new_chars_dropped": planned.get("new_chars_dropped") or []}


def confirm_plan(db: Session, project_id: str, article_id: str) -> dict:
    """作者拍板：draft → confirmed（此后生成会注入本章任务）。"""
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        raise RuntimeError("该篇还没有计划")
    plan.status = "confirmed"
    plan.updated_at = datetime.utcnow()
    db.commit()
    return {"plan_id": plan.id, "status": plan.status, "lines": len((plan.plan or {}).get("lines") or [])}


def get_plan(db: Session, project_id: str, article_id: str) -> dict | None:
    """取当前计划（含行列表）。"""
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        return None
    lines = (plan.plan or {}).get("lines") or []
    return {
        "id": plan.id,
        "project_id": plan.project_id,
        "article_id": plan.article_id,
        "template_ids": plan.template_ids or [],
        "template_names": plan.template_names or [],
        "origin": plan.origin,
        "status": plan.status,
        "notes": (plan.plan or {}).get("notes") or "",
        "lines": lines,
    }


def save_lines(db: Session, project_id: str, article_id: str, *,
               lines: list[dict], notes: str | None = None) -> dict:
    """作者在表格里改完提交（行级编辑的保存动作）。"""
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        raise RuntimeError("该篇还没有计划")
    fixed = _valid_lines(lines)
    # 同 refine_line：深拷贝断开与旧 JSON 值的共享引用（否则 UPDATE 被跳过）
    plan.plan = copy.deepcopy(
        {"lines": fixed, "notes": notes if notes is not None else (plan.plan or {}).get("notes") or ""})
    plan.updated_at = datetime.utcnow()
    db.commit()
    # 7.3.5：行变了，pending 引入单同步重落（confirmed/dismissed 不动）
    planned = refresh_planned_chars(db, project_id, article_id)
    return {"plan_id": plan.id, "lines": len(fixed),
            "new_chars_dropped": planned.get("new_chars_dropped") or []}


def upsert_draft_for_article(db: Session, project_id: str, article_id: str) -> dict:
    """确认前校验：篇必须存在。"""
    art = db.query(ArticleORM).filter_by(id=article_id).first()
    if art is None:
        raise RuntimeError("篇不存在")
    return {"article_id": art.id, "name": art.name}
