"""上下文各层的数据提取与渲染。

每个 layer_* 函数负责「查表 → 渲染成给模型看的文本」，返回 Block 或 None。
所有函数都必须容错：某张表查不到、字段缺失，都只影响这一层，
不能让整个生成流程崩掉——写小说的人不该因为势力表是空的就生成失败。

角色注入策略（用户确认）：主角全量 + 本章命中者全量 + 其余每人一行。
这是长篇的通行折中——全量注入所有角色到第 50 章就装不下了，
但只注入主角又会让配角人设漂移。
"""
import logging
import os
from sqlalchemy.orm import Session

from app.core.context.budget import (
    Block,
    P_CHARACTER, P_WORLD, P_FORESHADOW, P_MEMORY_RECENT,
    P_MEMORY_STAGE, P_ENTITY, P_CONTINUITY, P_DISCUSSION, P_CRITICAL,
)
from app.models.orm import (
    ProjectORM, VolumeORM, ArticleORM, ChapterORM,
    CharacterORM, FactionORM, LocationORM, RelationORM, SkillORM,
    ForeshadowORM, SettingORM, SettingTemplateORM, DiscussionMessageORM,
    ChapterMemoryORM, StageSummaryORM, ArticlePlanORM,
)

# 上一章结尾保留的字符数——够模型接住语气和场景，又不至于喧宾夺主
PREV_TAIL_CHARS = 600


logger = logging.getLogger(__name__)


