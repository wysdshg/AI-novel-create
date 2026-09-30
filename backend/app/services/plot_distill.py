"""情节模板凝练（Phase 7.1 步骤 5，2026-09-11）。

**上游**：`plot_import` 已把小说切成 章 → 情节段（beat）→ 故事弧（arc）。
**本模块**：把「相似的弧」凝练成**可复用模板**（`plot_templates`），供篇规划时借鉴。

流程（对应"AI 出候选组 + 用户确认"原则）：
  1. `cluster_arcs`  跨书聚类：用 bge-m3 向量算弧概括相似度，把"像同一个套路"的弧分到一组
     （= AI 候选组）；
  2. `distill_template`  对一组弧交给 DeepSeek 凝练成模板结构
     （phase → beat → **variants[各书的不同走法]**）；
  3. `distill_all` 串起来：聚类 → 逐组凝练 → 全部入库（`status="draft"` 待人工审核）。

**为什么 variants 是灵魂**：单个弧只能给"一种走法"，而模板的价值在于"这个节拍别人还有哪几种走法" ——
一组来自不同书的相似弧，恰好提供了同一节拍的多种处理方式（src 记来源书名）。

**Phase 7.3 ① cast（2026-09-12）**：模板除结构外还产出 **cast（人格化角色槽位）**，
供篇规划做向量选角。**定稿 B 方案：cast 记「功能槽位」+ `srcs` 溯源到各书代称**，
与 `variants[{src, how}]` 同构 —— 直接记来源书代称（A 方案）在跨书聚成一组时同一功能位会重复
（斗破「友·配角4」与修真「友·配角2」都是引路人 → 两槽位抢同一角色），且编号随重聚类漂移。

**人工确认**：本模块产出的一律是 `draft`；审核通过由人改 `status="reviewed"`（或删除）。
"""
import hashlib
import json
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.orm import BookAliasORM, ChapterSummaryORM, PlotTemplateORM
from app.services import app_config
from app.services import plot_template_crud as tpl_crud
from app.services import vector_index
from app.services.anonymizer import anonymize_text
from app.services.plot_import import (DS_KEY_CONFIG, _ds_post, ds_key,
                                      make_usage_cb, parse_json_loose)

logger = logging.getLogger(__name__)

# 弧聚类的相似度阈值（余弦，同一套路的不同书实现通常 0.80+；阈值取保守值防误并）
CLUSTER_THRESHOLD = 0.80

# ---- cast（人格化角色槽位）常量（Phase 7.3 ①）----
# 只有这些 kind 会被当作「角色槽位」送进凝练：势力/家族按职能位参与选角（如"敌对宗门长老"）。
# place/item/realm 是背景物，与"选角"无关（realm 更是境界阶梯，混进来只会污染向量匹配）。
CAST_KINDS = ("protagonist", "role", "sect", "family", "force")
CAST_MAX = 6        # 每模板 cast 上限（不列全部代称，否则模板爆炸）
CAST_HINT_MAX = 12  # 送进 LLM 的候选槽位上限（控 token；单书 role 类可达 77 条）

_ALIAS_RE_CACHE: dict[str, re.Pattern] = {}


def _alias_in_text(alias: str, text: str) -> bool:
    """代称是否在文本中出现（**带数字边界**）。

    ⚠️ 必须有负向断言：`友·配角1` 是 `友·配角10` 的前缀，直接 `in` 会误判
    （实测斗破 role 类 77 条，编号到两位数很常见）。
    """
    if not alias or not text:
        return False
    pat = _ALIAS_RE_CACHE.get(alias)
    if pat is None:
        pat = re.compile(re.escape(alias) + r"(?!\d)")
        _ALIAS_RE_CACHE[alias] = pat
    return pat.search(text) is not None


def _book_cast(db: Session, book_name: str) -> dict:
    """取一本书的角色槽位（`role_desc` **已即时匿名化**）+ 匿名化映射。

    为什么必须匿名化：`role_desc` 是**在原文上抽取**的，实测直接含源书专名
    （"…称主角为萧炎哥哥""云岚宗宗主"）→ 原样带进模板 cast 会打破已通过的
    「反抄袭门槛：源书专名命中 0」。**只改内存里的文本，不动 `book_aliases` 库值**
    （要留作反向还原的原料 —— 见 `anonymize_text` 文档）。

    返回三个键：
    - `slots`：只含 `CAST_KINDS`（选角要的槽位）。
    - `mapping`：**该书全部 kind** 的 (原名 → 代称)。
    - `mapping_strong`：`mapping` 去掉 `item` —— 见下。

    ⚠️ **`mapping` 必须是全 kind（2026-09-12 实测修 bug）**：首版只把 `CAST_KINDS` 的映射
    拿去替换，结果 `role_desc` 里 **item/place/realm 类专名全部漏网**（实测 18 处：
    清风观 / 紫晶源 / 斗之气 / 魔兽山脉 / 炼药师公会 …），照样带进了模板的 cast。

    ⚠️ **`mapping_strong` 剔除 `item` 的理由**：item 抽取噪声大，实录把「丹药」「长剑」
    「玉佩」这类**普通名词**也注册成了专名；对这类词做替换会把可读文本变成
    "提供关键友·物品5与技术支援"，**反而污染向量语义**。place/realm/force 是书级专名，
    必须替换；item 的漏网在 `verify` 里作为**软提示**报告，不作为失败判据。
    """
    rows = db.query(BookAliasORM).filter_by(book_name=book_name).all()
    mapping = [{"original": r.original, "alias": r.alias} for r in rows if r.original]
    mapping_strong = [m for m, r in zip(mapping, rows) if r.kind != "item"]
    slots = [{
        "alias": r.alias,
        "desc": anonymize_text(r.role_desc or "", mapping),
        "kind": r.kind,
        "relation": r.relation,
        "first_chapter": r.first_chapter or 0,
    } for r in rows if r.alias and r.kind in CAST_KINDS]
    return {"slots": slots, "mapping": mapping, "mapping_strong": mapping_strong}


