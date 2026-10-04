"""情节模板库（Phase 7.1，2026-09-11）。

三层检索中的前两层（第三层 LLM 扩查留给规划调用方）：
  1. genre_tags / scale 标签过滤（SQLite，零成本）；
  2. 向量检索 —— **chunk 粒度 = beat**：篇规划查模板目录，卡文时精确命中
     "当前节拍其他书的不同走法"（variants）。多查询 RRF 融合（复用 A 线思路）。
  原文永不入库；向量进 `vector_chunks`（source_type='plot_template'，
  project_id=`__global__`，与全局资料池同池）→ **检索复用 A 线 Hybrid 基建**。

设计原则：
- 计量/索引是旁路：向量化失败不影响模板 CRUD（无 key 时纯标签/关键词也能用）；
- 混合语义（"既像学院大比又像秘境寻宝"）不需要拆 —— 向量空间天然落在两簇之间；
  用户显式给多个关键词时走 multi-query RRF。
"""
import json
import logging
import re
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.orm import PlotTemplateORM
from app.services import vector_index

logger = logging.getLogger(__name__)

try:
    from app.services.reference_crud import GLOBAL_PROJECT_ID as GLOBAL
except Exception:  # noqa: BLE001 - 防御：循环导入时不炸模块加载
    GLOBAL = "__global__"

SCALE_ARC = "arc"
SCALE_SEGMENT = "segment"
SOURCE_TYPE = "plot_template"
# Phase 7.3 ②：cast 槽位的独立索引空间。**必须与模板块分开**：
# 模板块回答"这个套路是什么"，cast 块回答"这个功能位要什么样的人" ——
# 混在一个 source_type 里做 KNN，选角查询会被 beat 文本淹没。
SOURCE_TYPE_CAST = "plot_cast"
# 角色原型库（2026-09-15）：cast 槽位的**纯功能**索引空间。与 plot_cast 的区别：
# plot_cast 块是「slot+desc+mode+srcs 实现」混合文本（给选角阈值匹配用），
# char_archetype 块是「slot+desc+mode」**不带 srcs** —— 建卡人格参考场景
# （查询侧 = 角色草稿/人设描述）两边都不该带源书痕迹，文本形态对齐。
SOURCE_TYPE_ARCHETYPE = "char_archetype"

# RRF 常数（与 A 线 pick_relevant 同源）
RRF_K = 60


# ---------------------------------------------------------------------------
# 结构拍平与检索文本
# ---------------------------------------------------------------------------
def structure_beats(t: PlotTemplateORM) -> list[dict]:
    """把 structure 拍平为 [{phase, beat, variants}]（顺序保持）。"""
    out: list[dict] = []
    for ph in (t.structure or {}).get("phases") or []:
        for b in ph.get("beats") or []:
            out.append({
                "phase": ph.get("phase") or "",
                "beat": b.get("beat") or "",
                "variants": b.get("variants") or [],
            })
    return out


def _variants_text(variants: list) -> str:
    """变体 → 拼接文本。2026-09-19 起：`desc`（该环的具体剧情概括）存在时一并拼入——
    原子骨架的 `how` 只有弧名，语义信息太少，既不利界面展示也不利 beat 级向量召回。
    ⚠️ 改本函数后**存量向量块全部失效** → 必须跑 scripts/reindex_plot_templates.py 回填。"""
    parts = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        seg = f"（{v.get('src', '?')}）{v.get('how', '')}"
        if v.get("desc"):
            seg += f"：{v['desc']}"
        parts.append(seg)
    return "；".join(parts)


def beat_chunks(t: PlotTemplateORM) -> list[str]:
    """beat 级切块（每 beat 一块）—— 卡文场景的精确检索粒度。

    块内自带 模板名/阶段/节拍 上下文，保证单独命中一块时也能看懂语义。
    """
    chunks: list[str] = []
    for b in structure_beats(t):
        vs = _variants_text(b["variants"])
        chunk = f"{t.name}｜{b['phase']}｜{b['beat']}" + (f"：{vs}" if vs else "")
        chunks.append(chunk)
    return chunks


