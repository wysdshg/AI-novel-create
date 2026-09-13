"""角色向量选角（Phase 7.3 ③，2026-09-13）：「模板槽位 → 本书角色」的可解释匹配。

**要解决的问题**：模板里的 cast 槽位（如「引路人师长」）是**来源书的功能位**，
计划生成时得映射到作者自己小说的真实角色。此前这步靠 LLM 在 prompt 里自行分配：
- 代称（`友·配角1`）对 AI 无意义 → 跨模板聚合时（模板 A 的配角10 与模板 B 的配角13）必然混乱；
- 结果**不可解释** —— 说不出为什么是药老而不是云韵。

**做法**：槽位向量化（`plot_template_crud` 已索引）→ 对本书角色池做**显式余弦**排序
→ 状态门过滤 → 平局判据 → 写 `plan_castings`（唯一真相源）。

三条关键算法决策（2026-09-12 讨论定稿，别再"优化"回去）：

1. **不做「出现可能性 × 相似度」的乘法**：槽位是否出现是**结构决定**的（节拍被采用=出现），
   乘法让两个误差相乘、量纲混乱。拆成两步走：①选角（人设相似度）→ ②出场校验（历史出现+节拍需要）。
2. **否决"活跃系数"乘法惩罚**：无量纲启发式标量乘到余弦上，说不出量纲含义、无法标定。
   替代（效果相同且干净）：硬门（离散状态，不参与）+ 平局次级判据（相似度接近时优先活跃的）。
3. **打分统一走显式余弦**，**不用 `SqliteVecStore.search` 的 score**：
   后端的 `score = 1/(1+L2)` **不是余弦** —— 同一 0.7 余弦在库里显示 ≈0.56，
   照 0.65~0.75 直接标定会**过严**（实际要求 cos≈0.90+）。这里两侧文本都自己 embed、
   自己在 Python 里算余弦，分数就是余弦本身，阈值可比、可标定、可解释。

**降级**：embedding 不可用（无 key / 网络失败）时**不静默失败** —— 返回空 casting 并置
`reason`，让 plan 退回"LLM 自由分配"（与 7.2 的 `origin=free` 同款思路：不硬凑）。
"""
import logging
import math
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.orm import (
    ArticlePlanORM, ChapterMemoryORM, CharacterORM, ForeshadowORM,
    PlanCastingORM, PlotTemplateORM,
)
from app.services import plot_template_crud as tpl_crud

logger = logging.getLogger(__name__)

# ---- 阈值（⚠️ 由 `docs/03 §7.3` 的标定流程产出，见 calibrate_thresholds）----
# 2026-09-13 真机标定（临时项目 6 槽位 × 8 角色显式余弦，用完即删）：
#   全局 pos_min=0.5853、neg_max=0.5956（轻微交叠，仅慕容霸在「豪爽同伴」0.596 过线）。
#   取交叠区下沿 0.58：保住全部已知正例（0.5904 中点法会卡掉 0.585 的正例，样本太小不值得）；
#   0.596 的近邻错配由平局判据 + set_casting 人工改派兜底，可接受。
# 标定原则：宁可漏匹配（走 new_chars 让作者决定）也不要错配
# （错配是"把张冠李戴的角色写进正文"，比"没选角"难发现得多）。
MIN_SCORE = 0.58        # 低于此分视为"没匹配上" → 不写 character_id（走 new_chars）
TIE_EPS = 0.03          # top1−top2 < 此值算"接近"，启用平局次级判据

# 角色状态门：默认不进候选池的状态（可显式放行 —— 回忆/幻象/夺舍是正当用法）
BLOCKED_STATUS = ("dead",)
# 状态字段校验（抽取环节可能写入脏值）
VALID_STATUS = ("alive", "dormant", "departed", "dead")

# 派生 last_seen 时的"蛰伏"判定阈值（章）。⚠️ 只能拍 —— 当前无真实长篇数据可标定分布，
# 标 ⚠️ 存疑，等 7.4 放量后再按实际分布校准。
DORMANT_GAP = 30