def _pick_cast_for_arc(arc: dict, book_cast: dict) -> list[dict]:
    """从整本书的槽位里挑出**本弧真的用到**的那些（确定性预筛，省 token）。

    命中判据：代称字面出现在弧概括或任一 beat 的标签/概括里 —— 匿名化后概括里
    写的就是代称文本（如"敌·配角2 前来退婚"），所以字面命中是可靠的。
    主角恒在：每个套路都有主角，LLM 更需要知道"本书主角的代称是哪个"。
    """
    text = (arc.get("summary") or "") + "".join(
        f"{b.get('label') or ''}{b.get('summary') or ''}" for b in (arc.get("beats") or []))
    hit = [s for s in book_cast["slots"]
           if s["kind"] == "protagonist" or _alias_in_text(s["alias"], text)]
    if not hit:
        return book_cast["slots"][:CAST_HINT_MAX]
    if len(hit) > CAST_HINT_MAX:
        # 主角优先，其余按**首现章号**升序（先出场者更可能是主线角色）
        hit = sorted(hit, key=lambda s: (s["kind"] != "protagonist", s["first_chapter"]))
        logger.info(f"[plot_distill] 弧《{arc.get('book')}#{arc.get('arc_no')}》"
                    f"候选槽位过多，截到 {CAST_HINT_MAX}")
        hit = hit[:CAST_HINT_MAX]
    return hit


# ---- L2 性格 × 位阶（2026-09-16 立项，见 docs/08-C8 与 outputs/角色性格维度设计-草案.md）----
# 12 条双极维度；刻度 -10~+10，提示词只准用 9 个锚点。
TRAIT_DIMS: dict[str, str] = {
    "altruism": "利他↔自私", "honor": "信义↔背信", "mercy": "仁慈↔狠辣",
    "resolve": "坚毅↔易摧", "decisiveness": "果决↔犹豫", "discipline": "自律↔放纵",
    "risk": "冒险↔稳健", "rationality": "理性↔冲动", "guile": "城府↔直率",
    "idealism": "理想↔务实", "warmth": "热忱↔冷漠", "dominance": "强势↔随和",
}
TRAIT_ANCHORS = (0, 1, 4, 7, 10)      # 允许的**正向量级**（吸附与提示词都只给这些档）
_ANCHORS_SIGNED = tuple(sorted({0} | {s * a for a in TRAIT_ANCHORS if a for s in (1, -1)}))
TRAIT_INFER_MAX = 4                    # **推断值**的幅度上限（±7/±10 必须有文本依据）


def _normalize_traits(raw_traits, raw_basis) -> tuple[dict, dict, dict]:
    """校验并归一 LLM 产出的 `traits` / `basis`。返回 `(traits, basis, report)`。

    规则（见设计草案 §七）：
    - 只认 `TRAIT_DIMS` 里的 12 个键；**越界夹紧到 ±10**；非整数取整；
    - **缺键补 0**（不是失败——但计入 report 供观测，正常应当 12 键齐全）；
    - `basis` 只保留 `text|slot|peer`；缺省即 text；
    - 🔴 **越权推断**：`basis != text` 的维度若 |值| > `TRAIT_INFER_MAX`（4），夹紧到 ±1 并计入 report
      —— 极端档只能来自文本依据，否则整库反派会长成一个样（`03 §8.3` 的"稳定地雷同"）。
    """
    report = {"clamped": [], "missing": [], "over_inferred": [], "omitted": False}
    # **非人槽位允许整体省略**（2026-09-16 用户拍板）：组织/势力/群体没有"性格"，
    # 硬填会退化成"全 +1"（实测「接纳主角的新势力」12 维全 +1，在数值检索里是噪声）。
    if not raw_traits or not isinstance(raw_traits, dict):
        report["omitted"] = True
        return {}, {}, report
    basis_in = raw_basis if isinstance(raw_basis, dict) else {}
    out: dict[str, int] = {}
    out_basis: dict[str, str] = {}
    for k in TRAIT_DIMS:
        v = (raw_traits or {}).get(k) if isinstance(raw_traits, dict) else None
        if v is None:
            out[k] = 0
            report["missing"].append(k)
        else:
            try:
                iv = int(round(float(v)))
            except (TypeError, ValueError):
                iv = 0
                report["missing"].append(k)
            if abs(iv) > 10:
                report["clamped"].append(f"{k}={iv}")
                iv = max(-10, min(10, iv))
            out[k] = iv
        b = str(basis_in.get(k) or "text").strip().lower()
        b = b if b in ("text", "slot", "peer") else "text"
        if b != "text":
            if abs(out[k]) > TRAIT_INFER_MAX:
                report["over_inferred"].append(f"{k}={out[k]}({b})→±1")
                out[k] = 1 if out[k] > 0 else -1
            out_basis[k] = b
    return out, out_basis, report


# ---------------------------------------------------------------------------
# L2 角色画像：**零调用**聚合（2026-09-16 用户拍板，方案 A）
# ---------------------------------------------------------------------------
def _snap_anchor(v: float) -> int:
    """把数值**吸附到最近的锚点**（并列时取绝对值小的，即偏保守）。

    🔴 为什么必须吸附：观测**偶数个**时 `statistics.median` 会取中间两值的均值 →
    可能落在档位之间（实测 `[4,-4] → 0`、`[1,4] → 2.5`），
    那样就破坏了「档位可比」（检索时 +2 与 +4/0 是什么关系？）。

    ⚠️ `TRAIT_ANCHORS` 只存**正向量级**，这里展开成 ± 全集（否则 −7 会被吸成 0 ——
    实测踩过：单条观测 −7 结果返回 0，负值全被吃掉）。
    """
    cands = _ANCHORS_SIGNED
    return min(cands, key=lambda a: (abs(v - a), abs(a)))


