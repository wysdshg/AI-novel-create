"""写后摄取：一章写完之后，自动把它变成「结构化记忆」。

这是需求 1（AI 写数据库）和需求 4（章后给走向建议）的落点。

为什么不在生成时同步做：生成已经是流式长任务，再串一次抽取会让作者干等。
所以摄取放在正文落库之后单独跑，失败也只影响记忆，不影响正文。

抽取失败的兜底很关键——本地 4B 输出 JSON 的成功率大概八成。
剩下两成如果直接放弃，记忆表就会出现空洞，下一章读不到前情。
所以准备了规则兜底：正文首尾截取 + 已登记实体子串匹配，
质量不如模型抽取，但保证「记忆链不断」。
"""
import logging
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.context import layers
from app.core.gateway.registry import get_adapter
from app.models.orm import ChapterORM, ArticleORM, VolumeORM, ProjectORM
from app.services import (
    app_config, discussion_crud, memory_crud, model_crud,
    reference_crud, skill_dispatch,
)

# 摄取用的抽取指令。刻意用「填空题」形式而不是开放描述——
# 4B 模型对「照着模板填」的遵循度远高于「请你总结一下」。
EXTRACT_SYSTEM = (
    "你是小说编辑助手，负责把一章正文压缩成结构化档案。\n"
    "只输出一个 JSON 对象，不要输出任何解释、前言、Markdown 代码块标记。\n"
    "所有字段都必须用中文填写。字段说明：\n"
    '{\n'
    '  "summary": "本章剧情摘要，150~250字，只写发生了什么，不要评价",\n'
    '  "ending_hook": "本章结尾停在哪里、留下什么悬念，一句话",\n'
    '  "characters": ["本章出场的角色名"],\n'
    '  "locations": ["本章出现的地点名"],\n'
    '  "plot_points": ["关键事件，3~5条，每条一句话"],\n'
    '  "foreshadow_actions": [{"action": "bury/hint/resolve", "desc": "涉及的伏笔"}],\n'
    '  "new_entities": [{"kind": "character/faction/location", "name": "名称", '
    '"brief": "一句话说明"}],\n'
    '  "next_directions": [{"title": "走向标题", "detail": "具体怎么写，两三句", '
    '"tension": "高/中/低"}]\n'
    '}\n'
    "next_directions 给 3 条，要求方向彼此不同（不要三条都是打一架），"
    "并且必须能从本章结尾自然接上。\n"
    "new_entities 只填本章新出现、且资料里没有的；没有就给空数组。"
)

_JSON_KEYS = (
    "summary", "ending_hook", "characters", "locations",
    "plot_points", "foreshadow_actions", "new_entities", "next_directions",
)


# ===========================================================================
# JSON 抽取容错
# ===========================================================================

logger = logging.getLogger(__name__)


def parse_json_loose(text: str) -> dict | None:
    """从模型输出里把 JSON 抠出来。

    实测本地小模型常见的四种脏输出：包在 ```json 里、前面带一句「好的」、
    结尾多个逗号、以及中文全角引号。这里逐个处理，能救一个是一个。
    """
    if not text:
        return None
    s = text.strip()

    # 去掉代码块围栏
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)

    # 取第一个 { 到最后一个 } 之间
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return None
    body = s[i:j + 1]

    for attempt in range(3):
        try:
            data = json.loads(body)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            if attempt == 0:
                # 尾逗号
                body = re.sub(r",\s*([}\]])", r"\1", body)
            elif attempt == 1:
                # 全角引号 → 半角（只换成对出现的）
                body = body.replace("“", '"').replace("”", '"')
            else:
                return None
    return None


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        parts = [p.strip() for p in re.split(r"[；;\n]", v) if p.strip()]
        return parts
    return [v]