# ---------------------------------------------------------------------------
# 1. 角色人设向量文本
# ---------------------------------------------------------------------------
def character_profile_text(c: CharacterORM) -> str:
    """角色的**人设向量文本**（与槽位文本对齐维度）。

    🔴 字段取舍（关键）：只用 `role_type / personality / background / talent / brief /
    current_level`，**明确排除 `name`** —— 名字（"林尘"）是**无信息噪声**：
    它既不代表人设，又因为每个角色都不同而在向量里各占一个方向，纯属稀释。
    槽位文本那边同理不含代称（见 `cast_slot_text`），两边都不带名字 →
    "引路人师长"匹配的是"这人是什么样"，不是"这人叫什么"。
    """
    parts = []
    if c.role_type:
        parts.append(f"定位：{c.role_type}")
    if c.personality:
        parts.append(str(c.personality))
    if c.background:
        parts.append(str(c.background))
    if c.talent:
        parts.append(f"能力：{c.talent}")
    if c.current_level:
        parts.append(f"修为：{c.current_level}")
    if c.brief:
        parts.append(str(c.brief))
    return "｜".join(p.strip() for p in parts if p and p.strip())


def _cosine(a: list[float], b: list[float]) -> float:
    """显式余弦。两侧都来自 `embedding_client`（已 L2 归一化），零向量防御返回 0。"""
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# 2. 派生角色出场台账（纯统计，可重算）
# ---------------------------------------------------------------------------
def refresh_character_appearances(db: Session, project_id: str) -> dict:
    """从 `chapter_memories.characters` **纯派生** `last_seen_chapter` / `appearance_count`。

    为什么必须是派生而不是让 LLM 抽（7.3.5 定稿）：统计问题有唯一正确答案、能重算能验证；
    交给模型只会引入无谓漂移。**注意区分**："被人提到/回忆"**不算**出场
    （用户明确要求）—— 这里只认 `chapter_memories.characters` 里点名的。

    同名匹配：`chapter_memories.characters` 存的是**角色名字符串**（不是 id），
    所以按名字对齐。同名角色（本书内重名）会合并统计 —— 这是已知限制，
    重名本身在小说里就是需要作者消歧的问题，不在这里臆造规则。
    """
    stats: dict[str, dict] = {}
    rows = (db.query(ChapterMemoryORM)
            .filter_by(project_id=project_id)
            .order_by(ChapterMemoryORM.chapter_no.asc()).all())
    for m in rows:
        no = int(m.chapter_no or 0)
        for name in (m.characters or []):
            n = str(name).strip()
            if not n:
                continue
            s = stats.setdefault(n, {"count": 0, "last": 0})
            s["count"] += 1
            if no > s["last"]:
                s["last"] = no

    chars = db.query(CharacterORM).filter_by(project_id=project_id).all()
    updated = 0
    for c in chars:
        s = stats.get((c.name or "").strip())
        last = s["last"] if s else 0
        cnt = s["count"] if s else 0
        if c.last_seen_chapter != last or (c.appearance_count or 0) != cnt:
            c.last_seen_chapter = last or None
            c.appearance_count = cnt
            updated += 1
    if updated:
        db.commit()
    return {"characters": len(chars), "with_appearance": len(stats), "updated": updated}


def dormant_threshold(current_chapter: int, gap: int = DORMANT_GAP) -> int:
    """当前章往前推 `gap` 章 = 蛰伏判定线。返回阈值章号。"""
    return max(0, int(current_chapter) - max(1, int(gap)))