def template_chunks(t: PlotTemplateORM) -> list[str]:
    """向量化用的**全部**切块 = 1 个模板级块 + N 个 beat 级块。

    为什么必须有模板级块（2026-09-11 实测）：
    只索引 beat 块时，块的语义被"具体情节"主导，而模板名/logline/标签（如"金手指觉醒"）
    在 beat 文本里权重很低 → 模糊口述（"主角觉醒金手指"）反而召不回该模板
    （实测排序把"夺舍觉醒"排到了最后）。模板级块把 name + logline + 标签 + 各 beat 标题
    聚成一个块，正好代表"这个套路是什么"。
    """
    head = f"{t.name}｜{t.logline or ''}｜标签：{'、'.join(t.genre_tags or [])}"
    beats_head = "；".join(f"{b['phase']}·{b['beat']}" for b in structure_beats(t))
    chunks = [head + (f"｜节拍：{beats_head}" if beats_head else "")]
    chunks.extend(beat_chunks(t))
    return chunks


def structure_casts(t: PlotTemplateORM) -> list[dict]:
    """取模板的 cast 槽位（`structure["cast"]`）。缺 cast 返回 []（老模板未凝练 cast）。"""
    cast = (t.structure or {}).get("cast") or []
    return [c for c in cast if isinstance(c, dict)]


def cast_slot_text(c: dict) -> str:
    """单个槽位的**向量化文本**（Phase 7.3 ②）。

    🔴 为什么是「功能描述 + srcs 典型实现」的**混合文本**（2026-09-12 讨论定稿）：
    纯功能描述（"主角的导师型角色，掌握关键资源"）过于抽象 —— 与作者写的
    「具体角色人设」（"青云宗长老，脾气古怪，一手炼器绝活"）向量距离偏大，
    分数会**整体偏低且区分度差**，标定阈值时会误判成"全都不匹配"。
    掺进 srcs 的具体实现文本（各源书该槽位的 role_desc）后，向量落在
    "半抽象半具体"的位置，与真实人设的距离更合理。

    `slot` 名前置是刻意的：让"引路人师长"这个词本身参与匹配（作者口述
    常常就是这么说的）。
    """
    parts = [str(c.get("slot") or "")]
    if c.get("desc"):
        parts.append(str(c["desc"]))
    if c.get("mode"):
        parts.append(f"定位：{c['mode']}")
    for s in (c.get("srcs") or []):
        if isinstance(s, dict) and s.get("desc"):
            parts.append(str(s["desc"]))
    return "｜".join(p for p in parts if p.strip())


def cast_chunks(t: PlotTemplateORM) -> list[str]:
    """cast 级切块（每槽位一块），与 `structure["cast"]` **下标一一对应**。

    ⚠️ 顺序契约：`chunk_idx == cast 数组下标`。命中后直接用
    `structure["cast"][chunk_idx]` 反查，不必解析 chunk 文本
    （见 orm 里 source_stats 的约定与 7.3 方案）。
    """
    return [cast_slot_text(c) for c in structure_casts(t)]