def normalize_extract(data: dict) -> dict:
    """把模型可能给歪的结构掰回标准形状。"""
    out: dict[str, Any] = {}
    out["summary"] = str(data.get("summary") or "").strip()
    out["ending_hook"] = str(data.get("ending_hook") or "").strip()
    out["characters"] = [str(x).strip() for x in _as_list(data.get("characters")) if str(x).strip()]
    out["locations"] = [str(x).strip() for x in _as_list(data.get("locations")) if str(x).strip()]
    out["plot_points"] = [str(x).strip() for x in _as_list(data.get("plot_points")) if str(x).strip()][:6]

    fa = []
    for item in _as_list(data.get("foreshadow_actions")):
        if isinstance(item, dict):
            desc = str(item.get("desc") or item.get("description") or "").strip()
            act = str(item.get("action") or "hint").strip()
            if desc:
                fa.append({"action": act, "desc": desc})
        elif str(item).strip():
            fa.append({"action": "hint", "desc": str(item).strip()})
    out["foreshadow_actions"] = fa[:8]

    ne = []
    for item in _as_list(data.get("new_entities")):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                ne.append({
                    "kind": str(item.get("kind") or "character").strip(),
                    "name": name,
                    "brief": str(item.get("brief") or "").strip(),
                })
    out["new_entities"] = ne[:10]

    nd = []
    for item in _as_list(data.get("next_directions")):
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if title or detail:
                nd.append({
                    "title": title or detail[:20],
                    "detail": detail,
                    "tension": str(item.get("tension") or "中").strip(),
                })
        elif str(item).strip():
            nd.append({"title": str(item).strip()[:20], "detail": str(item).strip(), "tension": "中"})
    out["next_directions"] = nd[:5]
    return out


def fallback_extract(db: Session, project_id: str, content: str) -> dict:
    """模型不可用或解析失败时的规则兜底。

    摘要用首段 + 尾段拼（开头交代场景、结尾是钩子，中间过程可以省），
    出场实体用资料库已登记名做子串匹配。粗，但不会断链。
    """
    body = (content or "").strip()
    paras = [p.strip() for p in body.split("\n") if p.strip()]
    head = "".join(paras[:2])[:180] if paras else ""
    tail = "".join(paras[-2:])[-160:] if paras else ""
    summary = (head + ("……" if head and tail else "") + tail).strip()

    mentions = layers.extract_mentions(db, project_id, body)
    return {
        "summary": summary or "（本章摘要抽取失败，以下为空档，可点重新摄取）",
        "ending_hook": tail[-80:] if tail else "",
        "characters": sorted(mentions.get("characters", set())),
        "locations": sorted(mentions.get("locations", set())),
        "plot_points": [],
        "foreshadow_actions": [],
        "new_entities": [],
        "next_directions": [],
        "_fallback": True,
    }


# ===========================================================================
# 模型选择
# ===========================================================================

def _pick_model(db: Session):
    """优先用 role='memory' 的模型（抽取任务可以用更小更快的），否则用默认模型。"""
    from app.models.orm import ModelConfigORM
    try:
        m = (
            db.query(ModelConfigORM)
            .filter(ModelConfigORM.role == "memory")
            .filter(ModelConfigORM.status == "active")
            .first()
        )
        if m:
            return m
    except Exception as e:  # noqa: BLE001
        # 查 memory 角色模型失败 → 继续走 get_default 兜底。留痕，便于区分
        # 「确实没配 memory 模型」与「查表本身出错」（Phase 3.5）
        logger.warning(f"[ingestion] 查 memory 角色模型失败，将回退默认模型: {type(e).__name__}: {e}")
    d = model_crud.get_default(db)
    if d is not None and (d.status or "active") == "active":
        return d
    return None


def _model_config(m) -> dict:
    return {
        "api_base": m.api_base,
        "api_key": m.api_key,
        "model_name": m.model_name,
        "temperature": 0.2,          # 抽取是信息任务，温度必须压低
        "top_p": m.top_p,
        "max_tokens": min(m.max_tokens or 2000, 2000),
        "enable_thinking": False,    # 思考过程会污染 JSON 输出
    }


# ===========================================================================
# 主入口
# ===========================================================================