def _median_profile(observations: list) -> tuple[dict, dict]:

    """把同一角色的多次 traits 观测聚合成一个画像（纯函数，可单测）。

    - 逐维取**中位数**：抗"局部带偏"（一直无恶不作、某篇做了两件好事 → 中位**不会翻正**）；
      且观测值都落在 9 档锚点上 → **中位数仍是锚点**，保持"档位可比"
      （平均会造出 +3.7 这种档间值，检索时无法与锚点比较）。
    - 返回 `(traits, meta)`；`meta["spread"]` 记逐维极差（大 = 该维度观测不稳，
      可能是**角色弧光**、也可能是噪声；区分二者需要"弧序"，属模型版能力）。
    - 观测为空/无效 → `({}, {})`（调用方据此**不写**，避免空值覆盖）。
    """
    import statistics as _st
    sets = [t for t in observations if isinstance(t, dict) and t]
    if not sets:
        return {}, {}
    traits: dict[str, int] = {}
    spread: dict[str, int] = {}
    for k in TRAIT_DIMS:
        vals = [int(t[k]) for t in sets if isinstance(t.get(k), (int, float))]
        if not vals:
            continue
        traits[k] = _snap_anchor(_st.median(vals))     # 偶数个观测的均值会离档 → 吸附回锚点
        rng = max(vals) - min(vals)
        if rng:
            spread[k] = rng
    return traits, {"n": len(sets), "spread": spread}


def aggregate_profiles(db: Session, book_name: str | None = None) -> dict:
    """**零调用**：把「同一角色在多个模板里被 AI 打的 traits」聚合为角色画像，写回 `book_aliases`。

    🔴 为什么必须独立成层（而不是"凝练时顺手做"）：凝练调用的视野是**一组相似弧**，
    而同一个角色会出现在多个组里 → 每次只看到一块碎片 → 逐组打分**必然漂移**
    （实测：九星「主角」在 9 个模板里拿到 9 套分，12 维极差合计 40、平均每维差 3.3 档）。
    角色画像的属性是「**这个角色在本书里是什么人**」，**与模板如何分组无关** → 独立、可重算。

    幂等：可反复跑；没有任何观测的角色**不写空值**（不覆盖已有画像）。
    """
    from app.models.orm import BookAliasORM, PlotTemplateORM   # 懒加载，避免顶层耦合
    q = db.query(BookAliasORM)
    if book_name:
        q = q.filter(BookAliasORM.book_name == book_name)
    aliases = q.all()
    tpls = db.query(PlotTemplateORM).all()

    obs: dict[tuple, list] = {}
    src: dict[tuple, list] = {}
    for t in tpls:
        for c in ((t.structure or {}).get("cast") or []):
            tr = c.get("traits")
            if not isinstance(tr, dict) or not tr:
                continue
            for s in (c.get("srcs") or []):
                key = (str(s.get("book") or ""), str(s.get("alias") or ""))
                obs.setdefault(key, []).append(tr)
                src.setdefault(key, []).append(t.name)

    written = skipped = 0
    for a in aliases:
        key = (a.book_name, a.alias)
        got = obs.get(key) or []
        if not got:
            skipped += 1
            continue                      # **无观测 → 一律不动**（绝不拿空值覆盖已有画像）
        traits, meta = _median_profile(got)
        if not traits:
            continue
        meta["templates"] = sorted(set(src.get(key) or []))[:20]
        a.traits, a.traits_meta = traits, meta
        written += 1
    db.commit()
    logger.info(f"[plot_distill] 角色画像聚合（零调用）：扫描 {len(aliases)} 个角色 → "
                f"写入 {written} 个，无观测跳过 {skipped} 个；模板池 {len(tpls)} 个")
    return {"aliases": len(aliases), "written": written, "skipped": skipped,
            "templates_scanned": len(tpls)}



def _normalize_cast(raw, arcs: list[dict], db: Session) -> list[dict]:
    """校验并归一 LLM 产出的 cast（LLM 会编槽位、编溯源、写源书专名）。

    - `srcs` 里的 `(book, alias)` 必须**真实存在于该书 book_aliases**，否则该条 src 丢弃；
    - 一个槽位的 srcs 全非法 → **整个槽位丢弃**（宁可少一个槽位，也不能让幽灵槽位进选角）；
    - **`desc` 再过一遍匿名化**（用各书 `mapping_strong` 的并集）：LLM 写 desc 时会自然带出
      源书的物品/地名/境界（实测 18 处：清风观 / 紫晶源 / 斗之气 / 炼药师公会…），
      这里做确定性兜底替换，而不是指望 prompt 拦得住；
    - slot 名去重（同一功能位只能出现一次）、限长、上限 `CAST_MAX`。
    """
    if not isinstance(raw, list):
        return []
    books = sorted({a["book"] for a in arcs})
    allowed: dict[str, set[str]] = {}
    desc_of: dict[tuple[str, str], str] = {}
    strong: list[dict] = []
    for b in books:
        bc = _book_cast(db, b)
        allowed[b] = {s["alias"] for s in bc["slots"]}
        strong.extend(bc["mapping_strong"])
        for s in bc["slots"]:
            desc_of[(b, s["alias"])] = s["desc"]

    out: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()[:24]
        if not slot or slot in seen:
            continue
        srcs = []
        for s in (item.get("srcs") or []):
            if not isinstance(s, dict):
                continue
            book = str(s.get("book") or "").strip()
            alias = str(s.get("alias") or "").strip()
            if book in allowed and alias in allowed[book]:
                srcs.append({"book": book, "alias": alias})
            else:
                dropped += 1
        # 2026-09-18 修正：srcs 全非法时**保留槽位、srcs 置空**（原"整个槽位丢弃"会把单弧
        # 凝练的 cast 清空——实测太荒 68 条模板 cast 全 0）。槽位价值=功能位+位阶+12维性格，
        # 溯源缺失只影响"来自哪本书"，不该让整个槽位消失。跨书凝练同理受益。
        if not srcs:
            logger.warning(f"[plot_distill] 槽位「{slot}」溯源非法（LLM 编的代称），保留槽位但 srcs 置空")
        beats = [str(x).strip()[:40] for x in (item.get("beats") or []) if str(x).strip()][:8]
        desc = str(item.get("desc") or "").strip()
        if desc:
            desc = anonymize_text(desc, strong)      # LLM 写的 desc 也要过匿名化
        elif srcs:
            # 回填「该代称在本书的原始描述」。⚠️ 必须判 srcs 非空：上面 2026-09-18 的
            # "保留槽位、srcs 置空"修正让 srcs 可能为空，此处若不判会 IndexError
            # （槽位被静默丢弃，整组凝练记 failed）—— 见 04 记录。
            desc = desc_of.get((srcs[0]["book"], srcs[0]["alias"]), "")
        else:
            desc = ""       # 槽位保留但无溯源、LLM 也没写 desc → 留空，不猜
        # L2：位阶 + 性格刻度（2026-09-16）
        ranks = [str(x).strip()[:24] for x in (item.get("ranks") or []) if str(x).strip()][:5]
        ranks = [anonymize_text(r, strong) for r in ranks]      # 位阶也可能被写出专名（如"云岚宗宗主"）
        traits, basis, trep = _normalize_traits(item.get("traits"), item.get("basis"))
        if trep["missing"] or trep["over_inferred"]:
            logger.info(f"[plot_distill] 槽位「{slot}」traits 归一："
                        f"缺 {trep['missing']}｜越权推断 {trep['over_inferred']}")
        out.append({
            "slot": slot,
            "desc": desc[:300],   # 2026-09-17 放开：60 → 300（cast 描述是推断性格/位阶的关键输入）
            "mode": str(item.get("mode") or "").strip()[:12],
            "ranks": ranks,
            "traits": traits,
            "basis": basis,
            "beats": beats,
            "srcs": srcs,
        })
        seen.add(slot)
        if len(out) >= CAST_MAX:
            break
    if dropped:
        logger.warning(f"[plot_distill] cast 丢弃 {dropped} 条非法 src"
                       f"（代称不在该书映射表里 —— LLM 编的溯源）")
    return out