# ---------------------------------------------------------------------------
# 3. 候选池（状态门 + 人设文本）
# ---------------------------------------------------------------------------
def character_pool(db: Session, project_id: str, *,
                   include_dead: bool = False,
                   current_chapter: int | None = None) -> list[dict]:
    """本书角色的**选角候选池**（状态门过滤后）。

    - `dead`（已死）**默认不进池** —— 这是 7.3 与 7.3.5 的分界：没有这一条，
      casting 会系统性产出"第 60 章还在用第 40 章前就死掉的角色"。
      `include_dead=True` 显式放行（回忆/幻象/夺舍章节用）。
    - `departed`（离场）**保留在池里**但标 `needs_reentry_note` ——
      离场角色回归是正当剧情（失踪多年归来），要求的是"交代理由"而非"禁止出现"。
    - `dormant` 同理（久未出场，可以召回，但必须写明回归理由）。

    返回 `[{character, text, status, last_seen, appearance_count, needs_reentry_note}]`。
    """
    q = db.query(CharacterORM).filter_by(project_id=project_id)
    rows = q.all()
    out = []
    cur = int(current_chapter or 0)
    for c in rows:
        st = (c.status or "alive").strip() or "alive"
        if st not in VALID_STATUS:
            # 脏值（历史数据/抽取写坏）→ 按在世处理，但要留痕，别让它静默失效
            logger.warning(f"[casting] 角色《{c.name}》status 值非法 {st!r}，按 alive 处理")
            st = "alive"
        if st == "dead" and not include_dead:
            continue
        need_note = st in ("dormant", "departed")
        if st == "alive" and cur and c.last_seen_chapter:
            # 在世但久未出场 → 也要求回归理由（不然"这个人凭空又出现了"）
            if c.last_seen_chapter < dormant_threshold(cur):
                st_effective = "dormant"
                need_note = True
            else:
                st_effective = st
        else:
            st_effective = st
        out.append({
            "character": c,
            "text": character_profile_text(c),
            "status": st_effective,
            "last_seen": int(c.last_seen_chapter or 0),
            "appearance_count": int(c.appearance_count or 0),
            "needs_reentry_note": need_note,
        })
    return out


# ---------------------------------------------------------------------------
# 4. 平局判据（两级，只改排序不动打分）
# ---------------------------------------------------------------------------
def _cooccurrence(db: Session, project_id: str) -> dict[tuple[str, str], int]:
    """历史共现次数：同章 `chapter_memories.characters` 里同时出现的次数。

    用途：**只做平局判据**（相似度接近时，选跟场景里其他人搭过戏的）——
    不做乘法惩罚（见模块 docstring 决策 2）。
    """
    pair: dict[tuple[str, str], int] = {}
    rows = (db.query(ChapterMemoryORM)
            .filter_by(project_id=project_id)
            .with_entities(ChapterMemoryORM.characters).all())
    for (names,) in rows:
        uniq = sorted({str(n).strip() for n in (names or []) if str(n).strip()})
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                k = (uniq[i], uniq[j])
                pair[k] = pair.get(k, 0) + 1
    return pair


def _cooc(pair: dict, a: str, b: str) -> int:
    if not a or not b or a == b:
        return 0
    return pair.get(tuple(sorted((a, b))), 0)


def _rank(cands: list[dict], pair: dict, anchors: list[str]) -> list[dict]:
    """两级平局判据排序：① 与 anchors 的历史共现次数 → ② 活跃度（last_seen 更近优先）。

    **只改排序，不动打分**（决策 2 的替代方案）——
    相似度差得远时排序由分数决定；分数接近时（top1−top2 < TIE_EPS）才轮到这两个判据。
    """
    def key(c):
        # 负号 = 降序；活跃度用 last_seen（越大越活跃）
        co = max((_cooc(pair, c["character"].name, a) for a in anchors), default=0)
        return (-co, -c["last_seen"])
    return sorted(cands, key=key)


# ---------------------------------------------------------------------------
# 5. 选角主流程
# ---------------------------------------------------------------------------
def _slots_of(db: Session, template_ids: list[str]) -> list[dict]:
    """从模板取槽位清单（含 template_id 与下标，便于溯源）。"""
    out: list[dict] = []
    for tid in template_ids or []:
        t = db.query(PlotTemplateORM).filter_by(id=tid).first()
        if t is None:
            continue
        for idx, c in enumerate(tpl_crud.structure_casts(t)):
            out.append({
                "template_id": tid,
                "template_name": t.name,
                "idx": idx,
                "slot": str(c.get("slot") or "").strip(),
                "desc": str(c.get("desc") or "").strip(),
                "mode": str(c.get("mode") or "").strip(),
                "text": tpl_crud.cast_slot_text(c),
            })
    # 同一槽位名跨模板只留一条（后面靠 srcs 语义其实一致；重复只会抢同一个角色）
    seen: set[str] = set()
    uniq = []
    for s in out:
        if not s["slot"] or s["slot"] in seen:
            continue
        seen.add(s["slot"])
        uniq.append(s)
    return uniq