def archetype_text(c: dict) -> str:
    """单个槽位的**原型向量化文本**（角色原型库，2026-09-15）。

    🔴 与 `cast_slot_text` 的关键区别：**不带 srcs**。srcs 是各源书该槽位
    的具体实现（role_desc），掺进去后块语义被"某本书的具体角色"拉偏；
    原型库要回答的是"这个功能位要什么样的人"，批次3 建卡时查询侧
    （新角色草稿/人设描述）同样不带源书痕迹 —— 两边文本形态对齐。
    `slot` 名前置的理由同 `cast_slot_text`：作者口述常常直接说槽位名。
    """
    parts = [str(c.get("slot") or "")]
    # L2 升级（2026-09-16）：位阶 + 12 维性格刻度 → 让**文本检索也能吃到性格**；
    # 原始数值仍留在 structure.cast[].traits 里供数值过滤/排序。
    # ⚠️ 改本函数后**存量向量块全部失效** → 必须跑 scripts/reindex_plot_templates.py 回填。
    ranks = [str(r).strip() for r in (c.get("ranks") or []) if str(r).strip()]
    if ranks:
        parts.append("位阶：" + "/".join(ranks))
    traits = c.get("traits")
    if isinstance(traits, dict) and traits:
        try:
            from app.services.plot_distill import TRAIT_DIMS      # 懒加载，避免循环依赖
            names = TRAIT_DIMS
        except Exception:  # noqa: BLE001
            names = {}
        seg = [f"{names.get(k, k)}{int(v):+d}" for k, v in traits.items()
               if isinstance(v, (int, float)) and int(v) != 0]
        if seg:
            parts.append("性格：" + "·".join(seg))
    if c.get("desc"):
        parts.append(str(c["desc"]))
    if c.get("mode"):
        parts.append(f"定位：{c['mode']}")
    return "｜".join(p for p in parts if p.strip())


def archetype_chunks(t: PlotTemplateORM) -> list[str]:
    """原型级切块（每槽位一块），顺序契约同 `cast_chunks`：`chunk_idx == cast 数组下标`。"""
    return [archetype_text(c) for c in structure_casts(t)]


def search_text(t: PlotTemplateORM) -> str:
    """模板级检索/展示文本（目录用）。"""
    parts = [t.name, t.logline or "", " ".join(t.genre_tags or [])]
    for b in structure_beats(t):
        parts.append(f"{b['phase']}·{b['beat']}：{_variants_text(b['variants'])}")
    return "\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------
# 精确键优先匹配链（DEV-P3a ①）
# ---------------------------------------------------------------------------
# 背景：`.flow/docs/skel-v4.md` §二 拍板「①(大类,子事件) 精确匹配优先 → ②同大类其他
# 子事件（仅检索参考）→ ③其余按向量序」。P3 实测发现这条链**只存在于组装侧**，
# search() 是纯向量+关键词 —— 后果：口述说「比试」也会召回同大类的「夺宝争锋」。
#
# 🔴 **红线：推断层绝不阻断主链**。词表来自库内现役 arc 模板（不硬编码、不写库）；
# 词表快路径零成本优先，未命中才叫**轻量 LLM 分类**；网关挂/超时/返回垃圾 → 记原因、
# 退纯向量序，检索照常返回结果。任何一层抛错都在 search() 里兜住。
KEY_MATCH_SCENE = "sf_key_match"        # 用量记账场景名
KEY_MATCH_MAX_TOKENS = 80               # 只要一个短 JSON，不给模型发挥空间
KEY_MATCH_TIMEOUT = 30                   # 秒；分类是「锦上添花」，不值得让作者等
# 🔴 只试 1 次、不退避（DEV-P3a 实测加）：`_sf_post` 默认重试 5 次指数退避，
#    网关拒连时作者要白等 **38 秒**才拿到本该立刻返回的向量序结果
#    （证据 outputs/p3_inject/P3a_红线实测.txt）。分类失败可退化成纯向量序，
#    重试没有意义 —— 快速失败才是这里正确的行为。
KEY_MATCH_ATTEMPTS = 1


def key_vocab(db: Session) -> dict[str, list[str]]:
    """从**现役 arc 模板**的 source_stats 抽 {大类: [子事件, ...]}。

    🔴 只读、零成本、随库自同步 —— 不硬编码 57 类词表（那会在类清单变更时静默过期）。
    """
    out: dict[str, list[str]] = {}
    rows = (db.query(PlotTemplateORM)
            .filter(PlotTemplateORM.scale == SCALE_ARC,
                    PlotTemplateORM.status == "active").all())
    for o in rows:
        ss = o.source_stats or {}
        if not isinstance(ss, dict):
            continue
        cls = str(ss.get("class") or "").strip()
        if not cls:
            continue
        sub = str(ss.get("sub_event") or "").strip()
        bucket = out.setdefault(cls, [])
        if sub and sub not in bucket:
            bucket.append(sub)
    return out