# ---------------------------------------------------------------------------
# 1. 收集弧明细（含 beat 级细节）
# ---------------------------------------------------------------------------
def collect_arcs(db: Session, book_names: list[str] | None = None) -> list[dict]:
    """收集弧及其 beat 明细。

    返回 `[{book, arc_no, name, summary, beats: [{label, summary, chapters}], cast_candidates}]`
    `book_names=None` 表示全部书。

    `cast_candidates`（Phase 7.3 ①）是**该弧用到的角色槽位**（含匿名化后的 `role_desc`），
    由 `_book_cast` 联查 `book_aliases` + `_pick_cast_for_arc` 确定性预筛得到，
    供 `_distill_prompt` 送进凝练。按书缓存，避免逐弧重复查库。
    """
    q = db.query(ChapterSummaryORM).filter(ChapterSummaryORM.arc_no.isnot(None))
    if book_names:
        q = q.filter(ChapterSummaryORM.book_name.in_(book_names))
    rows = q.order_by(ChapterSummaryORM.book_name, ChapterSummaryORM.chapter_no).all()

    buckets: dict[tuple[str, int], dict] = {}
    for r in rows:
        key = (r.book_name, r.arc_no)
        b = buckets.setdefault(key, {
            "book": r.book_name,
            "arc_no": r.arc_no,
            "name": r.arc_name or f"弧{r.arc_no}",
            "summary": r.arc_summary or "",
            "segments": {},
        })
        if r.arc_name:
            b["name"] = r.arc_name
        if r.arc_summary:
            b["summary"] = r.arc_summary
        s = b["segments"].setdefault(r.segment_no, {
            "label": r.plot_label or "", "summary": r.segment_summary or "", "chapters": []})
        s["chapters"].append(r.chapter_no)
        if r.segment_summary:
            s["summary"] = r.segment_summary
        if r.plot_label:
            s["label"] = r.plot_label

    out = []
    for b in buckets.values():
        b["beats"] = [
            {"label": s["label"], "summary": s["summary"], "chapters": s["chapters"]}
            for _, s in sorted(b["segments"].items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ]
        b.pop("segments")
        out.append(b)

    # 附加 cast 候选（按书缓存一次；单本书映射表可上百条，逐弧查库会白跑 N 倍）
    cast_cache: dict[str, dict] = {}
    for a in out:
        book = a["book"]
        if book not in cast_cache:
            try:
                cast_cache[book] = _book_cast(db, book)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[plot_distill] 《{book}》槽位读取失败，本弧无 cast 候选: "
                               f"{type(e).__name__}: {e}")
                cast_cache[book] = {"slots": [], "mapping": []}
        a["cast_candidates"] = _pick_cast_for_arc(a, cast_cache[book])
    return out


# ---------------------------------------------------------------------------
# 2. 跨书聚类（AI 候选组）
# ---------------------------------------------------------------------------
def cluster_arcs(db: Session, arcs: list[dict] | None = None, *,
                 book_names: list[str] | None = None,
                 threshold: float = CLUSTER_THRESHOLD) -> list[dict]:
    """按弧概括的语义相似度聚类（贪心单遍，足够用）。

    返回候选组：[{arcs: [弧...], avg_sim, books: [书名...], suggest_name}]
    向量不可用时（未配检索 Key）→ 每个弧各成一组（**不硬报错**，退化为"每弧一模板"）。
    """
    arcs = arcs if arcs is not None else collect_arcs(db, book_names)
    if not arcs:
        return []

    vectors: list[list[float]] | None = None
    if vector_index.enabled(db):
        try:
            from app.services import embedding_client
            texts = [f"{a['name']}。{a['summary']}" for a in arcs]
            vectors = embedding_client.embed_texts(texts, db=db)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[plot_distill] 弧向量化失败，退化为每弧一组: {type(e).__name__}: {e}")
            vectors = None

    if not vectors:
        return [{"arcs": [a], "avg_sim": None, "books": [a["book"]], "suggest_name": a["name"]}
                for a in arcs]

    def cos(x, y):
        s = sum(i * j for i, j in zip(x, y))
        nx = sum(i * i for i in x) ** 0.5
        ny = sum(j * j for j in y) ** 0.5
        return s / (nx * ny) if nx and ny else 0.0

    groups: list[dict] = []
    for i, a in enumerate(arcs):
        placed = False
        for g in groups:
            sims = [cos(vectors[i], vectors[m]) for m in g["members"]]
            if max(sims) >= threshold:
                g["members"].append(i)
                g["sims"].extend(sims)
                placed = True
                break
        if not placed:
            groups.append({"members": [i], "sims": []})

    out = []
    for g in groups:
        members = [arcs[i] for i in g["members"]]
        books = sorted({m["book"] for m in members})
        out.append({
            "arcs": members,
            "avg_sim": round(sum(g["sims"]) / len(g["sims"]), 4) if g["sims"] else None,
            "books": books,
            "suggest_name": members[0]["name"] if len(members) == 1 else f"{members[0]['name']} 等",
        })
    out.sort(key=lambda x: -len(x["arcs"]))     # 大组优先
    return out