def cast_slots_for_plan(db: Session, project_id: str, article_id: str) -> dict:
    """读当前计划 → 取模板槽位 → 向量选角 → 写 `plan_castings`。

    返回 `{"castings": [...], "reason": str|None}`。
    `reason` 非空表示**降级**（无槽位 / 无候选池 / embedding 不可用），
    调用方应把它记进 plan，让前端能告诉作者"这篇没做选角，原因是 X"。
    """
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        return {"castings": [], "reason": "该篇还没有计划"}

    slots = _slots_of(db, plan.template_ids or [])
    if not slots:
        return {"castings": [], "reason": "模板没有 cast 槽位（老模板未凝练 cast → 退回 LLM 自由分配）"}

    pool = character_pool(db, project_id)
    usable = [c for c in pool if c["text"]]
    if not usable:
        return {"castings": [], "reason": "本书没有可向量化的角色（人设字段为空）→ 退回 LLM 自由分配"}

    # --- embedding（失败即降级，不硬凑）---
    try:
        from app.services import embedding_client
        slot_vecs = embedding_client.embed_texts([s["text"] for s in slots], db=db)
        char_vecs = embedding_client.embed_texts([c["text"] for c in usable], db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[casting] embedding 不可用，选角降级: {type(e).__name__}: {str(e)[:120]}")
        return {"castings": [], "reason": f"embedding 不可用（{type(e).__name__}）→ 退回 LLM 自由分配"}

    # 显式余弦（不用 store 的 1/(1+L2)）
    scored: list[list[dict]] = []
    for sv in slot_vecs:
        row = []
        for c, cv in zip(usable, char_vecs):
            row.append({**c, "score": round(_cosine(sv, cv), 6)})
        row.sort(key=lambda x: -x["score"])
        scored.append(row)

    # 出场校验用的锚点：本篇计划里已点名的角色（他们一定在场，"跟谁搭戏"以此为参照）
    anchors = _plan_character_names(plan)
    pair = _cooccurrence(db, project_id)

    results: list[dict] = []
    used: set[str] = set()
    for slot, row in zip(slots, scored):
        # 已被别的槽位占用的角色不再给（一个角色只能演一个功能位，否则"两槽位抢同一人"）
        free = [c for c in row if c["character"].id not in used]
        if not free:
            results.append({**slot, "character_id": None, "character_name": None,
                            "score": None, "reason": "所有候选角色已被其他槽位占用"})
            continue
        top1 = free[0]
        # 平局判据：与第二名接近时改用共现/活跃度重排（只影响接近的那批）
        tied = [c for c in free if top1["score"] - c["score"] < TIE_EPS]
        if len(tied) > 1:
            tied = _rank(tied, pair, anchors)
            top1 = tied[0]
        if top1["score"] < MIN_SCORE:
            # 不硬凑：留空 → 调用方/前端提示"这个功能位没找到合适角色，要不要新建？"
            results.append({**slot, "character_id": None, "character_name": None,
                            "score": top1["score"], "reason": "最高分低于阈值，不硬凑"})
            continue
        used.add(top1["character"].id)
        results.append({**slot,
                        "character_id": top1["character"].id,
                        "character_name": top1["character"].name,
                        "score": top1["score"],
                        "needs_reentry_note": bool(top1["needs_reentry_note"]),
                        "status": top1["status"],
                        "reason": None})

    _persist(db, plan, results)
    return {"castings": [_to_dict(r) for r in results], "reason": None}


def _plan_character_names(plan: ArticlePlanORM) -> list[str]:
    """计划里点名的角色（recall_chars + new_chars），作为共现判据的锚点。"""
    out: list[str] = []
    for ln in ((plan.plan or {}).get("lines") or []):
        for k in ("recall_chars", "new_chars"):
            for n in (ln.get(k) or []):
                s = str(n).strip()
                if s and s not in out:
                    out.append(s)
    return out


def _persist(db: Session, plan: ArticlePlanORM, results: list[dict]) -> None:
    """把选角结果写进 `plan_castings`（**保留 manual** 记录，只覆盖 auto）。"""
    old = db.query(PlanCastingORM).filter_by(plan_id=plan.id).all()
    manual = {o.slot: o for o in old if (o.source or "auto") == "manual"}
    # 先清 auto（manual 保留 —— 作者手改的选择不该被一次重算冲掉）
    db.query(PlanCastingORM).filter_by(plan_id=plan.id, source="auto").delete(
        synchronize_session=False)
    now = datetime.utcnow()
    for r in results:
        if r["slot"] in manual:
            continue        # 作者已定 → 不覆盖
        db.add(PlanCastingORM(
            id=uuid.uuid4().hex,
            plan_id=plan.id,
            project_id=plan.project_id,
            article_id=plan.article_id,
            template_id=r.get("template_id"),
            slot=r["slot"],
            slot_desc=r.get("desc") or None,
            slot_mode=r.get("mode") or None,
            character_id=r.get("character_id"),
            character_name=r.get("character_name"),
            score=r.get("score"),
            source="auto",
            needs_reentry_note=bool(r.get("needs_reentry_note")),
            created_at=now,
            updated_at=now,
        ))
    db.commit()


def _to_dict(r: dict) -> dict:
    return {
        "slot": r["slot"],
        "slot_desc": r.get("desc") or "",
        "slot_mode": r.get("mode") or "",
        "template_id": r.get("template_id"),
        "template_name": r.get("template_name"),
        "character_id": r.get("character_id"),
        "character_name": r.get("character_name"),
        "score": r.get("score"),
        "status": r.get("status"),
        "needs_reentry_note": bool(r.get("needs_reentry_note")),
        "reason": r.get("reason"),
    }


# ---------------------------------------------------------------------------
# 6. 读取 / 手改 / 清理
# ---------------------------------------------------------------------------
def list_castings(db: Session, project_id: str, article_id: str) -> list[dict]:
    """读该篇当前计划的 casting（唯一真相源 = 本表）。"""
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        return []
    rows = (db.query(PlanCastingORM).filter_by(plan_id=plan.id)
            .order_by(PlanCastingORM.slot).all())
    return [{
        "id": o.id, "slot": o.slot, "slot_desc": o.slot_desc, "slot_mode": o.slot_mode,
        "template_id": o.template_id, "character_id": o.character_id,
        "character_name": o.character_name, "score": o.score, "source": o.source,
        "needs_reentry_note": bool(o.needs_reentry_note),
    } for o in rows]


def set_casting(db: Session, project_id: str, article_id: str, *,
                slot: str, character_id: str | None) -> dict:
    """作者手改某个槽位的选角（改完标 `source="manual"`，重算不被覆盖）。

    ⚠️ **只动本表，不碰 `plan.json`** —— 避开 7.2 踩过的 JSON 列共享引用坑
    （读出来改完写回，SQLAlchemy 值比较判定 not dirty → UPDATE 被静默跳过）。
    （详见 `orm.PlanCastingORM`）。"""
    plan = (db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id)
            .order_by(ArticlePlanORM.updated_at.desc()).first())
    if plan is None:
        raise RuntimeError("该篇还没有计划")
    row = db.query(PlanCastingORM).filter_by(plan_id=plan.id, slot=slot).first()
    if row is None:
        raise RuntimeError(f"该篇没有槽位「{slot}」")
    if character_id:
        c = db.query(CharacterORM).filter_by(project_id=project_id, id=character_id).first()
        if c is None:
            raise RuntimeError(f"角色不存在（project 内查无此 id）: {character_id}")
        if (c.status or "alive") == "dead":
            # 已死角色**默认拒绝手改进来**（与状态门一致）；要放行必须先改角色状态，
            # 逼作者显式承认"我要让他出场"，而不是在选角界面里偷偷绕过。
            raise RuntimeError(f"角色《{c.name}》状态为「已死」—— 如需出场请先改角色状态")
        row.character_id = c.id
        row.character_name = c.name
        row.score = None            # 手改不产生分数，避免"看起来像算出来的"
    else:
        row.character_id = None
        row.character_name = None
        row.score = None
    row.source = "manual"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"slot": row.slot, "character_id": row.character_id,
            "character_name": row.character_name, "source": row.source}


