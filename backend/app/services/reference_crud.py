"""参考文档 CRUD 服务（分作品隔离，project_id 过滤）。

提供 get_references_corpus() 供后续「章节生成」模块检索拼接，
将本作品全部参考文档文本拼为一段上下文，直接注入生成 Prompt。
"""
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.orm import ReferenceDocORM, SettingORM, SettingTemplateORM
from app.schemas.reference import ReferenceDocCreate, ReferenceDoc, ReferenceDocSummary

# 单次正文上限（字符），防止超大文档撑爆上下文；超过仅截断并标注。
MAX_CONTENT_CHARS = 200_000

# 全局共享参考文档（「参考资料」池）用特殊 project_id 存储，与「每本小说参考文档」共表隔离。
GLOBAL_PROJECT_ID = "__global__"

# 篇章参考文档固定文件名（每篇一个，AI 生成章后写入）
ARTICLE_REF_FILENAME = "篇章参考"


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_summary(o: ReferenceDocORM) -> ReferenceDocSummary:
    return ReferenceDocSummary.model_validate(o)


def _to_full(o: ReferenceDocORM) -> ReferenceDoc:
    return ReferenceDoc.model_validate(o)


def list_references(db: Session, project_id: str) -> list[ReferenceDocSummary]:
    rows = (
        db.query(ReferenceDocORM)
        .filter_by(project_id=project_id)
        .order_by(ReferenceDocORM.created_at)
        .all()
    )
    return [_to_summary(r) for r in rows]


def auto_summary(content: str, limit: int = 220) -> str:
    """从正文首段抽一句话当摘要。

    没有 AI 也要有 summary——相关性打分和「其余文档一行式」注入都靠它。
    后续可由写后摄取用模型重写得更准，这里只保证不为空。
    """
    text = (content or "").strip()
    if not text:
        return ""
    for para in text.split("\n"):
        p = para.strip()
        # 跳过 Markdown 标题、分隔线这类没信息量的行
        if len(p) >= 10 and not p.startswith(("#", "---", "===", "|")):
            return p[:limit] + ("…" if len(p) > limit else "")
    return text[:limit]


def auto_tags(filename: str, content: str) -> list[str]:
    """从文件名切出检索标签。文件名往往就是最准的主题词（「太玄宗设定.md」）。"""
    stem = (filename or "").rsplit(".", 1)[0]
    raw = re.split(r"[\s_\-—·、,，。()（）\[\]【】]+", stem)
    tags = [t.strip() for t in raw if len(t.strip()) >= 2]
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:8]