# ---------------------------------------------------------------------------
# 3. 凝练模板（一组弧 → plot_templates 结构）
# ---------------------------------------------------------------------------
def _distill_prompt(arcs: list[dict]) -> str:
    blocks = []
    for i, a in enumerate(arcs, 1):
        beats = "\n".join(
            f"  · [{b['label'] or '—'}] 第{b['chapters'][0]}~{b['chapters'][-1]}章：{b['summary']}"
            for b in a["beats"]
        )
        cast_lines = "\n".join(
            f"  · {s['alias']}（{s['kind']}）：{s['desc'] or '—'}"
            for s in (a.get("cast_candidates") or [])
        )
        blocks.append(
            f"【弧 {i}】《{a['book']}》· {a['name']}\n弧概括：{a['summary']}\n节拍明细：\n{beats}"
            + (f"\n本弧用到的角色槽位：\n{cast_lines}" if cast_lines else "")
        )
    return (
        f"下面是从小说中提取的 {len(arcs)} 个「故事弧」（一个弧 = 一个有完整冲突升级链的故事单元）。\n"
        "请把它们凝练成**一个可复用的情节模板**，供其他小说写作时借鉴。\n\n"
        "要求：\n"
        "1. `name`：4~8 字的套路名（如 学院大比 / 秘境寻宝 / 金手指觉醒 / 家族危机）；\n"
        "2. `logline`：一句话说清这个套路的核心张力（30 字内）；\n"
        "3. `genre_tags`：2~4 个题材或场景标签；\n"
        "4. `structure`：拆成 3~5 个 `phases`（阶段，如 开局/发展/高潮/收尾），每个 phase 下有若干 `beats`（节拍）；\n"
        "   每个 beat 用 `variants` 记录**各弧在这个节拍上的不同处理方式**"
        "（`src` 填来源书名，`how` 填 **100~160 字的抽象处理**（起因/经过/结果）：🔴 不得出现任何源书人名/功法名/势力名/物品名/地名，"
        "   🔴 cast 的 `srcs.alias` 必须原样使用各弧【本弧角色槽位】给出的代称，禁止自创（自创过不了溯源校验），"
        "一律用功能词替代——主角/师长/敌对长老/某宗/关键功法/关键信物）—— 这是模板最有价值的部分；\n"
        "5. `pitfalls`：2~4 条这类套路的常见翻车点；\n"
        "6. `rhythm`：各阶段大致章数配比（如 \"2-3-3-2\"）。\n"
        "7. `cast`：**3~6 个「功能槽位」**（🔴 少于 3 个视为不合格），写清这个套路需要什么功能的角色（用于把槽位映射到\n"
        "   作者自己小说的真实角色）。每个槽位四个字段：\n"
        "   - `slot`：功能名（4~8 字），**写功能不写代称**，如 引路人师长 / 退婚的未婚妻 /\n"
        "     敌对宗门长老 / 忠心的同伴 / 高高在上的长辈；\n"
        "   - `desc`：15~30 字，说明**他与主角是什么关系、在故事里承担什么作用、大概什么来头**\n"
        "     （这是后续做角色匹配的依据，务必写功能与关系）。\n"
        "     🔴 `desc` 是**跨书复用的功能说明**，因此**不得出现任何源书专有名词**——\n"
        "     包括人名/宗门名/家族名/地名/物品名/功法名/境界名（如某宗、某玉佩、斗之气、清风观）。\n"
        "     需要提及时用功能词替代：把「某宗」写成「敌对宗门」，把物品写成「关键信物」，把境界写成「高阶修为」；\n"
        "   - `mode`：`助力` / `阻碍` / `见证` / `对手` 四选一；\n"
        "   - `ranks`：**位阶**，1~3 个短标签（如 家族高层 / 长老 / 宗门执事 / 商贾 / 散修 / 皇室）。\n"
        "     ⚠️ 位阶要填：同一个性格在不同位阶表现完全不同（「自私的长老」是结党营私，\n"
        "     「自私的散修」是偷奸耍滑），下游做「性格 × 位阶」组合判断时要靠它。不得含源书专名；\n"
        "   - `traits`：**性格刻度**，下列 12 个维度**每个都要给一个整数**。刻度 -10~+10，\n"
        "     但只允许取 **0 / ±1 / ±4 / ±7 / ±10** 这 9 个锚点（+10 = 正端拉满）：\n"
        "       利他↔自私 altruism：+10 舍己为人、为苍生赴死 ｜ -10 唯利是图、损人利己\n"
        "       信义↔背信 honor：+10 一诺千金、不出卖同伴 ｜ -10 翻脸无情、卖友求荣\n"
        "       仁慈↔狠辣 mercy：+10 得饶人处且饶人 ｜ -10 斩草除根、视人命如草芥\n"
        "       坚毅↔易摧 resolve：+10 百折不挠、越挫越强 ｜ -10 一触即溃、受挫认命\n"
        "       果决↔犹豫 decisiveness：+10 当机立断、雷厉风行 ｜ -10 优柔寡断、错失时机\n"
        "       自律↔放纵 discipline：+10 克己守戒、管得住自己 ｜ -10 纵欲无度、毫无底线\n"
        "       冒险↔稳健 risk：+10 敢赌敢拼、敢拿命换机会 ｜ -10 稳字当头、绝不冒无谓之险（此维两端无善恶）\n"
        "       理性↔冲动 rationality：+10 冷静算计、谋定后动 ｜ -10 血性上头、凭意气行事\n"
        "       城府↔直率 guile：+10 深藏不露、喜怒不形于色 ｜ -10 心直口快、藏不住事\n"
        "       理想↔务实 idealism：+10 心怀天下、有所不为 ｜ -10 只认利害、给钱就办事\n"
        "       热忱↔冷漠 warmth：+10 古道热肠、主动亲近人 ｜ -10 冷若冰霜、对谁都无感\n"
        "       强势↔随和 dominance：+10 唯我独尊、要求服从 ｜ -10 习惯退让、不争不抢\n"
        "     🔴 填法（重要，逐条照做）：\n"
        "       · 有节拍依据的维度按依据填，**不要**写进 `basis`（缺省即 text）；\n"
        "       · **文中没体现的维度不要填 0**：按「该位阶/功能位的群体倾向」或「与已填维度的顺势一致性」\n"
        "         推断，取值 **±1（最多 ±4）**，并把该维度来源写进 `basis`（\"slot\" 或 \"peer\"）；\n"
        "       · **±7 / ±10 必须有节拍文本依据**，推断一律不许给极端档；\n"
        "       · 🔴 **不许按「身份标签」成套推断**（最容易犯的错，务必逐条自查）：\n"
        "         **主角不一定是**无私 / 理性 / 仁慈的 —— 有自私的主角、冲动的主角、杀伐果断的主角；\n"
        "         **敌人不一定是**狠辣 / 背信的 —— 有讲义气、光明磊落、一诺千金的对手；\n"
        "         长辈不一定是慈祥的、商人不一定是奸诈的、宗门高层不一定是公正的……\n"
        "         **每条只按该角色在这条弧里的实际行为判断**，不要从「他是主角/反派/长老」推。\n"
        "       · 🔴 **道德三维度（利他 / 信义 / 仁慈）尤其不许用「位阶群体倾向」成套推断**：\n"
        "         这三维要么有本节拍的文本依据，要么只给 **±1 的顺势小幅推断**（并标 `peer`）；\n"
        "         只有**非道德维度**（强势 / 城府 / 理性 / 冒险 / 果决 等）才允许依据位阶倾向推。\n"
        "       · **矛盾要保留**：对家人极好却对外人极狠 = 人物立体，不要为「一致性」推平；\n"
        "       · **不许成套连锁**：自私 ≠ 必然背信、更 ≠ 必然狠辣，每维独立判断；\n"
        "       · 0 只留给「连定位都没有的角色」；\n"
        "       · **若该槽位是组织/势力/群体而不是个人**（如「接纳主角的新势力」「某宗门」），\n"
        "         `traits` **整体省略**（不要硬填——组织没有性格，硬填会变成一排 +1 噪声）；\n"
        "   - `basis`：只列**非文本来源**的维度，如 `{\"mercy\":\"peer\",\"guile\":\"slot\"}`；有依据的不用列；\n"
        "   - `beats`：该槽位主要出现在哪几个节拍（填节拍名）；\n"
        "   - `srcs`：**必须填**，从上方各弧「本弧用到的角色槽位」里挑出**对应这个功能位的代称**，\n"
        "     格式 `[{\"book\":\"书名\",\"alias\":\"友·配角4\"}]`（**照抄，不要改写、不要编造**）。\n"
        "   🔴 同一功能位**只能出现一次**：多个弧里承担同一功能的槽位（即使是不同书的、编号不同的）\n"
        "   必须**合并成一条**，把各书的代称都列进它的 `srcs`。\n\n"
        "只输出 JSON（不要 markdown 代码块、不要任何额外说明）：\n"
        '{"name":"...","logline":"...","genre_tags":["..."],'
        '"structure":{"phases":[{"phase":"...","beats":[{"beat":"...",'
        '"variants":[{"src":"书名","how":"..."}]}]}]},'
        '"cast":[{"slot":"...","desc":"...","mode":"助力","ranks":["家族高层","长老"],'
        '"traits":{"altruism":-4,"honor":7,"mercy":-1,"resolve":4,"decisiveness":1,'
        '"discipline":-1,"risk":1,"rationality":4,"guile":4,"idealism":-1,"warmth":1,"dominance":4},'
        '"basis":{"mercy":"peer"},"beats":["..."],'
        '"srcs":[{"book":"书名","alias":"代称"}]}],'
        '"pitfalls":["..."],"rhythm":"..."}\n\n'
        + "\n\n".join(blocks)
    )