def delete_for_plan(db: Session, plan_id: str) -> int:
    """清掉某计划的全部 casting（重算/删计划时调）。"""
    return db.query(PlanCastingORM).filter_by(plan_id=plan_id).delete(synchronize_session=False)


# ---------------------------------------------------------------------------
# 7. 出场校验（只过滤/标记，不参与打分）
# ---------------------------------------------------------------------------
def check_appearances(db: Session, project_id: str, lines: list[dict]) -> dict:
    """计划行的**出场校验**（确定性，零 LLM）。

    两件事：
    1. **硬拦截**：`已死` 角色出现在任何一行的 `recall_chars` → 剔除并报告
       （与 7.2 "查无此人剔除"同一位置、同一风格）；
    2. **软提示**：`蛰伏/离场` 角色被召回 → 标记该行需要回归理由
       （7.3.5 会在这里挂"回归理由材料包"，本步先只标记）。

    刻意**不参与打分** —— 打分只答"这个人设像不像这个功能位"，
    "他现在能不能出现"是另一个问题（决策 1：两步走，不乘法）。
    """
    dead_names = {c.name for c in db.query(CharacterORM)
                  .filter_by(project_id=project_id).all()
                  if (c.status or "alive") == "dead" and c.name}
    status_of = {c.name: (c.status or "alive") for c in db.query(CharacterORM)
                 .filter_by(project_id=project_id).all() if c.name}
    blocked: list[str] = []
    need_note: list[dict] = []
    for ln in lines or []:
        kept = []
        for n in (ln.get("recall_chars") or []):
            s = str(n).strip()
            if s in dead_names:
                blocked.append(f"第{ln.get('no')}章：{s}")
                continue
            if status_of.get(s) in ("dormant", "departed"):
                need_note.append({"line": ln.get("no"), "name": s, "status": status_of[s]})
            kept.append(n)
        ln["recall_chars"] = kept
    return {"blocked_dead": blocked, "needs_reentry_note": need_note}