def _keyword_guess_key(query: str, vocab: dict[str, list[str]]) -> tuple[str, str] | None:
    """词表关键词快路径（零成本）：口述里直接出现类名/子事件名就命中。

    返回 `(大类, 子事件)`：子事件命中 → 精确键；只命中类名 → `(类, "")` 只定到大类层。
    全不命中 → None（交 LLM 分类）。**取最长命中名**：大类与子事件可能互相包含。
    """
    q = (query or "").strip()
    if not q or not vocab:
        return None
    best: tuple[int, str, str] | None = None      # (名长, 大类, 子事件)
    for cls, subs in vocab.items():
        for name, sub in [(cls, "")] + [(s, s) for s in subs]:
            if name and name in q and (best is None or len(name) > best[0]):
                best = (len(name), cls, sub)
    return (best[1], best[2]) if best else None


def _llm_guess_key(db: Session, query: str, vocab: dict[str, list[str]], *,
                   timeout: int = KEY_MATCH_TIMEOUT) -> tuple[tuple[str, str] | None, dict]:
    """轻量 LLM 分类（qwen3-8B 关思考 / temp 0 / 短输出，与 refine-hint 同款配置）。

    🔴 **本函数绝不抛异常**：任何失败都变成 `(None, {ok: False, reason})`，
    由 search() 退纯向量序。防幻觉：返回的类/子事件必须落在词表内，否则只保留大类层。
    """
    if not vocab:
        return None, {"source": "off", "ok": False, "class": None, "sub_event": None,
                      "reason": "词表为空（库里没有带键的 arc 模板）"}
    classes = sorted(vocab)
    sub_lines = "\n".join(f"  {c}：{'、'.join(sorted(vocab[c]))}" for c in classes)
    prompt = (
        "你是情节分类器。判断下面这句作者口述属于哪个（大类, 子事件）。\n"
        "只能从给定清单里选；都不像就返回空对象。\n"
        "只输出一个 JSON 对象，不要任何解释。\n\n"
        f"清单：\n{sub_lines}\n\n"
        f"口述：{query}\n\n"
        '输出：{"class":"","sub_event":""}'
    )
    acct: dict = {"source": "llm", "ok": False, "class": None, "sub_event": None}
    try:
        from app.services import plot_import
        raw = plot_import.sf_chat(db, prompt, scene=KEY_MATCH_SCENE,
                                  max_tokens=KEY_MATCH_MAX_TOKENS,
                                  temperature=0.0, timeout=timeout,
                                  attempts=KEY_MATCH_ATTEMPTS)
        raw = (raw or "").strip()
        # 模型爱加代码块/前后废话：取最外层 JSON 对象
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0) if m else raw)
        if not isinstance(obj, dict):
            raise ValueError(f"分类返回不是 JSON 对象: {type(obj).__name__}")
        cls = str(obj.get("class") or "").strip()
        sub = str(obj.get("sub_event") or "").strip()
        if cls not in vocab:
            acct["reason"] = f"分类返回的类不在词表内: {cls!r}"
            acct["raw"] = raw[:200]
            return None, acct
        # 子事件必须属于该类，否则只保留大类层（防幻觉串类）
        if sub and sub not in vocab[cls]:
            acct["reason"] = f"子事件不属于该类，降级到大类层: {sub!r}"
            sub = ""
        acct.update({"ok": True, "class": cls, "sub_event": sub,
                     "downgraded": not sub and bool(str(obj.get("sub_event") or "").strip())})
        return (cls, sub), acct
    except Exception as e:  # noqa: BLE001
        # 🔴 红线落点：这里吞掉一切（含超时/网关挂/网关返回垃圾），主链照常
        acct["reason"] = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning(f"[plot_tpl] 键分类失败，退纯向量序（不阻断检索）: {acct['reason']}")
        return None, acct