def _safe(fn, default=None):
    """查询容错包装：单层失败不影响其它层。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[context.layers] 层提取失败，已跳过: {type(e).__name__}: {str(e)[:120]}")
        return default


# 设定库里「永远展开描述」的骨干体系：这些是长篇保持人设/战力一致性的骨架，
# 哪怕本章没显式提到，模型也得知道有哪些层级，否则容易写出冲突的设定。
# 注意：官制类（体系）不在此列——官僚体系是参考资料，按层级拆分后按需发送
# （relevant 模式下只有命中的层级才展开详述，其余仅保留 name+levels 阶梯），
# 避免一条巨无霸设定常驻占窗口。
_SETTING_BACKBONE = {"境界", "修炼"}

# 设定分类排序：体系（官制）优先，确保小模型注意力先落在知识问答高频项。
# 模块级共享：layer_world 与 build_setting_catalog 都用它。
_CAT_ORDER = {"体系": 0, "境界": 1, "货币": 2, "规则": 3}


def _relevance(text: str, query: str) -> float:
    """文本与查询的相关性（0~1），2-gram 重叠率。

    零依赖、对中文自造词友好（和 reference_crud.score_reference 同一思路，
    但更轻——只算重叠率，不给标签/文件名加权，因为设定/伏笔没有那些字段）。
    """
    if not (text and query):
        return 0.0
    grams = {text[i:i + 2] for i in range(len(text) - 1)}
    if not grams:
        return 0.0
    hit = sum(1 for g in grams if g in query)
    return hit / len(grams)


# ===========================================================================
# 实体识别：从文本里找出提到了哪些已登记的角色 / 势力 / 地点
# ===========================================================================

def extract_mentions(db: Session, project_id: str, *texts: str) -> dict[str, set[str]]:
    """用「已登记实体名做子串匹配」代替分词。

    为什么不用分词器：小说里的人名地名多是自造词（「陈墨渊」「太玄宗」），
    通用分词器切不准，反而会漏。而资料库里本来就存着标准名称，
    直接拿它们去正文里找，零依赖且不会误切。
    """
    haystack = "\n".join(t for t in texts if t)
    found: dict[str, set[str]] = {"characters": set(), "factions": set(), "locations": set()}
    if not haystack.strip():
        return found

    def _match(model, bucket: str):
        rows = db.query(model).filter_by(project_id=project_id).all()
        for r in rows:
            name = (r.name or "").strip()
            # 单字名误报率太高（「王」「李」），跳过
            if len(name) >= 2 and name in haystack:
                found[bucket].add(name)

    _safe(lambda: _match(CharacterORM, "characters"))
    _safe(lambda: _match(FactionORM, "factions"))
    _safe(lambda: _match(LocationORM, "locations"))
    return found


# ===========================================================================
# L1 世界观：小说概况 + 卷/篇概览 + 全局设定库
# ===========================================================================

def layer_world(db: Session, project_id: str, volume_id: str | None = None,
                article_id: str | None = None, mode: str = "full",
                query_text: str = "") -> Block | None:
    """世界观层。

    mode="full"（默认）：设定库全量照旧（零回归）。
    mode="relevant"：设定 name+levels 永远保留（模型始终知道有哪些体系），
    仅 verbose description 按相关性展开；骨干体系(境界/体系/修炼)描述始终展开。
    """
    lines: list[str] = []

    def _load():
        p = db.query(ProjectORM).filter_by(id=project_id).first()
        if p:
            head = f"作品《{p.name}》"
            if p.genre:
                head += f"，题材：{p.genre}"
            head += f"，已写 {p.chapter_count or 0} 章"
            lines.append(head)
            if (p.summary or "").strip():
                lines.append(f"全书梗概：{p.summary.strip()}")

        if article_id:
            a = db.query(ArticleORM).filter_by(id=article_id).first()
            if a:
                if (a.summary or "").strip():
                    lines.append(f"当前篇《{a.name}》概要：{a.summary.strip()}")
                else:
                    lines.append(f"当前篇：《{a.name}》")
                v = db.query(VolumeORM).filter_by(id=a.volume_id).first()
                if v and (v.summary or "").strip():
                    lines.append(f"当前卷《{v.name}》概要：{v.summary.strip()}")
        elif volume_id:
            v = db.query(VolumeORM).filter_by(id=volume_id).first()
            if v and (v.summary or "").strip():
                lines.append(f"当前卷《{v.name}》概要：{v.summary.strip()}")

    _safe(_load)

    # 获取本小说的设定过滤列表
    _proj_setting_ids = None
    _p = db.query(ProjectORM).filter_by(id=project_id).first()
    if _p and _p.setting_ids is not None:
        _proj_setting_ids = set(_p.setting_ids)

    # 全局设定库（境界/货币/体系/规则），跨作品共享
    # 排序优化：体系（官制等知识问答高频）优先，确保小模型注意力先落在上面
    # _CAT_ORDER 已提升到模块级（见文件顶部），此处直接复用
    def _settings():
        rows = db.query(SettingORM).all()
        # 按 setting_ids 过滤：仅保留本小说选中的设定
        if _proj_setting_ids is not None:
            rows = [s for s in rows if s.id in _proj_setting_ids]
        rows.sort(key=lambda s: (_CAT_ORDER.get(s.category or "其它", 99), s.name))
        if not rows:
            return
        by_cat: dict[str, list[str]] = {}
        for s in rows:
            desc = (s.description or "").strip()
            levels = s.levels or []
            piece = f"{s.name}"
            if levels:
                shown = "→".join(str(x) for x in levels[:12])
                piece += f"（{shown}{'…' if len(levels) > 12 else ''}）"
            # 按需模式：name+levels 永远保留（模型始终知道有哪些体系，不回归）；
            # 仅 verbose description 按相关性展开，骨干体系描述始终展开。
            expand = bool(desc) and (
                mode != "relevant"
                or (s.category or "") in _SETTING_BACKBONE
                or _relevance(desc + " " + (s.name or ""), query_text) >= 0.06
            )
            if expand and desc:
                piece += f"：{desc}"
            by_cat.setdefault(s.category or "其它", []).append(piece)
        for cat, items in by_cat.items():
            lines.append(f"[{cat}] " + "；".join(items))

    _safe(_settings)

    # 设定模板（2026-09-26：按题材一套一套的单文档模板，同一 setting_ids 空间）
    # 模板是整套大文档，不适合按条展开——常驻只报名号，详情走 LOAD_SETTING 按需取。
    # setting_ids=None 表示全量模式（旧语义），此时全部模板都要报名号。
    def _tpl_names():
        tquery = db.query(SettingTemplateORM)
        if _proj_setting_ids is not None:
            if not _proj_setting_ids:
                return
            tquery = tquery.filter(SettingTemplateORM.id.in_(_proj_setting_ids))
        trows = tquery.all()
        if not trows:
            return
        names = "；".join(f"{t.name}（{t.genre or '通用'}，含境界/货币/体系/规则分节）"
                          for t in trows)
        lines.append(f"[设定模板] {names}"
                     f" —— 需要某套模板完整内容时 LOAD_SETTING 该模板 id 获取全文")

    _safe(_tpl_names)

    if not lines:
        return None
    return Block(
        key="world", title="【世界观与作品设定】",
        content="\n".join(lines),
        priority=P_WORLD, order=20, min_chars=200,
    )


# ===========================================================================
# 设定库目录（B 方案：按需加载）
# ===========================================================================

def build_setting_catalog(db: Session, project_id: str) -> str:
    """构造「设定库目录」——只列 id + 名称，不含层级阶梯/摘要/标签。

    用于 B 方案：把设定库从「描述常驻 system」改为「目录 + 按需加载」。
    模型凭名称即可判断该加载哪个设定，需要完整说明时通过 LOAD_SETTING:<id>
    请求，后端再注入详情，避免全文占窗。

    与参考文档的 LOAD_REFS 平行：两者用不同标记、不同 id 空间（SettingORM vs
    ReferenceDocORM），可在同一首轮文本里同时出现，Pass1 一并解析。
    目录风格刻意与参考文档目录保持一致——只给 id + 名称，靠模型按需请求。
    """
    # 项目选定设定过滤：仅展示本小说勾选的设定
    _p = db.query(ProjectORM).filter_by(id=project_id).first()
    _proj_setting_ids = set(_p.setting_ids) if (_p and _p.setting_ids) else None

    def _load():
        rows = db.query(SettingORM).all()
        if _proj_setting_ids is not None:
            rows = [s for s in rows if s.id in _proj_setting_ids]
        rows.sort(key=lambda s: (_CAT_ORDER.get(s.category or "其它", 99), s.name))
        if not rows:
            return ""
        lines = [
            "## 设定库目录（世界观数据库·按需加载详情）",
            "下面是本作品已选用的设定体系清单（仅列名称，不含说明）。",
            "除非回答需要某体系的完整说明，否则不要加载——直接作答即可。\n"
            "需要某设定完整内容（含层级、描述、标签）时，在正式回答前先输出一行"
            "（**支持一次请求多个设定，用逗号分隔**）：\n"
            "  LOAD_SETTING:<id1>,<id2>,<id3>\n"
            "【重要】如果你的回答需要多份设定联动（如同时涉及官制+俸禄），"
            "必须把所有相关 id 写在同一行一次性请求，不要只请求一个——其余信息你无法准确猜测。"
            "系统会把所有请求的设定详情一并注入后再让你作答。",
            "",
        ]
        for s in rows:
            line = f"- id={s.id} | [{s.category or '其它'}] {s.name}"
            lines.append(line)
        # 设定模板（按题材整篇注入的对象）同样进目录；None=全量模式时全部列出
        tquery = db.query(SettingTemplateORM)
        if _proj_setting_ids is not None:
            if _proj_setting_ids:
                tquery = tquery.filter(SettingTemplateORM.id.in_(_proj_setting_ids))
            else:
                tquery = None
        if tquery is not None:
            for t in tquery.all():
                lines.append(f"- id={t.id} | [设定模板] {t.name}（{t.genre or '通用'}）")
        lines.append("")
        return "\n".join(lines)

    return _safe(_load) or ""


# ===========================================================================
# L2 角色卡：主角全量 + 命中者全量 + 其余一行
# ===========================================================================

def _render_character_full(db: Session, c: CharacterORM) -> str:
    head_bits = [c.role_type or "角色"]
    if c.gender:
        head_bits.append(c.gender)
    if c.age:
        head_bits.append(f"{c.age}岁")
    if c.current_level:
        head_bits.append(c.current_level)
    out = [f"· {c.name}（{'，'.join(head_bits)}）"]
    if (c.personality or "").strip():
        out.append(f"  性格：{c.personality.strip()}")
    if (c.background or "").strip():
        out.append(f"  背景：{c.background.strip()}")
    if (c.talent or "").strip():
        out.append(f"  天赋：{c.talent.strip()}")
    if (c.brief or "").strip():
        out.append(f"  简介：{c.brief.strip()}")

    # 技能：JSON 列里的 + skills 表里 owner_id 指向本人的，合并去重
    names: list[str] = []
    for s in (c.skills or []):
        if isinstance(s, dict):
            n = s.get("name") or s.get("技能") or ""
        else:
            n = str(s)
        if n:
            names.append(n)

    def _own():
        rows = db.query(SkillORM).filter_by(project_id=c.project_id, owner_id=c.id).all()
        for r in rows:
            label = r.name or ""
            if r.level:
                label += f"({r.level})"
            if (r.effect or "").strip():
                label += f"—{r.effect.strip()[:40]}"
            if label:
                names.append(label)

    _safe(_own)
    if names:
        seen, uniq = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        out.append(f"  技能：{'、'.join(uniq[:8])}")
    return "\n".join(out)


def _render_character_brief(c: CharacterORM) -> str:
    bits = [c.role_type or "角色"]
    if c.gender:
        bits.append(c.gender)
    if c.current_level:
        bits.append(c.current_level)
    tail = (c.brief or c.personality or "").strip().replace("\n", " ")
    if len(tail) > 40:
        tail = tail[:40] + "…"
    return f"· {c.name}（{'，'.join(bits)}）{'— ' + tail if tail else ''}"


def layer_characters(db: Session, project_id: str, focus_names: set[str] | None = None) -> Block | None:
    focus_names = focus_names or set()

    def _load():
        rows = db.query(CharacterORM).filter_by(project_id=project_id).all()
        if not rows:
            return None
        full_rows, brief_rows = [], []
        for c in rows:
            is_lead = (c.role_type or "") in ("主角", "主要角色")
            if is_lead or (c.name in focus_names):
                full_rows.append(c)
            else:
                brief_rows.append(c)

        parts: list[str] = []
        if full_rows:
            parts.append("\n".join(_render_character_full(db, c) for c in full_rows))
        if brief_rows:
            parts.append(
                "其他已登记角色（仅列名，如本章需要展开请保持与下列定位一致）：\n"
                + "\n".join(_render_character_brief(c) for c in brief_rows)
            )
        return "\n\n".join(parts)

    content = _safe(_load)
    if not content:
        return None
    return Block(
        key="characters", title="【角色设定】",
        content=content, priority=P_CHARACTER, order=30, min_chars=300,
    )


# ===========================================================================
# 势力 / 地点 / 关系
# ===========================================================================

def layer_entities(db: Session, project_id: str, focus: dict[str, set[str]] | None = None,
                   mode: str = "full", query_text: str = "") -> Block | None:
    """势力 / 地点 / 关系层。

    mode="full"（默认）：全量（零回归）。
    mode="relevant"：势力/地点本就按 focus 过滤；关系只保留「两端角色任一被本章命中」
    的那些——无关人物的关系不占窗口，但一旦角色出场其社会关系仍可见。
    """
    focus = focus or {}
    hit_f = focus.get("factions", set())
    hit_l = focus.get("locations", set())
    mentioned_chars = focus.get("characters", set())
    sections: list[str] = []

    def _factions():
        rows = db.query(FactionORM).filter_by(project_id=project_id).all()
        if not rows:
            return
        items = []
        for f in rows:
            detailed = f.name in hit_f
            piece = f"· {f.name}"
            if f.territory:
                piece += f"（据{f.territory}）"
            if detailed and (f.description or "").strip():
                piece += f"：{f.description.strip()[:150]}"
            elif (f.description or "").strip():
                piece += f"：{f.description.strip()[:50]}"
            if f.status:
                piece += f" [{f.status}]"
            items.append(piece)
        sections.append("势力：\n" + "\n".join(items))

    def _locations():
        rows = db.query(LocationORM).filter_by(project_id=project_id).all()
        if not rows:
            return
        items = []
        for l in rows:
            detailed = l.name in hit_l
            piece = f"· {l.name}"
            meta = [x for x in (l.location_type, l.region, l.plane) if x]
            if meta:
                piece += f"（{'/'.join(meta)}）"
            desc = (l.description or "").strip()
            if desc:
                piece += f"：{desc[:150] if detailed else desc[:50]}"
            items.append(piece)
        sections.append("地点：\n" + "\n".join(items))

    def _relations():
        rows = db.query(RelationORM).filter_by(project_id=project_id).all()
        if not rows:
            return
        char_rows = db.query(CharacterORM).filter_by(project_id=project_id).all()
        chars = {c.id: c.name for c in char_rows}
        # 按需模式：保留「命中角色 ∪ 主角/主要角色」的社会关系。
        # 主角关系对人设一致性至关重要，不能因 hint 没点名主角就丢；
        # 只裁剪「两个都既非命中、也非主角」的冷僻关系。
        keep_chars = set(mentioned_chars)
        if mode == "relevant":
            keep_chars |= {c.name for c in char_rows
                           if (c.role_type or "") in ("主角", "主要角色")}
        items = []
        for r in rows:
            a = chars.get(r.subject_id, r.subject_id)
            b = chars.get(r.object_id, r.object_id)
            if mode == "relevant" and keep_chars and a not in keep_chars and b not in keep_chars:
                continue
            piece = f"· {a} —{r.relation_type}({r.strength})→ {b}"
            if (r.note or "").strip():
                piece += f"，{r.note.strip()[:40]}"
            items.append(piece)
        if items:
            sections.append("人物关系：\n" + "\n".join(items))

    _safe(_factions)
    _safe(_locations)
    _safe(_relations)

    if not sections:
        return None
    return Block(
        key="entities", title="【势力·地点·关系】",
        content="\n\n".join(sections), priority=P_ENTITY, order=40, min_chars=200,
    )


# ===========================================================================
# 伏笔
# ===========================================================================

def layer_foreshadows(db: Session, project_id: str, trigger_ids: list[str] | None = None,
                      mode: str = "full", query_text: str = "") -> Block | None:
    """伏笔层。

    mode="full"（默认）：全部 pending 照旧（零回归）。
    mode="relevant"：作者指定(trigger_ids)必须项永远保留；其余 pending 按
    description+trigger_condition 与本章要点的相关性裁剪，全不相关时留最近 3 条兜底。
    """
    trigger_ids = trigger_ids or []

    def _load():
        rows = (
            db.query(ForeshadowORM)
            .filter_by(project_id=project_id)
            .filter(ForeshadowORM.enabled.is_(True))
            .all()
        )
        if not rows:
            return None
        must, pending = [], []
        for f in rows:
            desc = (f.description or "").strip()
            if not desc:
                continue
            piece = f"· {desc}"
            if f.buried_chapter:
                piece += f"（埋于第{f.buried_chapter}章）"
            if (f.trigger_condition or "").strip():
                piece += f"，触发条件：{f.trigger_condition.strip()}"
            if f.id in trigger_ids:
                must.append(piece)
            elif (f.status or "pending") == "pending":
                pending.append((f, piece))
        # 按需模式：pending 按与本章要点的相关性裁剪
        if mode == "relevant" and pending:
            scored = []
            for f, piece in pending:
                txt = (f.description or "") + " " + (f.trigger_condition or "")
                scored.append((_relevance(txt, query_text), piece))
            scored.sort(key=lambda x: -x[0])
            kept_pending = [p for rel, p in scored if rel > 0]
            if not kept_pending:
                kept_pending = [p for _, p in scored[:3]]  # 全不相关则留最近 3 条兜底
            pending_pieces = kept_pending
        else:
            pending_pieces = [p for _, p in pending]
        out = []
        if must:
            out.append("本章必须呼应（作者指定）：\n" + "\n".join(must))
        if pending_pieces:
            out.append("尚未回收的伏笔（可自然带过，勿强行收束）：\n" + "\n".join(pending_pieces[:15]))
        return "\n\n".join(out) if out else None

    content = _safe(_load)
    if not content:
        return None
    return Block(
        key="foreshadows", title="【伏笔状态】",
        content=content, priority=P_FORESHADOW, order=50, min_chars=100,
    )


# ===========================================================================
# L5 上一章结尾 + L4 最近章记忆 + L3 阶段摘要
# ===========================================================================

def layer_prev_chapter(db: Session, project_id: str, chapter_no: int) -> Block | None:
    def _load():
        prev = (
            db.query(ChapterORM)
            .filter_by(project_id=project_id)
            .filter(ChapterORM.chapter_no < chapter_no)
            .order_by(ChapterORM.chapter_no.desc())
            .first()
        )
        if prev is None:
            return None
        body = (prev.content or "").strip()
        if not body:
            return None
        tail = body[-PREV_TAIL_CHARS:]
        head = f"上一章（第{prev.chapter_no}章 {prev.title or ''}）的结尾原文，本章开头必须自然接住："
        return f"{head}\n…{tail}"

    content = _safe(_load)
    if not content:
        return None
    return Block(
        key="prev_tail", title="【上一章结尾】",
        content=content, priority=P_CONTINUITY, order=60, min_chars=200,
    )


def layer_chapter_plan(db: Session, project_id: str, article_id: str | None,
                       chapter_no: int | None) -> Block | None:
    """本篇规划中「本章」的任务卡（Phase 7.2，2026-09-11）。

    规划是**作者拍板过的**（`article_plans.status="confirmed"`）—— 所以 priority 用
    P_CRITICAL（等同"本章要点"级，永不裁剪）。包含：本章节拍 / 剧情要点 / 章尾钩子 /
    目标字数 / 可引新角色 / 应召回老角色，以及前后章的计划（让本章知道自己在篇里的位置）。
    """
    def _load():
        if not article_id or chapter_no is None:
            return None
        plan = (
            db.query(ArticlePlanORM)
            .filter_by(project_id=project_id, article_id=article_id, status="confirmed")
            .order_by(ArticlePlanORM.updated_at.desc())
            .first()
        )
        if plan is None:
            return None
        lines = (plan.plan or {}).get("lines") or []

        def _line(no: int):
            for x in lines:
                try:
                    if int(x.get("no") or 0) == no:
                        return x
                except (TypeError, ValueError):
                    continue
            return None

        line = _line(int(chapter_no))
        if not line:
            return None
        parts: list[str] = []
        if line.get("template_ref"):
            parts.append(f"本篇套路参考：{line['template_ref']}")
        if line.get("beat"):
            parts.append(f"本章节拍（必须踩到）：{line['beat']}")
        if line.get("summary"):
            parts.append(f"本章剧情要点：{line['summary']}")
        if line.get("hook"):
            parts.append(f"章尾钩子（必须埋）：{line['hook']}")
        if line.get("target_words"):
            parts.append(f"目标字数：约 {line['target_words']} 字")
        if line.get("new_chars"):
            parts.append(f"可引入新角色：{'、'.join(map(str, line['new_chars']))}")
        if line.get("recall_chars"):
            parts.append(f"应召回的老角色：{'、'.join(map(str, line['recall_chars']))}")
        prev_l = _line(int(chapter_no) - 1)
        next_l = _line(int(chapter_no) + 1)
        if prev_l and prev_l.get("summary"):
            parts.append(f"上一章（第{prev_l['no']}章）计划要点：{prev_l['summary']}")
        if next_l and next_l.get("summary"):
            parts.append(f"下一章（第{next_l['no']}章）将写：{next_l['summary']}")
        if not parts:
            return None
        return "（以下任务来自作者已确认的篇规划，优先级最高，必须全部完成）\n" + "\n".join(parts)

    content = _safe(_load)
    if not content:
        return None
    return Block(
        key="chapter_plan", title="【本篇规划 · 本章任务（作者已确认）】",
        content=content, priority=P_CRITICAL, order=18, min_chars=80,
    )


def layer_recent_memories(db: Session, project_id: str, chapter_no: int, limit: int = 3) -> Block | None:
    def _load():
        rows = (
            db.query(ChapterMemoryORM)
            .filter_by(project_id=project_id)
            .filter(ChapterMemoryORM.chapter_no < chapter_no)
            .order_by(ChapterMemoryORM.chapter_no.desc())
            .limit(max(1, limit))
            .all()
        )
        if not rows:
            return None
        rows = list(reversed(rows))  # 按时间正序读更符合阅读直觉
        items = []
        for m in rows:
            piece = [f"第{m.chapter_no}章 {m.title or ''}：{(m.summary or '').strip()}"]
            if (m.ending_hook or "").strip():
                piece.append(f"  结尾悬念：{m.ending_hook.strip()}")
            if m.plot_points:
                pts = "；".join(str(p) for p in m.plot_points[:4])
                piece.append(f"  关键事件：{pts}")
            items.append("\n".join(piece))
        return "\n".join(items)

    content = _safe(_load)
    if not content:
        return None
    return Block(
        key="recent_memory", title="【最近章节回顾】",
        content=content, priority=P_MEMORY_RECENT, order=55, min_chars=200,
    )


def layer_stage_summaries(db: Session, project_id: str, chapter_no: int) -> Block | None:
    def _load():
        rows = (
            db.query(StageSummaryORM)
            .filter_by(project_id=project_id)
            .filter(StageSummaryORM.to_chapter_no < chapter_no)
            .order_by(StageSummaryORM.from_chapter_no)
            .all()
        )
        if not rows:
            return None
        items = []
        for s in rows:
            piece = [f"第{s.from_chapter_no}~{s.to_chapter_no}章：{(s.summary or '').strip()}"]
            if s.open_threads:
                piece.append(f"  未收束线索：{'；'.join(str(t) for t in s.open_threads[:5])}")
            items.append("\n".join(piece))
        return "\n".join(items)

    content = _safe(_load)
    if not content:
        return None
    return Block(
        key="stage_memory", title="【前情脉络（阶段压缩）】",
        content=content, priority=P_MEMORY_STAGE, order=52, min_chars=150,
    )


# ===========================================================================
# 商讨记录
# ===========================================================================

def layer_discussion(db: Session, project_id: str, chapter_id: str | None = None,
                     conversation_id: str | None = None, limit: int = 8) -> Block | None:
    def _load():
        q = (
            db.query(DiscussionMessageORM)
            .filter_by(project_id=project_id)
            .filter(DiscussionMessageORM.archived_chapter_id.is_(None))
        )
        # 线程优先级与写入侧严格对称：章线程 > 会话线程 > 小说级默认线程（两者皆 NULL）
        if chapter_id:
            q = q.filter(DiscussionMessageORM.chapter_id == chapter_id)
        elif conversation_id:
            q = q.filter(DiscussionMessageORM.conversation_id == conversation_id)
        else:
            q = q.filter(
                DiscussionMessageORM.chapter_id.is_(None),
                DiscussionMessageORM.conversation_id.is_(None),
            )
        rows = q.order_by(DiscussionMessageORM.created_at.desc()).limit(max(1, limit)).all()
        if not rows:
            return None
        rows = list(reversed(rows))
        items = []
        for m in rows:
            who = "作者" if m.role == "user" else "助手"
            body = (m.content or "").strip().replace("\n", " ")
            if len(body) > 400:
                body = body[:400] + "…"
            if body:
                items.append(f"{who}：{body}")
        return "\n".join(items) if items else None

    content = _safe(_load)
    if not content:
        return None
    return Block(
        key="discussion", title="【作者近期的剧情商讨（这是作者的真实意图，优先遵循）】",
        content=content, priority=P_DISCUSSION, order=15, min_chars=200,
    )