# ---------------------------------------------------------------------------
# 7.5 回归理由材料包（7.3.5，确定性预取，零 LLM）
# ---------------------------------------------------------------------------
def reentry_material(db: Session, project_id: str, character_id: str, *,
                     max_digest: int = 20) -> dict:
    """为「蛰伏/离场角色被召回」预取**回归理由材料**（docs/03 §7.3.5 定稿）。

    三步全是确定性查询（**不走 FC** —— 把确定性任务交给不确定性组件是同一个坑）：
    1. **谁需要材料**：调用方已按 `needs_reentry_note` 筛过；
    2. **他哪章消失**：台账 `last_seen_chapter`（派生字段，可重算可验证）；
    3. **缺席期材料**，按方案认可的**来源优先级**：
       ① 未回收伏笔（`related_ids` 挂了本角色，或描述里点名）—— 把"编理由"变成
         "收伏笔"，埋-收结构天然成立，几乎零额外成本；
       ② 缺席期**世界线事件**（章节记忆里点名本角色的章 = 他缺席时世界在发生什么）
         + 逐章摘要 digest（封顶 `max_digest` 条防爆炸）；
       ③ 两者皆无 → `fallback`（纯新编，质量最差，如实标注不伪装）。

    ⚠️ "被人提到/回忆"不算出场（用户定稿）—— 但**正因为不算出场**，缺席期里
    提到他的章节恰恰是"世界线还挂着他"的证据，作为回归材料正合适。
    """
    c = db.query(CharacterORM).filter_by(id=character_id,
                                         project_id=project_id).first()
    if c is None:
        return {}
    last = c.last_seen_chapter

    # ① 未回收伏笔：related_ids 挂了本角色，或描述里点名（两路都查，互为补充）
    foreshadows: list[dict] = []
    try:
        rows = (db.query(ForeshadowORM)
                .filter_by(project_id=project_id, enabled=True)
                .filter(ForeshadowORM.status != "resolved").all())
        for f in rows:
            if character_id in (f.related_ids or []) or \
                    (c.name and c.name in (f.description or "")):
                foreshadows.append({"id": f.id, "description": f.description,
                                    "buried_chapter": f.buried_chapter})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[casting] 伏笔预取失败: {type(e).__name__}: {e}")

    # ② 缺席期：实体命中章（世界线事件）+ 逐章摘要 digest
    world_events: list[dict] = []
    arc_digest: list[dict] = []
    try:
        q = db.query(ChapterMemoryORM).filter_by(project_id=project_id)
        if last is not None:
            q = q.filter(ChapterMemoryORM.chapter_no > int(last))
        mems = q.order_by(ChapterMemoryORM.chapter_no.asc()).all()
        for m in mems:
            s = m.summary or ""
            if c.name and (c.name in (m.characters or []) or c.name in s):
                world_events.append({"chapter_no": m.chapter_no, "title": m.title,
                                     "summary": s[:200]})
            if len(arc_digest) < max_digest:
                arc_digest.append({"chapter_no": m.chapter_no, "summary": s[:120]})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[casting] 缺席期预取失败: {type(e).__name__}: {e}")

    priority = "foreshadow" if foreshadows else (
        "world_events" if world_events else "fallback")
    return {
        "character_id": c.id,
        "name": c.name,
        "status": c.status or "alive",
        "last_seen_chapter": last,
        "foreshadows": foreshadows,
        "world_events": world_events[:10],
        "arc_digest": arc_digest,
        "priority": priority,
    }