def distill_template(db: Session, arcs: list[dict], *, name_hint: str | None = None,
                     status: str = "draft", max_tokens: int = 4000):
    """把一组弧凝练成一个模板并入库（含 beat 级向量化）。返回 (ORM 对象, 原始输出)。

    幂等策略：**不覆盖已有模板** —— 每次调用都新建（draft），由人工在审核时决定留哪个。
    这样"重新凝练"不会毁掉此前的人工修改。
    """
    if not arcs:
        raise ValueError("arcs 为空，无法凝练")
    key = ds_key(db)
    if not key:
        raise RuntimeError(f"未配置 DeepSeek Key（app_configs.{DS_KEY_CONFIG}）")

    # ⚠️ LLM 调用前结束 Session 事务（2026-09-14 修，记账锁的真正根因）：
    # SQLAlchemy 2.0 的 Session 是 **autobegin** —— distill_all 里 collect_arcs/cluster_arcs
    # 的 SELECT 会开一个**长读事务**并一直挂着（直到 commit/rollback）。而用量记账回调是在
    # `_ds_post` 内**同步**执行的（同线程）→ 它的独立 Session 去写 `llm_usage_logs` 时，
    # 与这个悬挂读事务撞 SQLite 锁 → 整轮记账全丢（实测 38/38 失败；光在回调里重试没用，
    # 因为主线程事务要挂到下一组才结束）。arcs 是内存 dict，rollback 不影响它们。
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        pass

    # LLM 偶发格式问题（如输出被截断）→ 重试一次，避免"偶尔一次坏输出就丢一组"
    raw, data = "", {}
    for attempt in range(2):
        raw = _ds_post(key, _distill_prompt(arcs), max_tokens=max_tokens,
                       on_usage=make_usage_cb("ds_distill"))
        data = parse_json_loose(raw) or {}
        if data.get("name") and data.get("structure"):
            break
        logger.warning(f"[plot_distill] 凝练结果不完整（第 {attempt + 1}/2 次），"
                       f"raw 前 120 字: {raw[:120]!r}")
    if not data.get("name") or not data.get("structure"):
        return None, raw

    books = sorted({a["book"] for a in arcs})
    # cast（Phase 7.3 ①）：LLM 会编槽位与溯源 → 必须校验后入库；
    # 没有任何合法槽位时不写 cast 键（而不是写空数组：空键会被下游误读成"这模板没有角色"）。
    structure = data.get("structure") or {}
    cast = _normalize_cast(data.get("cast"), arcs, db)
    if cast:
        structure = {**structure, "cast": cast}
    else:
        logger.warning(f"[plot_distill] 模板《{data.get('name')}》未产出合法 cast"
                       f"（原始 cast 条数 {(data.get('cast') or []) if isinstance(data.get('cast'), list) else 0}）")
    o = tpl_crud.create(db, {
        "name": name_hint or str(data.get("name")),
        "scale": "arc",
        "genre_tags": data.get("genre_tags") or [],
        "logline": data.get("logline"),
        "structure": structure,
        "pitfalls": data.get("pitfalls") or [],
        "rhythm": data.get("rhythm"),
        "source_stats": {
            "books": len(books),
            "book_names": books,
            "arc_refs": [f"{a['book']}#{a['arc_no']}:{a['name']}" for a in arcs],
            "cast_slots": len(cast),
        },
        "status": status,
    })
    return o, raw