def _guess_key(db: Session, query: str, vocab: dict[str, list[str]], *,
              llm: bool = True) -> tuple[tuple[str, str] | None, dict]:
    """口述 → (大类,子事件)：词表快路径优先，未命中才走轻量 LLM 分类。"""
    hit = _keyword_guess_key(query, vocab)
    if hit:
        return hit, {"source": "keyword", "ok": True, "class": hit[0], "sub_event": hit[1]}
    if not llm:
        return None, {"source": "off", "ok": False, "class": None, "sub_event": None,
                      "reason": "未启用 LLM 分类"}
    return _llm_guess_key(db, query, vocab)


def _rerank_by_key(items: list[dict], key: tuple[str, str] | None) -> list[dict]:
    """三层重排：精确键(0) → 同大类其他子事件(1) → 其余(2)。**同级内保持原向量序**。"""
    if not key or not items:
        return items
    cls, sub = key
    if not cls:
        return items
    tiered = []
    for i, it in enumerate(items):
        ss = it.get("source_stats") or {}
        c = str(ss.get("class") or "") if isinstance(ss, dict) else ""
        s = str(ss.get("sub_event") or "") if isinstance(ss, dict) else ""
        if c != cls:
            tier = 2
        elif sub and s == sub:
            tier = 0
        else:
            tier = 1
        tiered.append((tier, i, it))
    tiered.sort(key=lambda x: (x[0], x[1]))       # 显式带原下标，稳定
    return [it for _, _, it in tiered]


def _apply_key_match(db: Session, query: str, items: list[dict], *,
                     llm: bool = True) -> tuple[list[dict], dict]:
    """检索结果按精确键重排。**任何异常都退化为原序**并把原因记进账。

    🔴🔴 **向量优先守卫**（P3a 实网冒烟实测加的）：分类器与向量 top1 的**大类不一致**时
    **不重排**。理由：分类器是弱信号且实测不稳定（同一句口述它给过「擂台大比」和
    「拜师入门」两个答案，后者把错误模板顶到 top1、把向量排第一的正确模板压下去）。
    精确键链只能**细化**向量已给出的答案（大类内的子事件重排），不能**推翻**它。
    冲突不丢弃 —— 记进 `key_match`（`conflict` / `vector_top_class`），前端与排查可追。
    """
    if not items:
        return items, {"source": "off", "ok": False, "class": None, "sub_event": None,
                       "reason": "无候选可重排"}
    try:
        vocab = key_vocab(db)
        key, acct = _guess_key(db, query, vocab, llm=llm)
        acct.setdefault("reranked", False)
        acct.setdefault("applied", False)
        if not key or not key[0]:
            return items, acct
        # 🔴 向量优先守卫
        top_cls = ""
        ss0 = items[0].get("source_stats") or {}
        if isinstance(ss0, dict):
            top_cls = str(ss0.get("class") or "")
        if top_cls and key[0] != top_cls:
            acct["conflict"] = True
            acct["vector_top_class"] = top_cls
            acct["reason"] = (f"分类器判「{key[0]}」与向量 top1 的「{top_cls}」冲突，"
                              f"以向量序为准（不重排）")
            return items, acct
        acct["conflict"] = False
        acct["vector_top_class"] = top_cls
        out = _rerank_by_key(items, key)
        acct["applied"] = True
        acct["reranked"] = [i["name"] for i in out] != [i["name"] for i in items]
        acct["order"] = [i["name"] for i in out]
        return out, acct
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plot_tpl] 键匹配重排失败，退原序（不阻断检索）: "
                       f"{type(e).__name__}: {e}")
        return items, {"source": "error", "ok": False, "class": None, "sub_event": None,
                       "reranked": False, "applied": False, "conflict": False,
                       "reason": f"{type(e).__name__}: {str(e)[:160]}"}