# ---------------------------------------------------------------------------
# 8. 阈值标定（临时项目 + 带人设角色；用完删）
# ---------------------------------------------------------------------------
def calibrate_thresholds(db: Session, project_id: str, slots: list[dict], *,
                         positives: dict[str, list[str]],
                         negatives: dict[str, list[str]] | None = None) -> dict:
    """用**带人设的角色**测出「正例最低分 / 负例最高分 / 分位数」，产出标定表。

    `positives`: {槽位名: [应该匹配上的角色名]}; `negatives`: {槽位名: [不该匹配上的角色名]}。
    返回 `{per_slot: [...], suggestion: {min_score}}`。

    ⚠️ 为什么必须用临时项目：生产库 `characters` 只有 2 个角色、人设字段全空 →
    **没有可向量化的角色池**，匹配效果与阈值都无从实测（见 docs/03 §7.3「阈值标定前提」）。
    """
    pool = character_pool(db, project_id, include_dead=True)
    by_name = {c["character"].name: c for c in pool if c["text"]}
    usable = [c for c in pool if c["text"]]
    if not usable:
        return {"per_slot": [], "suggestion": None, "reason": "无可向量化角色"}

    try:
        from app.services import embedding_client
        slot_vecs = embedding_client.embed_texts([s["text"] for s in slots], db=db)
        char_vecs = embedding_client.embed_texts([c["text"] for c in usable], db=db)
    except Exception as e:  # noqa: BLE001
        return {"per_slot": [], "suggestion": None,
                "reason": f"embedding 不可用: {type(e).__name__}"}

    per_slot = []
    pos_min = None
    neg_max = None
    for slot, sv in zip(slots, slot_vecs):
        scores = {c["character"].name: round(_cosine(sv, cv), 6)
                  for c, cv in zip(usable, char_vecs)}
        p = [scores[n] for n in (positives.get(slot["slot"]) or []) if n in scores]
        n_ = [scores[n] for n in ((negatives or {}).get(slot["slot"]) or []) if n in scores]
        rec = {"slot": slot["slot"],
               "pos_min": min(p) if p else None, "pos_max": max(p) if p else None,
               "neg_max": max(n_) if n_ else None, "neg_min": min(n_) if n_ else None,
               "top3": sorted(scores.items(), key=lambda kv: -kv[1])[:3]}
        per_slot.append(rec)
        if p:
            pos_min = min(p) if pos_min is None else min(pos_min, min(p))
        if n_:
            neg_max = max(n_) if neg_max is None else max(neg_max, max(n_))

    suggestion = None
    if pos_min is not None:
        # 建议阈值取「正例最低分」与「负例最高分」的中点；只有正例时用正例最低分的 0.9 折
        suggestion = round((pos_min + neg_max) / 2, 4) if neg_max is not None \
            else round(pos_min * 0.9, 4)
    return {"per_slot": per_slot, "suggestion": {"min_score": suggestion},
            "pos_min": pos_min, "neg_max": neg_max}