def ingest_chapter(
    db: Session,
    project_id: str,
    chapter: ChapterORM,
    push_directions: bool | None = None,
    push_chapter_id: str | None = None,
    push_conversation_id: str | None = None,
    extract: bool | None = None,
    aggregate: bool | None = None,
) -> dict:
    """一章写完后的全部收尾动作。

    顺序：抽记忆 → 落库 → 写篇章摘要（给参考文档）→ 推走向卡片到对话区
    → 够章数就压阶段摘要。

    每步独立 try，前一步失败不阻断后一步——记忆抽歪了不该导致摘要也不写。

    分阶段开关（默认 None = 读 app_config，行为与旧版完全一致）：
    - extract=False：跳过 LLM 抽取，改走 fallback_extract 规则兜底。
      **不是**整段跳过——规则摘要仍会落库、篇章摘要与走向卡片链路不断，
      只是摘要不如 AI 精炼。目的是「省一次调用但保留数据链」（测试多轮验证用）。
    - aggregate=False：概览聚合的篇级不再调 LLM，退回 _concat_summary 拼接。
      概览页照样有内容（展示层，不影响生成质量）。
    """
    result: dict[str, Any] = {"chapter_id": chapter.id, "chapter_no": chapter.chapter_no}
    content = (chapter.content or "").strip()
    if not content:
        result["skipped"] = "章节正文为空"
        return result

    # 分阶段开关解析：显式入参优先，否则读全局配置（默认 True，保持旧行为）
    if extract is None:
        extract = bool(app_config.get(db, app_config.KEY_EXTRACT_ENABLED, True))
    if aggregate is None:
        aggregate = bool(app_config.get(db, app_config.KEY_AGGREGATE_OVERVIEW, True))
    result["extract_llm"] = bool(extract)
    result["aggregate_llm"] = bool(aggregate)

    # ---------- 1. 抽取 ----------
    extracted: dict | None = None
    raw_out = None
    m = _pick_model(db)
    if m is not None and extract:
        sys_parts = [EXTRACT_SYSTEM]
        skill_block = skill_dispatch.build_block(db, "memory")
        if skill_block:
            sys_parts.append(skill_block)
        # 超长正文截首尾，中间大段打斗描写对抽取贡献有限
        clip = content if len(content) <= 8000 else content[:5000] + "\n…（中略）…\n" + content[-3000:]
        messages = [
            {"role": "system", "content": "\n\n".join(sys_parts)},
            {"role": "user", "content": f"第{chapter.chapter_no}章 {chapter.title or ''}\n\n{clip}"},
        ]
        try:
            adapter = get_adapter(m.vendor, _model_config(m))
            raw_out = adapter.chat(messages)
            parsed = parse_json_loose(raw_out or "")
            if parsed:
                extracted = normalize_extract(parsed)
                if not extracted.get("summary"):
                    extracted = None  # 摘要都空，等于没抽出来
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ingestion] 记忆抽取调用失败: {type(e).__name__}: {str(e)[:150]}")

    used_fallback = extracted is None
    if used_fallback:
        extracted = fallback_extract(db, project_id, content)
    result["fallback"] = used_fallback
    # 区分「LLM 抽取失败」与「按开关主动跳过」——日志/测试断言需要能分辨，
    # 否则省调用被误读成抽取坏了。
    result["fallback_reason"] = (
        "extract_disabled" if (not extract) else ("llm_failed" if used_fallback else None)
    )
    # aggregate 开关由调用方（chapter.py 的后台线程）读取后决定是否跑概览聚合——
    # 聚合不在本函数内，故把解析结果透出去，避免调用方再读一次配置造成不一致。
    result["_aggregate"] = bool(aggregate)

    # ---------- 2. 落库 ----------
    try:
        mem = memory_crud.upsert_chapter_memory(
            db, project_id, chapter.id,
            {
                **extracted,
                "article_id": chapter.article_id,
                "chapter_no": chapter.chapter_no,
                "title": chapter.title,
                "status": "pending" if extracted.get("new_entities") else "confirmed",
                "raw": (raw_out or "")[:4000] if used_fallback else None,
            },
        )
        result["memory_id"] = mem.id
        result["summary"] = mem.summary
        result["new_entities"] = mem.new_entities or []
        result["next_directions"] = mem.next_directions or []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ingestion] 记忆落库失败: {e}")
        result["memory_error"] = str(e)[:200]

    # ---------- 2.6 伏笔回注（Phase 2.1）----------
    # 补齐「最后一米」：`foreshadow_actions` 此前只存进章级记忆、从不写 `foreshadows` 表，
    # 导致 AI 每章白抽、`layer_foreshadows` 的读路径永远读不到数据。这里写回。
    # 幂等（重跑同一章不重复建），失败静默不阻断后续步骤。
    try:
        from app.services import foreshadow_crud
        fs_stats = foreshadow_crud.sync_from_actions(
            db, project_id, chapter.chapter_no, extracted.get("foreshadow_actions"))
        result["foreshadow_sync"] = fs_stats
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ingestion] 伏笔回注失败: {type(e).__name__}: {e}")

    # ---------- 3. 篇章摘要写进参考文档（追加，不覆盖） ----------
    if chapter.article_id:
        try:
            digest_lines = [extracted.get("summary", "")]
            if extracted.get("plot_points"):
                digest_lines.append("关键事件：" + "；".join(extracted["plot_points"]))
            if extracted.get("ending_hook"):
                digest_lines.append("结尾：" + extracted["ending_hook"])
            reference_crud.append_article_digest(
                db, project_id, chapter.article_id,
                chapter_no=chapter.chapter_no,
                title=chapter.title or f"第{chapter.chapter_no}章",
                digest="\n".join(x for x in digest_lines if x),
            )
            result["digest_written"] = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ingestion] 写篇章摘要失败: {e}")

    # ---------- 3.5 向量索引同步（A 线检索升级，失败静默不阻断） ----------
    try:
        from app.services import vector_index
        vec_stats = vector_index.sync_after_ingest(
            db, project_id, result.get("memory_id") or "", chapter.article_id)
        result["vector_index"] = vec_stats
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ingestion] 向量索引同步跳过: {type(e).__name__}: {e}")

    # ---------- 4. 走向卡片推送到对话区 ----------
    # 2026-09-13 用户拍板下线：默认不再每章自动推「三条路」走向卡片（省心不省链路——
    # 抽取 prompt 的 next_directions 字段与本推送块代码全保留，恢复只需开配置）。
    if push_directions is None:
        # 硬编码兜底也置 False（与 DEFAULTS 保持一致）：即使未来该键被移出 DEFAULTS，
        # 也不会静默复活自动推送。显式传参 push_directions=True 仍可强制开。
        push_directions = bool(app_config.get(db, app_config.KEY_PUSH_DIRECTIONS, False))
    dirs = extracted.get("next_directions") or []
    if push_directions and dirs:
        try:
            body = _render_directions(chapter.chapter_no, dirs)
            # 走向建议推回「被生成的这一章」自己的对话线程（push_chapter_id=chapter.id）：
            # 保证「第N章的走向出现在第N章的对话框」，而不是小说级默认线程（问题2 根因）。
            # thread 优先级：conversation_id > chapter_id > 小说级；push_conversation_id 为 None
            # 时完全由 chapter_id 决定归属（调用方在 chapter.py 传入被生成章的 id）。
            discussion_crud.add_message(
                db, project_id, role="assistant", content=body,
                chapter_id=push_chapter_id,
                conversation_id=push_conversation_id,
                meta={
                    "type": "post_chapter_directions",
                    "chapter_id": chapter.id,
                    "chapter_no": chapter.chapter_no,
                    "directions": dirs,
                },
            )
            result["directions_pushed"] = len(dirs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ingestion] 推送走向卡片失败: {e}")

    # ---------- 5. 阶段压缩 ----------
    try:
        every = int(app_config.get(db, app_config.KEY_STAGE_EVERY, 10) or 10)
        rng = memory_crud.find_uncompressed_range(db, project_id, every)
        if rng:
            s = compress_stage(db, project_id, rng[0], rng[1])
            if s:
                result["stage_compressed"] = {"from": rng[0], "to": rng[1]}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ingestion] 阶段压缩失败: {e}")

    return result