# ---------------------------------------------------------------------------
# 向量化（旁路，失败不影响 CRUD）
# ---------------------------------------------------------------------------
def index_template(db: Session, t: PlotTemplateORM) -> int:
    """重建模板向量（先删旧块再建 模板级 + beat 级 + **cast 级** + **原型级**）。返回块数；失败返回 0（静默）。

    ⚠️ **三个 source_type 都要清**（plot_template / plot_cast / char_archetype）：
    只清部分会留下孤儿块 —— 槽位改了或 cast 被删光，旧槽位向量还在池子里：
    plot_cast 孤儿会让选角反查 `cast[idx]` 错位越界；char_archetype 孤儿会让
    原型检索召回已不存在的槽位（2026-09-15 角色原型库接入第三路）。
    """
    try:
        vector_index.remove_source(db, GLOBAL, SOURCE_TYPE, t.id)
        vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_CAST, t.id)
        vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_ARCHETYPE, t.id)
        n = vector_index.index_chunks(db, GLOBAL, SOURCE_TYPE, t.id, template_chunks(t))
        n_cast = vector_index.index_chunks(db, GLOBAL, SOURCE_TYPE_CAST, t.id, cast_chunks(t))
        n_arch = vector_index.index_chunks(db, GLOBAL, SOURCE_TYPE_ARCHETYPE, t.id, archetype_chunks(t))
        if n or n_cast or n_arch:
            logger.info(f"[plot_tpl] 模板已向量化 name={t.name} beats={n} cast={n_cast} archetype={n_arch}")
        return n + n_cast + n_arch
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plot_tpl] 模板向量化失败（不影响保存）: {type(e).__name__}: {e}")
        return 0


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def _to_dict(t: PlotTemplateORM) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "scale": t.scale,
        "genre_tags": t.genre_tags or [],
        "logline": t.logline,
        "structure": t.structure or {},
        "pitfalls": t.pitfalls or [],
        "rhythm": t.rhythm,
        "source_stats": t.source_stats or {},
        "status": t.status,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def create(db: Session, data: dict) -> PlotTemplateORM:
    o = PlotTemplateORM(
        id=uuid.uuid4().hex,
        name=data.get("name") or "未命名模板",
        scale=data.get("scale") or SCALE_ARC,
        genre_tags=data.get("genre_tags") or [],
        logline=data.get("logline"),
        structure=data.get("structure") or {},
        pitfalls=data.get("pitfalls") or [],
        rhythm=data.get("rhythm"),
        source_stats=data.get("source_stats") or {},
        status=data.get("status") or "draft",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    index_template(db, o)
    return o


def update(db: Session, template_id: str, data: dict) -> PlotTemplateORM | None:
    o = db.query(PlotTemplateORM).filter_by(id=template_id).first()
    if o is None:
        return None
    for k in ("name", "scale", "genre_tags", "logline", "structure",
              "pitfalls", "rhythm", "source_stats", "status"):
        if k in data and data[k] is not None:
            setattr(o, k, data[k])
    o.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(o)
    index_template(db, o)   # 结构变了 → 重建向量
    return o


def get(db: Session, template_id: str) -> PlotTemplateORM | None:
    return db.query(PlotTemplateORM).filter_by(id=template_id).first()


def list_templates(db: Session, *, scale: str | None = None,
                   status: str | None = None) -> list[dict]:
    q = db.query(PlotTemplateORM)
    if scale:
        q = q.filter(PlotTemplateORM.scale == scale)
    if status:
        q = q.filter(PlotTemplateORM.status == status)
    rows = q.order_by(PlotTemplateORM.updated_at.desc()).all()
    return [_to_dict(t) for t in rows]


def delete(db: Session, template_id: str) -> bool:
    o = db.query(PlotTemplateORM).filter_by(id=template_id).first()
    if o is None:
        return False
    # 三个 source_type 都要清：漏掉 plot_cast / char_archetype 会让槽位向量
    # 永久留在全局池里 —— 模板已不存在，但选角/原型检索仍会召回它的槽位。
    vector_index.remove_source(db, GLOBAL, SOURCE_TYPE, template_id)
    vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_CAST, template_id)
    vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_ARCHETYPE, template_id)
    db.delete(o)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# 检索（层 1 标签过滤 + 层 2 向量 multi-query RRF；层 3 LLM 扩查留给调用方）