def create_reference(db: Session, project_id: str, data: ReferenceDocCreate) -> ReferenceDoc:
    content = data.content_text or ""
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n…(内容超出上限，已截断)"
    now = _now()
    o = ReferenceDocORM(
        id=uuid.uuid4().hex,
        project_id=project_id,
        filename=data.filename,
        content_type=data.content_type or "text/plain",
        size=data.size or len(content.encode("utf-8")),
        content_text=content,
        summary=auto_summary(content),
        tags=auto_tags(data.filename, content),
        source="global" if project_id == GLOBAL_PROJECT_ID else "upload",
        created_at=now,
        updated_at=now,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    _index_doc_silent(db, o)  # A3：上传即建向量索引（无 key 静默跳过）
    return _to_full(o)


def _index_doc_silent(db: Session, o: ReferenceDocORM) -> None:
    """文档落库后同步建向量索引。失败静默——向量是增强能力，不阻断上传主流程。"""
    try:
        from app.services import vector_index
        vector_index.index_reference_doc(db, o)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[reference_crud] 向量索引跳过: {type(e).__name__}: {str(e)[:80]}")


def get_reference(db: Session, project_id: str, doc_id: str) -> ReferenceDoc | None:
    o = db.query(ReferenceDocORM).filter_by(project_id=project_id, id=doc_id).first()
    return _to_full(o) if o else None


def delete_reference(db: Session, project_id: str, doc_id: str) -> bool:
    o = db.query(ReferenceDocORM).filter_by(project_id=project_id, id=doc_id).first()
    if o is None:
        return False
    try:
        from app.services import vector_index
        vector_index.remove_reference_doc(db, o)  # 向量块与文档同事务删除
    except Exception as e:  # noqa: BLE001
        # 删向量失败不阻断删文档（否则用户删不掉），但留痕：否则会留下孤儿向量块
        # 且用户「删除后仍能检索到」无从解释（Phase 3.5）
        logger.warning(
            f"[reference_crud] 删除文档 {str(doc_id)[:8]} 的向量块失败，将留下孤儿向量: "
            f"{type(e).__name__}: {e}"
        )
    db.delete(o)
    db.commit()
    return True


def update_reference(db: Session, project_id: str, doc_id: str, filename: str) -> ReferenceDoc | None:
    o = db.query(ReferenceDocORM).filter_by(project_id=project_id, id=doc_id).first()
    if o is None:
        return None
    o.filename = filename
    o.tags = auto_tags(filename, o.content_text or "")
    o.updated_at = _now()
    db.commit()
    db.refresh(o)
    return _to_full(o)


def get_references_corpus(db: Session, project_id: str, article_id: str | None = None, limit: int = 10) -> str:
    """检索本作品参考文档，拼为一段上下文文本，供 AI 生成时读取。

    - 默认：本作品（project_id）维度全部参考文档。
    - article_id 给定：额外纳入该篇维度的「篇章参考文档」（每篇一个，AI 写入）。
    limit: 最多拼接的文档数（按上传顺序）。返回空串表示无参考文档。
    """
    q = db.query(ReferenceDocORM).filter_by(project_id=project_id)
    if article_id is not None:
        # 同作品的篇维度文档也一并纳入（project_id 必匹配，再按 article_id 过滤）
        q = q.filter(
            (ReferenceDocORM.article_id.is_(None)) | (ReferenceDocORM.article_id == article_id)
        )
    rows = q.order_by(ReferenceDocORM.created_at).limit(limit).all()
    if not rows:
        return ""
    blocks = []
    for i, o in enumerate(rows, 1):
        scope = "（篇章参考）" if o.article_id else ""
        blocks.append(f"【参考文档 {i}{scope}：{o.filename}】\n{o.content_text}\n")
    return "\n\n".join(blocks)


# ----------------------------------------------------------------------------
# 按需加载（retrieve-on-demand）目录：给 AI 一份「文件清单」，由它决定加载哪些。
# 这样避免把所有参考一股脑塞进上下文；仅选中项才取正文注入。
# ----------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """轻量 token 估算（零依赖）。

    与 budget.py 口径一致：中文 1 token ≈ 1.5 字符。用于目录里展示「这份文件多大」，
    帮助 AI 决策是否加载；后端预算系统仍按字符算，不受此影响。
    """
    chars = len(text or "")
    return round(chars / 1.5)


def build_catalog(
    db: Session,
    project_id: str,
    article_id: str | None = None,
    include_global: bool = False,
) -> list:
    """构造参考文件目录（给 AI 看的清单），不含正文。

    - 默认：本小说（project_id）维度参考文档（不含篇章维度，除非给了 article_id）。
    - include_global=True：并上全局池（GLOBAL_PROJECT_ID）资料，允许 AI 在线拉取全局参考。
    - locked：source=='auto' 或 article_id 非空 的文档（篇章摘要），必备上下文，AI 不可跳过。
    - 排序：locked 置顶；其余按相关性（score_reference）降序，让最相关的排前面，
      减少 AI 在长目录里瞎挑。

    返回 list[ReferenceCatalogItem]。
    """
    from app.schemas.reference import ReferenceCatalogItem

    rows: list[ReferenceDocORM] = []
    q = db.query(ReferenceDocORM).filter_by(project_id=project_id)
    if article_id is not None:
        q = q.filter(
            (ReferenceDocORM.article_id.is_(None)) | (ReferenceDocORM.article_id == article_id)
        )
    else:
        q = q.filter(ReferenceDocORM.article_id.is_(None))
    rows.extend(q.all())

    if include_global and project_id != GLOBAL_PROJECT_ID:
        grow = (
            db.query(ReferenceDocORM)
            .filter_by(project_id=GLOBAL_PROJECT_ID)
            .filter(ReferenceDocORM.article_id.is_(None))
            .all()
        )
        rows.extend(grow)

    items = []
    for o in rows:
        content = o.content_text or ""
        locked = (o.source or "") == "auto" or bool(o.article_id)
        items.append(
            ReferenceCatalogItem(
                id=o.id,
                filename=o.filename,
                summary=o.summary,
                tags=list(o.tags or []),
                source=o.source or "upload",
                size=o.size or len(content.encode("utf-8")),
                content_chars=len(content),
                token_est=estimate_tokens(content),
                article_id=o.article_id,
                locked=locked,
            )
        )

    # locked 置顶；其余按文件名稳定排序（相关性排序需 query_text，目录阶段无 query，保持简单）
    items.sort(key=lambda it: (not it.locked, it.filename))
    return items


def format_catalog_prompt(items: list, include_global: bool = False) -> str:
    """把目录渲染成一段系统提示词前缀，供 AI 决策要加载哪些文件。

    稳定前缀（不随每轮对话变化，除非目录本身变）→ 配合 Ollama cache_prompt 缓存，
    后续轮次前缀 KV 复用，前缀重传成本≈0。
    """
    if not items:
        return ""

    lines = ["", "## 可用参考文件目录（按需加载）",
             "下面是当前作品可参考的资料清单。除非你确定需要其中某份资料来准确回答，"
             "否则不要加载——直接作答即可。\n"
             "需要时在正式回答前先输出一行加载指令（**支持一次请求多个文件，用逗号分隔**）：\n"
             "  LOAD_REFS:<id1>,<id2>,<id3>\n"
             "【重要】如果你的回答需要用到多份资料（例如同时涉及官制和俸禄），"
             "必须把所有相关文件的 id 写在同一行里一次性请求，不要只请求一个然后靠猜。"
             "系统会把所有请求的正文一并注入后再让你作答。带 [必备] 的项已自动加载，无需你请求。", ""]
    for it in items:
        tag = " [必备]" if it.locked else ""
        summary = (it.summary or "").strip().replace("\n", " ")
        tags = " ".join(f"#{t}" for t in it.tags[:6])
        lines.append(
            f"- id={it.id} | {it.filename}{tag} | ~{it.token_est} tokens | {it.source}"
        )
        if summary:
            lines.append(f"    摘要：{summary[:80]}")
        if tags:
            lines.append(f"    标签：{tags}")
    if include_global:
        lines.append("")
        lines.append("（注：以上含全局参考资料池，可直接加载，无需先导入本作品。）")
    lines.append("")
    return "\n".join(lines)


_LOAD_REFS_RE = re.compile(r"LOAD_REFS:\s*([0-9a-fA-F,\s]+)", re.IGNORECASE)


def parse_load_refs(text: str) -> list[str] | None:
    """从模型首轮回复里解析 LOAD_REFS 指令。

    返回选中的 id 列表；若没有该指令则返回 None（表示模型直接作答、无需额外加载）。
    容忍模型夹带少量其它文字——取第一个匹配即可。
    """
    if not text:
        return None
    m = _LOAD_REFS_RE.search(text)
    if not m:
        return None
    ids = [x.strip() for x in m.group(1).split(",") if x.strip()]
    return ids or None


def fetch_refs_by_ids(db: Session, ids: list[str]) -> list[tuple[str, str]]:
    """按 id 批量取参考文档正文（id 在整张表唯一，不限 project）。

    返回 [(filename, content_text), ...]，按传入 id 顺序。用于 retrieve_refs 语义：
    一次往返取回全部选中正文，注入上下文。
    """
    if not ids:
        return []
    rows = db.query(ReferenceDocORM).filter(ReferenceDocORM.id.in_(ids)).all()
    by_id = {o.id: o for o in rows}
    out = []
    for i in ids:
        o = by_id.get(i)
        if o:
            out.append((o.filename, o.content_text or ""))
    return out


# ----------------------------------------------------------------------------
# 设定库按需加载（B 方案）：与参考文档的 LOAD_REFS 平行。
# 设定库（SettingORM）与参考文档（ReferenceDocORM）用不同标记 + 不同 id 空间，
# 避免 id 碰撞；两者可在同一模型首轮文本里同时出现，Pass1 一并解析。
# ----------------------------------------------------------------------------

_LOAD_SETTING_RE = re.compile(r"LOAD_SETTING:\s*([0-9a-fA-F,\s]+)", re.IGNORECASE)


def parse_load_setting(text: str) -> list[str] | None:
    """从模型首轮回复里解析 LOAD_SETTING 指令（设定库按需加载）。

    返回选中的设定 id 列表；若没有该指令则返回 None（表示模型直接作答、无需加载设定）。
    容忍模型夹带少量其它文字——取第一个匹配即可。
    """
    if not text:
        return None
    m = _LOAD_SETTING_RE.search(text)
    if not m:
        return None
    ids = [x.strip() for x in m.group(1).split(",") if x.strip()]
    return ids or None


def fetch_settings_by_ids(db: Session, ids: list[str]) -> list[tuple[str, str]]:
    """按 id 批量取设定库详情（SettingORM.id，整表唯一，不限 project）。

    返回 [(name, detail_text), ...]，按传入 id 顺序。detail 含层级阶梯 + 完整描述，
    供 LOAD_SETTING 语义：一次往返取回全部选中设定，注入上下文。

    2026-09-26 起兼容设定模板（SettingTemplateORM，同一 UUID 空间）：
    旧表未命中的 id 再查模板表，模板整篇 Markdown content 即详情。
    """
    if not ids:
        return []
    rows = db.query(SettingORM).filter(SettingORM.id.in_(ids)).all()
    by_id = {o.id: o for o in rows}
    # 模板表兜底：旧表未命中的 id
    missing = [i for i in ids if i not in by_id]
    tpl_by_id: dict = {}
    if missing:
        trows = (db.query(SettingTemplateORM)
                 .filter(SettingTemplateORM.id.in_(missing)).all())
        tpl_by_id = {o.id: o for o in trows}
    out = []
    for i in ids:
        o = by_id.get(i)
        if o:
            parts = [f"【设定：{o.name}（{o.category or '其它'}）】"]
            if o.levels:
                parts.append("层级阶梯：" + "→".join(str(x) for x in o.levels))
            desc = (o.description or "").strip()
            if desc:
                parts.append(desc)
            out.append((o.name, "\n".join(parts) + "\n"))
            continue
        t = tpl_by_id.get(i)
        if t:
            out.append((f"【设定模板：{t.name}】", (t.content or "").strip() + "\n"))
    return out


def import_global_references(db: Session, project_id: str, doc_ids: list[str]) -> int:
    """将选中的「全局参考资料」复制进指定小说（成为该小说的参考文档）。

    返回实际复制的条数。全局池本身不受影响。
    """
    if not doc_ids:
        return 0
    src = (
        db.query(ReferenceDocORM)
        .filter_by(project_id=GLOBAL_PROJECT_ID)
        .filter(ReferenceDocORM.id.in_(doc_ids))
        .all()
    )
    now = _now()
    count = 0
    created: list[ReferenceDocORM] = []
    for o in src:
        content = o.content_text or ""
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + "\n…(内容超出上限，已截断)"
        copy = ReferenceDocORM(
            id=uuid.uuid4().hex,
            project_id=project_id,
            article_id=None,  # 进入小说维度，非篇章维度
            filename=o.filename,
            content_type=o.content_type or "text/plain",
            size=o.size or len(content.encode("utf-8")),
            content_text=content,
            summary=o.summary or auto_summary(content),
            tags=list(o.tags or []) or auto_tags(o.filename, content),
            source="global",
            created_at=now,
            updated_at=now,
        )
        db.add(copy)
        created.append(copy)
        count += 1
    db.commit()
    for copy in created:
        _index_doc_silent(db, copy)  # A3：导入副本也建向量索引
    return count


# 章节段落分隔标记：用它定位/替换单章内容，实现「追加而不覆盖、重生成而不重复」
_CH_MARK = "<!-- ch:{no} -->"
_CH_MARK_RE = re.compile(r"<!-- ch:(\d+) -->")


def append_article_digest(
    db: Session,
    project_id: str,
    article_id: str,
    chapter_no: int,
    title: str | None,
    digest: str,
) -> None:
    """把一章的**摘要**追加进「篇章参考文档」。

    这里修掉了一个会静默丢数据的 bug：原实现每生成一章就用本章全文
    整体覆盖这份文档，于是写第 2 章时第 1 章的内容被冲掉，
    所谓「篇章参考」永远只剩最后一章。

    现在改为：
    - 按 `<!-- ch:N -->` 标记分段，每章一段，**追加**不覆盖；
    - 同一章重新生成时只替换它自己那一段，不会产生重复段；
    - 存的是摘要不是全文——全文已经在 chapters 表里，
      参考文档存全文既撑爆上下文又没有额外价值。
    """
    digest = (digest or "").strip()
    if not digest:
        return
    now = _now()
    header = f"{_CH_MARK.format(no=chapter_no)}\n## 第{chapter_no}章 {title or ''}".rstrip()
    section = f"{header}\n{digest}"

    existing = (
        db.query(ReferenceDocORM)
        .filter_by(project_id=project_id, article_id=article_id, filename=ARTICLE_REF_FILENAME)
        .first()
    )

    if existing is None:
        body = f"本篇已完成章节的剧情摘要，按章号排列。\n\n{section}"
        db.add(ReferenceDocORM(
            id=uuid.uuid4().hex,
            project_id=project_id,
            article_id=article_id,
            filename=ARTICLE_REF_FILENAME,
            content_type="text/plain",
            size=len(body.encode("utf-8")),
            content_text=body,
            summary="本篇已写章节的剧情摘要索引",
            tags=["篇章摘要", "剧情回顾"],
            source="auto",
            created_at=now,
            updated_at=now,
        ))
        db.commit()
        return

    old = existing.content_text or ""
    marker = _CH_MARK.format(no=chapter_no)
    if marker in old:
        # 该章已有段落（重新生成）：只替换它，直到下一个章节标记为止
        start = old.index(marker)
        rest = old[start + len(marker):]
        nxt = _CH_MARK_RE.search(rest)
        end = start + len(marker) + (nxt.start() if nxt else len(rest))
        new_body = old[:start] + section + ("\n\n" if nxt else "") + old[end:]
    else:
        new_body = old.rstrip() + "\n\n" + section

    if len(new_body) > MAX_CONTENT_CHARS:
        # 超限时从最早的章节开始丢，保住最近的剧情
        marks = list(_CH_MARK_RE.finditer(new_body))
        while len(new_body) > MAX_CONTENT_CHARS and len(marks) > 1:
            new_body = new_body[:marks[0].start()] + new_body[marks[1].start():]
            marks = list(_CH_MARK_RE.finditer(new_body))

    existing.content_text = new_body
    existing.size = len(new_body.encode("utf-8"))
    existing.source = "auto"
    existing.updated_at = now
    db.commit()


def upsert_article_reference(db: Session, project_id: str, article_id: str, content: str) -> None:
    """兼容旧签名。内部已改为追加式摘要，不再整体覆盖。

    没有章号信息时按「未编号」段落追加，避免老调用点静默丢数据。
    """
    append_article_digest(db, project_id, article_id, 0, "未编号", (content or "")[:1500])


# ===========================================================================
# 相关性筛选（需求 2-B：生成时只挑真正用得上的资料）
# ===========================================================================

def score_reference(doc: ReferenceDocORM, query_text: str, entity_names: set[str]) -> float:
    """给文档打相关性分。

    不用向量检索的原因：本地只有 chat 模型没有 embedding 模型，
    而且小说的专有名词（自造人名/门派名）恰恰是关键词匹配最擅长、
    通用嵌入模型最不擅长的部分。

    打分维度：
      标签命中     ×5  —— 标签是人工/半自动标注的主题词，最准
      文件名命中   ×4
      摘要重合     ×2
      实体名出现   ×1.5（上限 5 次）—— 文档里讲到了本章要出场的人/地
    """
    q = query_text or ""
    score = 0.0

    for tag in (doc.tags or []):
        t = str(tag).strip()
        if len(t) >= 2 and t in q:
            score += 5.0

    stem = (doc.filename or "").rsplit(".", 1)[0].strip()
    if len(stem) >= 2 and stem in q:
        score += 4.0

    summary = (doc.summary or "")[:300]
    if summary:
        # 摘要里的 2-gram 有多少出现在 query 里——粗糙但对中文很有效，且零依赖
        grams = {summary[i:i + 2] for i in range(len(summary) - 1)}
        if grams:
            hit = sum(1 for g in grams if g in q)
            score += min(hit / len(grams) * 6.0, 3.0)

    body = doc.content_text or ""
    if body and entity_names:
        for name in entity_names:
            c = body.count(name)
            if c:
                score += min(c, 5) * 1.5

    return round(score, 2)


def pick_relevant(
    db: Session,
    project_id: str,
    article_id: str | None,
    query_text: str,
    entity_names: set[str] | None = None,
    top_k: int = 4,
    per_doc_chars: int = 3000,
) -> tuple[str, list[dict]]:
    """挑出与本章相关的参考文档，拼成上下文（A3+A4：关键词/向量双通道 RRF + rerank 重排）。

    双通道：
      通道一 关键词 —— score_reference（标签/文件名/摘要/实体名），专有名词最强项；
      通道二 向量   —— vector_index.search_similar（bge-m3 语义检索），
                       未配 key / 未建索引时静默为空，行为退化回纯关键词版。
    融合：RRF（Reciprocal Rank Fusion，k=60 标准参数）——
      score(doc) = Σ 1/(60 + rank_通道(doc))，只对出现在至少一个通道的文档计分。
      RRF 只用名次不用原始分，两路量纲不同也无需调权。
    重排（A4）：RRF 取 top_k×3 候选 → bge-reranker-v2-m3 交叉编码器逐对打分 →
      取 top_k。rerank 失败静默降级为 RRF 原序。

    返回 (拼接文本, 命中明细)。明细供前端展示「本章参考了哪几份资料」。

    篇章摘要（source=auto）永远置顶且不参与竞争——那是本篇已发生的剧情，
    属于必备上下文，不是可选参考。
    """
    entity_names = entity_names or set()
    try:
        q = db.query(ReferenceDocORM).filter_by(project_id=project_id)
        if article_id is not None:
            q = q.filter(
                (ReferenceDocORM.article_id.is_(None))
                | (ReferenceDocORM.article_id == article_id)
            )
        else:
            q = q.filter(ReferenceDocORM.article_id.is_(None))
        rows = q.all()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[reference_crud.pick_relevant] 查询失败: {e}")
        return "", []

    if not rows:
        return "", []

    must = [r for r in rows if (r.source or "") == "auto" or r.article_id]
    optional = [r for r in rows if r not in must]

    # ---- 通道一：关键词 ----
    kw_scored = sorted(
        ((score_reference(d, query_text, entity_names), d) for d in optional),
        key=lambda x: (-x[0], x[1].created_at or _now()),
    )
    kw_rank = {d.id: i for i, (s, d) in enumerate(kw_scored, 1) if s > 0}

    # ---- 通道二：向量（失败/未配置静默为空）----
    vec_rank: dict[str, int] = {}
    vec_top: dict[str, float] = {}
    try:
        from app.services import vector_index
        optional_ids = {d.id for d in optional}
        hits = vector_index.search_similar(
            db, project_id, "ref_doc", query_text, top_k=max(8, top_k * 3))
        for h in hits:
            if h.source_id not in optional_ids:
                continue
            if h.source_id not in vec_top or h.score > vec_top[h.source_id]:
                vec_top[h.source_id] = h.score  # 块级得分聚合到文档级：取最好块
        for i, sid in enumerate(sorted(vec_top, key=lambda k: -vec_top[k]), 1):
            vec_rank[sid] = i
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[reference_crud.pick_relevant] 向量通道跳过: {type(e).__name__}: {str(e)[:80]}")

    # ---- RRF 融合 ----
    _RRF_K = 60

    def _rrf(doc_id: str) -> float:
        s = 0.0
        if doc_id in kw_rank:
            s += 1.0 / (_RRF_K + kw_rank[doc_id])
        if doc_id in vec_rank:
            s += 1.0 / (_RRF_K + vec_rank[doc_id])
        return s

    picked = sorted(optional, key=lambda d: (-_rrf(d.id), d.created_at or _now()))
    candidates = [d for d in picked if _rrf(d.id) > 0][: max(top_k * 3, top_k + 4)]

    # ---- A4：rerank 重排（RRF 候选 → 交叉编码器逐对打分 → 取 top_k）----
    # RRF 只保证「大致相关」；bge-reranker 区分度实测高一个量级
    # （0.128 / 0.035 / 0.010 / 0.000 vs bge-m3 余弦 0.5174/0.5166/0.5141）。
    # 失败/无 key 静默降级为 RRF 原序，绝不阻断生成。
    rerank_scores: dict[str, float] = {}
    if len(candidates) > top_k:
        try:
            from app.services import rerank_client
            order = rerank_client.rerank(
                query_text, [d.summary or d.filename or "" for d in candidates], db=db)
            ranked = [candidates[it["index"]] for it in order]
            for it in order:
                rerank_scores[candidates[it["index"]].id] = it["relevance_score"]
            # rerank 返回完整的候选序；取前 top_k（分数为 0 的仍保留，交由 RRF 序兜底）
            picked = ranked[: max(0, top_k)]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reference_crud.pick_relevant] rerank 跳过: {type(e).__name__}: {str(e)[:80]}")
            picked = candidates[: max(0, top_k)]
    else:
        picked = candidates[: max(0, top_k)]

    # 一份都没命中时，退回最早上传的一份保底——总比完全没有世界观参考强
    if not picked and optional:
        picked = [optional[0]]

    detail: list[dict] = []
    blocks: list[str] = []

    for d in must:
        text = (d.content_text or "").strip()
        if not text:
            continue
        blocks.append(f"【本篇已写剧情摘要】\n{text[:per_doc_chars * 2]}")
        detail.append({"id": d.id, "filename": d.filename, "score": None, "reason": "本篇剧情摘要（必备）"})

    kw_score_of = {d.id: s for s, d in kw_scored}
    for d in picked:
        text = (d.content_text or "").strip()
        if not text:
            continue
        clipped = text[:per_doc_chars]
        if len(text) > per_doc_chars:
            clipped += f"\n…（全文 {len(text)} 字，此处截取前 {per_doc_chars} 字）"
        blocks.append(f"【参考资料：{d.filename}】\n{clipped}")
        channels = [c for c, hit in (("关键词", d.id in kw_rank), ("向量", d.id in vec_rank)) if hit]
        detail.append({
            "id": d.id, "filename": d.filename,
            "score": round(_rrf(d.id), 6),
            "kw_score": kw_score_of.get(d.id, 0.0),
            "vec_score": round(vec_top.get(d.id, 0.0), 4),
            "rerank_score": (round(rerank_scores[d.id], 4)
                             if d.id in rerank_scores else None),
            "channels": channels or ["保底"],
            "reason": ("+".join(channels) + " 相关性命中") if channels else "保底（无命中）",
        })

    # 未入选的只留一行索引，让模型知道「还有这些资料存在」
    rest = [d for d in optional if d not in picked]
    if rest:
        lines = [f"· {d.filename}：{(d.summary or '')[:60]}" for d in rest[:12]]
        blocks.append("【其余可用资料（本章未展开，如需要请在要点中指明）】\n" + "\n".join(lines))

    return "\n\n".join(blocks), detail


def recommend_global_references(
    db: Session, project_id: str, top_k: int = 8
) -> list[dict]:
    """需求 2-A：从全局资料池里推荐值得导入本作品的资料。

    用作品名/题材/梗概 + 已有角色势力地点名当查询，对全局池打分排序。
    只给建议，导入与否由作者点确认——这是「AI 判断哪些资料需要借鉴」的入口。
    """
    from app.models.orm import ProjectORM, CharacterORM, FactionORM, LocationORM

    try:
        p = db.query(ProjectORM).filter_by(id=project_id).first()
        query_bits = []
        if p:
            query_bits += [p.name or "", p.genre or "", p.summary or ""]
        names: set[str] = set()
        for model in (CharacterORM, FactionORM, LocationORM):
            for r in db.query(model).filter_by(project_id=project_id).all():
                if r.name:
                    names.add(r.name)
        query_bits += list(names)
        query_text = " ".join(query_bits)

        existing_names = {
            r.filename for r in db.query(ReferenceDocORM).filter_by(project_id=project_id).all()
        }
        pool = db.query(ReferenceDocORM).filter_by(project_id=GLOBAL_PROJECT_ID).all()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[reference_crud.recommend_global_references] 失败: {e}")
        return []

    out = []
    for d in pool:
        if d.filename in existing_names:
            continue  # 已导入过的不再推荐
        s = score_reference(d, query_text, names)
        out.append({
            "id": d.id,
            "filename": d.filename,
            "summary": (d.summary or "")[:120],
            "tags": d.tags or [],
            "size": d.size,
            "score": s,
            "recommended": s >= 4.0,
        })
    out.sort(key=lambda x: -x["score"])
    return out[:max(1, top_k)]