# ---------------------------------------------------------------------------
# 4. 一键流程：聚类 → 逐组凝练 → 入库
# ---------------------------------------------------------------------------
def _distill_fingerprint(db: Session, book_names: list[str] | None,
                         threshold: float, min_arcs: int, min_books: int = 1) -> str:
    """凝练输入指纹（08-B8③，2026-09-16）：弧集合 + 聚类参数任一变化则指纹变。

    指纹 = sha256(书池 + threshold + min_arcs + min_books + collect_arcs 全量输出)。
    输入没变 → 重跑只会产出等价的组 → 纯烧钱 → 跳过。`default=str` 兜底日期等非 JSON 类型。
    """
    arcs = collect_arcs(db, book_names)
    payload = {
        "books": sorted(book_names) if book_names else None,
        "threshold": threshold,
        "min_arcs": min_arcs,
        "min_books": min_books,
        "arcs": arcs,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def distill_all(db: Session, *, book_names: list[str] | None = None,
                threshold: float = CLUSTER_THRESHOLD,
                min_arcs: int = 1, min_books: int = 1, replace_drafts: bool = True,
                force: bool = False) -> dict:
    """聚类全部弧 → 每组凝练一个模板 → 入库（draft）。

    `min_arcs` 可过滤太小的组（如只要"至少 2 个弧才算跨书套路"时设 2）。
    `min_books`（2026-09-17 加，用户拍板 C）：组内**至少来自几本书**才凝练 ——
    设 2 = **只出跨书套路模板**。理由：variants 的价值在"同一节拍各书的不同处理"，
    单书组（哪怕 3 条弧）写法趋同，抽象度不足；且 `--min-arcs 1` 时单书弧会灌出大量
    彼此相似的模板（实测斗破 34 弧 → 34 个模板）。**单书弧不产出，只积累**，
    等库里有相似弧时自然成组。指纹含 `min_books`（改门槛会触发重算）。
    `replace_drafts=True`（默认）会**先清掉 draft 模板再重建** —— 避免反复跑时重复堆积；
    `status="reviewed"`（人工审核过）的模板**不受影响**。

    **按池清理（2026-09-14 加）**：`book_names` 给定时，只清「来源书与本池有交集」的 draft
    —— 支持「按题材分池多次跑」各池互不干扰（否则跑历史池会把玄幻池的 draft 一起清掉）；
    `book_names=None`（全库跑）仍清全部 draft。`source_stats.book_names` 是唯一判据。
    失败单组不中断整体（记录在结果里，含原始输出片段便于排查）。

    **幂等跳过（08-B8③，2026-09-16 加）**：弧集合与聚类参数都与上次成功运行一致
    （指纹存 `app_configs: plot_distill.fingerprint.{池}`）→ 直接返回 `skipped=True`，
    **不调 LLM**。指纹在**每次跑完**后更新（含部分失败——失败组重试用 `force=True`）。
    指纹检查在一切清理/LLM 动作**之前**，避免「指纹一致却已清了 draft」的中间状态。
    """
    pool_key = "|".join(sorted(book_names)) if book_names else "all"
    fp_key = f"plot_distill.fingerprint.{pool_key}"
    fp = _distill_fingerprint(db, book_names, threshold, min_arcs, min_books)
    if not force and app_config.get(db, fp_key) == fp:
        logger.info(f"[plot_distill] 幂等跳过：凝练输入未变化（指纹一致），pool={pool_key}。"
                    f"force=True 可强制重跑")
        return {"skipped": True,
                "reason": "凝练输入未变化（弧集合与聚类参数均与上次一致）",
                "groups": 0, "created": 0, "templates": [], "failed": [],
                "fingerprint": fp}

    if replace_drafts:
        # ⚠️ 连带清向量（2026-09-13 修）：`query().delete()` 是**批量 SQL**，绕过 ORM 的
        # 对象级钩子 —— 只删行不清 `vector_chunks` 的话，每重跑一次 distill 就往全局
        # 池子里积一批**指向已不存在模板**的孤儿块（模板块 + cast 块两套）。
        # 后果不只是"浪费空间"：全局 KNN 的 top-k 名额会被这些死块挤占，
        # 而且 cast 孤儿块在选角时反查 `structure["cast"][idx]` 会直接取到错位/越界。
        pool = set(book_names) if book_names else None
        cand = db.query(PlotTemplateORM).filter_by(status="draft").all()
        if pool is not None:
            cand = [t for t in cand
                    if set((t.source_stats or {}).get("book_names") or []) & pool]
        old_ids = [t.id for t in cand]
        for tid in old_ids:
            try:
                vector_index.remove_source(db, tpl_crud.GLOBAL, tpl_crud.SOURCE_TYPE, tid)
                vector_index.remove_source(db, tpl_crud.GLOBAL, tpl_crud.SOURCE_TYPE_CAST, tid)
                vector_index.remove_source(db, tpl_crud.GLOBAL, tpl_crud.SOURCE_TYPE_ARCHETYPE, tid)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[plot_distill] 清理 draft 向量失败 id={str(tid)[:8]}: "
                               f"{type(e).__name__}: {e}")
        if old_ids:
            db.query(PlotTemplateORM).filter(
                PlotTemplateORM.id.in_(old_ids)).delete(synchronize_session=False)
            db.commit()
            logger.info(f"[plot_distill] 清理旧 draft 模板 {len(old_ids)} 个"
                        f"（reviewed 不动{'' if pool is None else '；池内来源 ' + str(sorted(pool))}）"
                        f" + 其向量块")

    arcs = collect_arcs(db, book_names)
    groups = cluster_arcs(db, arcs, threshold=threshold)
    groups = [g for g in groups if len(g["arcs"]) >= max(1, min_arcs)]
    if min_books > 1:
        before = len(groups)
        groups = [g for g in groups
                  if len({a["book"] for a in g["arcs"]}) >= min_books]
        logger.info(f"[plot_distill] min_books={min_books}：{before} 组 → {len(groups)} 组"
                    f"（单书组不产出，只积累；--min-books 1 可改回）")

    created, failed = [], []
    for g in groups:
        try:
            o, raw = distill_template(db, g["arcs"])
            if o is None:
                failed.append({"group": g["suggest_name"],
                               "reason": f"JSON 不完整；raw={(raw or '')[:100]!r}"})
                continue
            created.append({
                "id": o.id, "name": o.name, "books": g["books"],
                "arcs": len(g["arcs"]), "avg_sim": g["avg_sim"],
            })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[plot_distill] 组「{g['suggest_name']}」凝练失败: "
                           f"{type(e).__name__}: {e}")
            failed.append({"group": g["suggest_name"], "reason": f"{type(e).__name__}: {e}"})
    # 跑完就更新指纹（含部分失败）——「这套输入已经跑过了」；失败组要重试请用 force=True
    app_config.set_value(db, fp_key, fp)
    return {"groups": len(groups), "created": len(created),
            "templates": created, "failed": failed}