# ---------------------------------------------------------------------------
def _tag_filter(db: Session, scale: str | None, tags: list[str]) -> list[PlotTemplateORM]:
    q = db.query(PlotTemplateORM)
    if scale:
        q = q.filter(PlotTemplateORM.scale == scale)
    # 🔴 退役模板不参与检索（2026-09-19 加，配合 scripts/retire_plot_templates.py）：
    #    status='archived' = 已退役（旧方案模板退出检索，但数据完整保留、可一条 UPDATE 回滚）。
    #    当前库里 164 条全是 draft → 本过滤不改变现有行为。
    q = q.filter(PlotTemplateORM.status != "archived")
    rows = q.all()
    if not tags:
        return rows
    want = {t.strip().lower() for t in tags if t.strip()}
    out = []
    for r in rows:
        have = {str(x).strip().lower() for x in (r.genre_tags or [])}
        # 标签语义：模板命中任一标签即可（过滤，非打分）
        if have & want:
            out.append(r)
    return out


def _keyword_filter(db: Session, query: str, scale: str | None,
                    rows: list[PlotTemplateORM]) -> list[dict]:
    """向量不可用时的兜底：名称/logline/beat 文本关键词匹配。"""
    kws = [w for w in (query or "").replace("，", " ").replace(",", " ").split() if w]
    scored: list[tuple[float, PlotTemplateORM, list[dict]]] = []
    for t in rows:
        if scale and t.scale != scale:
            continue
        hits: list[dict] = []
        score = 0.0
        text_all = search_text(t)
        for kw in kws:
            if kw in t.name or kw in (t.logline or ""):
                score += 5.0
            for b in structure_beats(t):
                blob = f"{b['phase']}·{b['beat']}：{_variants_text(b['variants'])}"
                if kw in blob:
                    score += 1.0
                    hits.append(b)
        if score > 0 and kws:
            # name/logline 命中而 beat 未命中 → 视为"目录级命中"，带上全部 beats
            # （关键词匹配的是模板名，作者关心的仍是该模板的节拍与走法）
            beats_out = hits if hits else structure_beats(t)
            scored.append((score, t, beats_out))
    scored.sort(key=lambda x: -x[0])
    return [
        {**_to_dict(t), "matched_beats": hits, "_score": s}
        for s, t, hits in scored[:8]
    ]