# ===========================================================================
# 概览向上聚合（问题1）：章 → 篇 → 卷 → 小说
# ===========================================================================

def _strip_text(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _concat_summary(child_summaries, cap: int = 500) -> str | None:
    """把子级摘要拼接成一段（避免调用模型，用于自动模式兜底 / 降级）。"""
    parts = [s for s in child_summaries if s and str(s).strip()]
    if not parts:
        return None
    text = "；".join(p.strip() for p in parts)
    if len(text) > cap:
        text = text[:cap] + "…"
    return text or None


def _resolve_agg_model(db: Session, model_id: str | None = None):
    """聚合用模型：优先用调用方指定的 model_id（与对话页所选一致），否则 memory 角色 / 默认模型。

    Phase 3.4：本文件原先自写了一份「指定 model_id → active 校验 → 回退」，与
    `model_crud.resolve_model` 语义相同；现直接复用后者，只保留「再退到 memory 角色」这一
    本函数独有的差异（聚合任务优先挑 memory 角色模型更省成本）。
    """
    m = model_crud.resolve_model(db, model_id)
    if m is not None:
        return m
    # resolve_model 只覆盖「默认模型」；聚合场景额外允许挑 memory 角色模型兜底。
    return _pick_model(db)


def _llm_compress_summary(heading: str, child_summaries: list[str], target_words: int,
                          model) -> str | None:
    """用模型把若干子级摘要压成一段约 target_words 字的连贯概览。失败返回 None。"""
    bullet = "\n".join(f"- {s}" for s in child_summaries if s and str(s).strip())
    if not bullet:
        return None
    prompt = (
        f"你是小说编辑，把下面「{heading}」下属各部分的摘要压成一段连贯的概览，"
        f"约 {target_words} 字。要求：只写主线进展与关键变化，不要评价、不要分点、"
        f"不要标题、不要以「好的 / 以下是」开头。\n{bullet}"
    )
    try:
        cfg = dict(_model_config(model))
        cfg["max_tokens"] = 600
        adapter = get_adapter(model.vendor, cfg)
        text = adapter.chat([{"role": "user", "content": prompt}])
        return _strip_text(text) or None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[aggregation] LLM 压缩失败({heading}): {type(e).__name__}: {str(e)[:150]}")
        return None


def aggregate_overview(db: Session, project_id: str, article_id: str | None = None,
                       auto: bool = True, model_id: str | None = None) -> dict:
    """向上聚合概览并写回各 ORM 的 summary 字段（问题1：AI 生成完章节后的自动概览）。

    - auto=True（章生成后后台自动调用）：篇级用 LLM 压缩；卷/小说级退化为拼接子级摘要，
      保证概览页永不出现「暂无 AI 概览」占位，且不额外烧 API（规避 NVIDIA 限流）。
    - auto=False（手动「刷新概览」）：篇/卷/小说三级全部用 LLM 压缩，产出精修概览；
      LLM 不可用时退化拼接，保证总能出内容。
    每一层独立 try，失败只降级/跳过，不阻断其它层。返回各层更新条数。
    """
    result = {"articles": 0, "volumes": 0, "project": 0}
    model = _resolve_agg_model(db, model_id)

    # ---- 0. 选范围 ----
    if article_id:
        arts = db.query(ArticleORM).filter_by(id=article_id, project_id=project_id).all()
    else:
        arts = db.query(ArticleORM).filter_by(project_id=project_id).all()

    # ---- 1. 篇级 ----
    for art in arts:
        mems = memory_crud.list_chapter_memories(db, project_id, article_id=art.id)
        # 跳过以「（」开头的规则兜底空摘要（如「本章摘要抽取失败…」）
        child = [m.summary for m in mems
                 if (m.summary or "").strip() and not m.summary.startswith("（")]
        if not child:
            continue
        if model is not None:
            s = _llm_compress_summary(f"篇《{art.name}》", child, 200, model)
        else:
            s = None
        if not s:
            s = _concat_summary(child, cap=500)
        if s and s != (art.summary or ""):
            art.summary = s
            result["articles"] += 1

    # ---- 2. 卷级 ----
    if article_id and arts:
        vol_ids = list({a.volume_id for a in arts if a.volume_id})
    else:
        vol_ids = [v.id for v in db.query(VolumeORM).filter_by(project_id=project_id).all()]
    volumes = db.query(VolumeORM).filter(VolumeORM.id.in_(vol_ids)).all() if vol_ids else []
    for vol in volumes:
        arts_in = db.query(ArticleORM).filter_by(volume_id=vol.id, project_id=project_id).all()
        child = [a.summary for a in arts_in if (a.summary or "").strip()]
        if not child:
            continue
        if (not auto) and model is not None:
            s = _llm_compress_summary(f"卷《{vol.name}》", child, 250, model) or _concat_summary(child, cap=600)
        else:
            s = _concat_summary(child, cap=600)
        if s and s != (vol.summary or ""):
            vol.summary = s
            result["volumes"] += 1

    # ---- 3. 小说级 ----
    all_vols = db.query(VolumeORM).filter_by(project_id=project_id).all()
    child = [v.summary for v in all_vols if (v.summary or "").strip()]
    if child:
        if (not auto) and model is not None:
            s = _llm_compress_summary("小说总览", child, 300, model) or _concat_summary(child, cap=800)
        else:
            s = _concat_summary(child, cap=800)
        proj = db.query(ProjectORM).filter_by(id=project_id).first()
        if proj and s and s != (proj.summary or ""):
            proj.summary = s
            result["project"] += 1

    try:
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[aggregation] 提交失败: {e}")
        db.rollback()
    return result


def _render_directions(chapter_no: int, dirs: list[dict]) -> str:
    lines = [f"第{chapter_no}章写完了。接下来我看到三条路可以走："]
    for i, d in enumerate(dirs, 1):
        t = d.get("title") or ""
        detail = d.get("detail") or ""
        tension = d.get("tension") or ""
        lines.append(f"\n{i}. {t}（张力{tension}）\n   {detail}")
    lines.append("\n想走哪条直接说，也可以让我换几个方向。")
    return "\n".join(lines)


# ===========================================================================
# 阶段摘要压缩
# ===========================================================================

STAGE_SYSTEM = (
    "你是小说编辑，把连续若干章的档案压成一段阶段脉络。\n"
    "只输出 JSON，不要解释：\n"
    '{"summary": "这一阶段的主线进展，200~300字", '
    '"key_events": ["改变格局的事件，3~6条"], '
    '"open_threads": ["到此仍未收束的线索"]}'
)


def compress_stage(db: Session, project_id: str, from_no: int, to_no: int) -> dict | None:
    """把 [from_no, to_no] 的章级记忆压成一条阶段摘要。"""
    rows = [
        r for r in memory_crud.list_chapter_memories(db, project_id)
        if from_no <= r.chapter_no <= to_no
    ]
    if not rows:
        return None
    rows.sort(key=lambda r: r.chapter_no)

    material_lines = []
    for r in rows:
        material_lines.append(f"第{r.chapter_no}章 {r.title or ''}：{r.summary or ''}")
        if r.plot_points:
            material_lines.append("  事件：" + "；".join(str(p) for p in r.plot_points))
    material = "\n".join(material_lines)

    data = None
    m = _pick_model(db)
    if m is not None:
        try:
            adapter = get_adapter(m.vendor, _model_config(m))
            out = adapter.chat([
                {"role": "system", "content": STAGE_SYSTEM},
                {"role": "user", "content": material[:12000]},
            ])
            data = parse_json_loose(out or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ingestion.compress_stage] 模型调用失败: {str(e)[:150]}")

    if not data or not str(data.get("summary") or "").strip():
        # 兜底：直接把各章摘要串起来截断，信息密度低但不丢链
        data = {
            "summary": "；".join((r.summary or "")[:60] for r in rows)[:600],
            "key_events": [],
            "open_threads": [],
        }

    o = memory_crud.upsert_stage_summary(
        db, project_id, from_no, to_no,
        summary=str(data.get("summary") or "").strip(),
        key_events=[str(x) for x in _as_list(data.get("key_events"))][:8],
        open_threads=[str(x) for x in _as_list(data.get("open_threads"))][:8],
        scope="range",
    )
    return memory_crud.stage_to_dict(o)