# ---------------------------------------------------------------------------
# 5. 审核报告（人工确认 draft 模板的主要窗口）
# ---------------------------------------------------------------------------
def export_template_report(db: Session, out_path: str | None = None,
                           status: str | None = "draft") -> str:
    """导出模板审核报告（Markdown）。`status=None` 导出全部。

    审核动作（在 API / 将来 UI 上做）：满意的改 `status="reviewed"`，不要的删除。
    """
    q = db.query(PlotTemplateORM)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(PlotTemplateORM.updated_at.desc()).all()

    out: list[str] = [
        f"# 模板审核报告（{len(rows)} 个 · status={status or '全部'}）",
        "",
        "> 审核：满意的把 `status` 改为 `reviewed`（`PUT /plot-templates/{{id}}`），不要的直接删除。",
        "> draft 模板在下次 `distill` 跑批时会被清理重建，reviewed 的不受影响。",
        "",
        "---",
        "",
    ]
    for t in rows:
        src = t.source_stats or {}
        out.append(f"## {t.name}")
        out.append("")
        out.append(f"- **一句话**：{t.logline or '—'}")
        out.append(f"- **标签**：{'、'.join(t.genre_tags or []) or '—'}　**节奏**：{t.rhythm or '—'}")
        out.append(f"- **来源**：{src.get('books', 0)} 本书（{'、'.join(src.get('book_names') or []) or '—'}）"
                   f"　`id={t.id[:8]}`")
        for ph in (t.structure or {}).get("phases") or []:
            out.append("")
            out.append(f"### 阶段 · {ph.get('phase') or '—'}")
            out.append("")
            for b in ph.get("beats") or []:
                vs = "；".join(
                    f"**{v.get('src', '?')}**：{v.get('how', '')}"
                    for v in (b.get("variants") or []) if isinstance(v, dict)
                )
                out.append(f"- **{b.get('beat') or '—'}** → {vs or '—'}")
        # Phase 7.3 ①：cast（人格化角色槽位）—— 这是 7.3 向量选角的输入，
        # 审核时必须能看到（否则无法判断槽位质量）。缺 cast 要显式标出来，别让人误以为没这功能。
        cast = (t.structure or {}).get("cast") or []
        out.append("")
        if cast:
            out.append(f"### 角色槽位（cast · {len(cast)} 个）")
            out.append("")
            for c in cast:
                srcs = "、".join(f"{v.get('book')}·{v.get('alias')}"
                                for v in (c.get("srcs") or []) if isinstance(v, dict))
                out.append(f"- **{c.get('slot') or '—'}**（{c.get('mode') or '—'}）"
                           f"：{c.get('desc') or '—'}")
                out.append(f"  - 出现节拍：{'、'.join(c.get('beats') or []) or '—'}")
                out.append(f"  - 溯源：{srcs or '—'}")
        else:
            out.append("### 角色槽位（cast）")
            out.append("")
            out.append("⚠️ **本模板没有 cast** —— 凝练时未产出合法槽位（或为 cast 功能上线前的旧模板）。"
                       "这类模板在篇规划里无法做向量选角，选角会退回 LLM 自由分配。")
        if t.pitfalls:
            out.append("")
            out.append("**常见翻车点**：" + "；".join(t.pitfalls))
        out.append("")
        out.append("---")
        out.append("")

    if out_path is None:
        root = Path(__file__).resolve().parents[3]      # backend/app/services → 项目根
        out_path = str(root / "outputs" / "模板审核报告.md")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(out), encoding="utf-8")
    return out_path


def scrub_en(text: str | None) -> str | None:
    """清除中英混杂的 ASCII 单词（如"凭借 superior 的身法"）。

    模型偶尔会把英文形容词/名词直接写进中文叙述；对模板库是硬伤（注入后 AI 会学着中英混杂）。
    只删 ASCII 字母串，保留数字与标点；随后压缩空格与"的的"。
    """
    if not text:
        return text
    s = re.sub(r"[A-Za-z]{2,}", "", text)
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = s.replace("的的", "的").replace(" 的", " 的")
    s = re.sub(r"^(凭借|通过|利用|以)\s+", r"\1", s)
    return s