def search(db: Session, *, query: str, queries: list[str] | None = None,
           scale: str | None = None, tags: list[str] | None = None,
           top_k: int = 8, key_match: bool = True) -> dict:
    """模板检索主入口。

    - `query` 一句模糊口述即可；`queries` 显式多查询（如 ["学院大比","秘境寻宝"]），
      多路各查 beat 级 chunks 后 RRF 融合；
    - `key_match`（DEV-P3a ①，默认开）：口述先推断 (大类,子事件)，再按
      「精确键 → 同大类 → 其余」三层重排。**推断层不阻断检索**（网关挂/超时退纯向量序），
      推断与重排依据记在返回值的 `key_match` 账里。`key_match=False` 可关闭（纯向量序）。
    - 无向量能力时自动回退关键词匹配（`mode=fallback`），**不硬报错**；
    - 返回模板 + 其被命中的 beats（卡文场景直接看 variants）。
    """
    q_list = [q for q in ([query] + list(queries or [])) if q and q.strip()]
    pool = _tag_filter(db, scale, tags or [])

    use_vector = vector_index.enabled(db) and q_list
    if not use_vector:
        items = _keyword_filter(db, query or " ".join(queries or []), scale, pool)
        items, acct = _apply_key_match(db, query or " ".join(queries or []), items,
                                       llm=key_match)
        return {"mode": "fallback_tags", "queries": q_list, "items": items,
                "key_match": acct, "reranked": bool(acct.get("reranked"))}

    # --- multi-query 向量检索，beat 级 RRF 融合 ---
    beat_rrf: dict[tuple[str, str], float] = {}   # (template_id, beat_text) -> rrf 分
    for q in q_list:
        hits = vector_index.search_similar(db, GLOBAL, SOURCE_TYPE, q, top_k=top_k * 3)
        if not hits:
            continue
        # 相对阈值：相似度分数量纲随实现不同（brute=余弦，sqlite-vec=1/(1+L2)），
        # 但**同一查询内**的相对比例可比。只吸收与该查询 top1 相比 >= 30% 的命中 ——
        # 滤掉正交/远距噪声，避免不相关模板混进 RRF 尾部（2026-09-11 单测实测发现）。
        thresh = hits[0].score * 0.3
        kept = [h for h in hits if h.score >= thresh]
        for rank, h in enumerate(kept):
            key = (h.source_id, h.chunk_text)
            beat_rrf.setdefault(key, 0.0)
            beat_rrf[key] += 1.0 / (RRF_K + rank + 1)

    if not beat_rrf:
        items = _keyword_filter(db, query or " ".join(q_list), scale, pool)
        items, acct = _apply_key_match(db, query or " ".join(q_list), items, llm=key_match)
        return {"mode": "fallback_tags", "queries": q_list, "items": items,
                "key_match": acct, "reranked": bool(acct.get("reranked"))}

    # 聚合到模板：模板分 = 其 beat 的最优 RRF；同时收集每个模板的 matched beats
    by_template: dict[str, dict] = {}
    for (tid, _chunk), s in sorted(beat_rrf.items(), key=lambda x: -x[1]):
        by_template.setdefault(tid, {"best": 0.0, "beats": []})
        if s > by_template[tid]["best"]:
            by_template[tid]["best"] = s

    items: list[dict] = []
    for tid, info in by_template.items():
        t = get(db, tid)
        if t is None:
            continue
        if (t.status or "") == "archived":     # 退役模板不参与向量结果（2026-09-19）
            continue
        if scale and t.scale != scale:
            continue
        if tags:
            have = {str(x).strip().lower() for x in (t.genre_tags or [])}
            want = {x.strip().lower() for x in tags}
            if not (have & want):
                continue
        # 收集该模板被命中的 beat（按 RRF 排序，去重）
        beats = []
        for (btid, _chunk), s in sorted(beat_rrf.items(), key=lambda x: -x[1]):
            if btid != tid:
                continue
            for b in structure_beats(t):
                key_txt = f"{t.name}｜{b['phase']}｜{b['beat']}"
                if _chunk.startswith(key_txt) and b not in beats:
                    beats.append(b)
        items.append({**_to_dict(t), "matched_beats": beats[:6], "_score": round(info["best"], 6)})

    items.sort(key=lambda x: -x["_score"])
    # 🔴 DEV-P3a ①：先按向量序截 top_k，再按精确键重排 —— 重排只调整**已召回集合**的
    #    顺序，不把没召回的模板硬拽进来（那会引入更差的候选）。
    items = items[:top_k]
    items, acct = _apply_key_match(db, query or " ".join(q_list), items, llm=key_match)
    return {"mode": "vector", "queries": q_list, "items": items,
            "key_match": acct, "reranked": bool(acct.get("reranked"))}
