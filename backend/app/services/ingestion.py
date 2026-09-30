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
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.context import layers
from app.core.gateway.registry import get_adapter
from app.models.orm import ChapterORM, ArticleORM, VolumeORM, ProjectORM
from app.services import (
    app_config, discussion_crud, memory_crud, model_crud,
    reference_crud, skill_dispatch, usage_crud,
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
    '  "new_entities": [{"kind": "character/faction/location/item/skill", "name": "名称", '
    '"brief": "一句话说明", "category": "（item/skill 才填）分类如 丹药/武器/拳法/剑术", '
    '"owner": "（item/skill 才填）谁持有这个物品/谁掌握这个技能，必须是 characters 里的人名，无主就空"}],\n'
    '  "relations": [{"subject": "角色A", "object": "角色B", "type": "A对B的称呼或关系词"}],\n'
    '  "char_changes": [{"name": "角色名", "change": "本章体现的转变（一句话）", '
    '"personality": "（可选）新的性格描述", "current_level": "（可选）新的境界/等级"}],\n'
    '  "next_directions": [{"title": "走向标题", "detail": "具体怎么写，两三句", '
    '"tension": "高/中/低"}]\n'
    '}\n'
    "next_directions 给 3 条，要求方向彼此不同（不要三条都是打一架），"
    "并且必须能从本章结尾自然接上。\n"
    "new_entities 只填本章新出现、且资料里没有的；没有就给空数组。\n"
    "🔴 抓人优先（S2③，治「第一篇没角色」）：凡本章**新出场、有名字、有戏份**的角色——"
    "有名有姓、有行动或对话——必须**逐个**抽进 new_entities（kind=character，brief 写清"
    "身份与本章作用），**宁可多抽不可漏抽**；只有没有名字的路人（无名侍卫/围观群众）才不抽，"
    "道具/功法/概念不是角色，不要混进来。已经出过场的角色不要重复抽。\n"
    "relations 只写本章有互动的两人，subject 和 object 必须出自 characters 数组，"
    "type 用其中一人对另一人的称呼或关系词（如师父/结拜/宿敌），每个字都简短。\n"
    "🔴 char_changes（B18×S3，2026-09-17）：只记**本章有明确文本体现**的角色变化"
    "（境界突破、心态转变、立场变化、伤势/身份变化…），一句话说清；"
    "name 必须是本书真实角色名；**没体现就不写、没有就给空数组**。"
    "这些变化只会成为**待作者确认的修订**（不会自动改角色卡），所以值得记全。\n"
    "🔴 new_entities 分级（docs/09 M8）：name 必填；kind 只能是 character/faction/location/item/skill；"
    "item/skill 须给 category（丹药/武器/材料/拳法/剑术…自由词）和 owner（谁持有/谁掌握，"
    "必须出自 characters，无主就空）；brief 一句话（会标 ai_generated，作者可改）。"
    "物品/技能与已有条目同名就不要再报（不覆盖已有描述）。"
)

_JSON_KEYS = (
    "summary", "ending_hook", "characters", "locations",
    "plot_points", "foreshadow_actions", "new_entities", "relations", "next_directions",
    "char_changes",
)


# ===========================================================================
# JSON 抽取容错
# ===========================================================================

logger = logging.getLogger(__name__)


def _close_brackets(body: str) -> str | None:
    """按「括号栈」补全被截断的 JSON 结尾（字符串内部的括号不计）。

    - 已平衡 → 原样返回；
    - 只是少了结尾（`[`/`{` 未闭合）→ 追加对应闭合符；
    - **截断在字符串中间**或括号错位（`]`/`}` 交错、多余）→ 返回 None，
      交给调用方的其它候选（如"回退到上一个逗号"）处理，避免补出一个假 JSON。
    """
    stack: list[str] = []
    in_str = esc = False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and ((ch == "}") == (stack[-1] == "{")):
                stack.pop()
            else:
                return None            # 括号错位 → 不在本函数职责内
    if in_str:
        return None                    # 截断在字符串里 → 补引号会造出半句假摘要
    if not stack:
        return body
    return body + "".join("}" if c == "{" else "]" for c in reversed(stack))


def _repairs(body: str) -> list[str]:
    """产出「截断修复」候选串，按**最可能先命中**的顺序排列（调用方逐个 json.loads 试探）。

    实测高频形态（2026-09-16 试验，见 docs/08 B19）：
      `{"chapters":[{…},{…}` + `}`  ← 最后一个 `}` 其实是**多余的**，缺的是数组的 `]`
    这种"多一个尾 `}`"光靠补闭合是修不好的（补出来变成 `}}]}`），必须先去掉尾 `}` 再补。
    """
    out: list[str] = []
    b = body.strip()
    out.append(b)                      # ① 原样（合法 JSON 走这条，零成本）
    out.append(b + "]}")               # ② 只缺数组+对象闭合
    out.append(b + "]")                # ③ 只缺数组闭合（顶层是 list 的情形）
    out.append(b + "}")
    if b.endswith("}"):                # ④ 尾部多了 `}`（真实现象）→ 换成补 `]` 再收尾
        out.append(b[:-1] + "]}")
    s = b                              # ⑤ 截断在半途 → 回退到上一个逗号再补括号
    for _ in range(4):
        cut = s.rfind(",")
        if cut <= 0:
            break
        s = s[:cut]
        c = _close_brackets(s)
        if c:
            out.append(c)
    c = _close_brackets(b)
    if c:
        out.append(c)
    return out


def parse_json_loose(text: str) -> dict | None:
    """从模型输出里把 JSON 抠出来。

    实测本地小模型常见的五种脏输出：包在 ```json 里、前面带一句「好的」、
    结尾多个逗号、中文全角引号，以及**输出被截断（括号未闭合）**。逐个处理，能救一个是一个。

    🔴 **修复顺序有讲究（2026-09-16 重写，见 docs/08 B19）**：
    1. 先试「原样」，再试「补/去括号」类修复；
    2. **尾逗号**随后；
    3. **全角引号 → 半角 放在最后**——它是破坏性的（会把正文里成对的全角引号也换掉），
       旧实现在第 2 次尝试就做这件事，结果把"本来就只差个 `]`"的输出改得更坏。
    实测收益：九星霸体诀 40 章摘要原本 4 批丢 3 批（20 条合法摘要被整批丢弃），加上截断修复后可直接救回。
    """
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)

    i = s.find("{")
    if i == -1:
        return None
    # 有末尾 `}` 时取到它（砍掉后面的说明文字）；同时保留"一直取到串尾"的候选，
    # 供"整个输出被截断、最后一个 `}` 其实是多余"的情形使用。
    j = s.rfind("}")
    bodies = ([s[i:j + 1]] if j > i else []) + [s[i:]]

    tried: set[str] = set()
    for body in bodies:
        for text_fix in (lambda x: x,
                         lambda x: re.sub(r",\s*([}\]])", r"\1", x),        # 尾逗号
                         lambda x: x.replace("“", '"').replace("”", '"')):  # 全角引号（最后手段）
            for cand in _repairs(text_fix(body)):
                if cand in tried:
                    continue
                tried.add(cand)
                try:
                    data = json.loads(cand)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(data, dict):
                    return data
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
    _KINDS = {"character", "faction", "location", "item", "skill"}
    for item in _as_list(data.get("new_entities")):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "character").strip().lower()
            if not name or kind not in _KINDS:
                continue                       # M8 分级：kind 白名单外直接丢（防 AI 瞎造类别）
            ne.append({
                "kind": kind,
                "name": name,
                "brief": str(item.get("brief") or "").strip(),
                "category": str(item.get("category") or "").strip() or None,
                # A7（2026-10-01）：item/skill 的持有/掌握者，抽取链连「持有物品/掌握技能」边用
                "owner": str(item.get("owner") or "").strip() or None,
            })
    out["new_entities"] = ne[:12]

    # B18×S3（2026-09-17）：章节里体现的角色**主观项变化** → 交给修订表走 pending 审批。
    #  只保留 SNAPSHOT_FIELDS 里的键（personality/current_level 等），change 作 note。
    cc = []
    for it in _as_list(data.get("char_changes")):
        if not isinstance(it, dict):
            continue
        nm = str(it.get("name") or "").strip()
        if not nm:
            continue
        fields = {}
        for k in ("personality", "background", "current_level", "talent", "brief", "role_type"):
            v = str(it.get(k) or "").strip()
            if v:
                fields[k] = v
        cc.append({"name": nm, "change": str(it.get("change") or "").strip(),
                   "fields": fields})
    out["char_changes"] = cc[:6]

    rel = []
    for item in _as_list(data.get("relations")):
        # 只认结构化条目：字符串形态（"A对B：师父"）缺字段结构，丢弃不猜
        if isinstance(item, dict):
            subj = str(item.get("subject") or "").strip()
            obj = str(item.get("object") or "").strip()
            rtype = str(item.get("type") or item.get("relation_type") or "").strip()
            if subj and obj and rtype:
                # relation_type 列宽 String(20)，超长直接截断（防 DB 报错）
                rel.append({"subject": subj, "object": obj, "type": rtype[:20]})
    out["relations"] = rel[:12]

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
        "relations": [],
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
        # max_tokens 下限 2600：抽取 schema 加 relations 后满字段输出约 2100+ token，
        # 预算不足会被硬截断 → JSON 解析失败 → 整章白抽走规则兜底（得不偿失）。
        # 上限 4096：防用户配置过大时抽取这种短任务白占预算（正文生成有独立覆盖，见 docs/04 B8）。
        "max_tokens": min(max(m.max_tokens or 2000, 2600), 4096),
        "enable_thinking": False,    # 思考过程会污染 JSON 输出
    }


def _meter(db: Session, *, scene: str, project_id: str | None, model, adapter,
           t0: float, ok: bool, messages: list | None, out_text: str | None) -> None:
    """记录一次模型调用用量（08-B5：摄取链路此前不记账 → 观测页统计偏低）。

    `record_usage` 自身失败静默（计量是旁路，见 usage_crud 模块注释），这里不再包 try。
    语义：只记「模型调用」本身 —— 解析失败不算调用失败（走规则兜底），调用失败（ok=False）也记
    （prompt 已发出同样烧 token）。
    """
    try:
        usage_crud.record_usage(
            db,
            scene=scene,
            vendor=getattr(model, "vendor", None),
            model_name=getattr(model, "model_name", None),
            usage=getattr(adapter, "last_usage", None) if adapter is not None else None,
            project_id=project_id,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=ok,
            prompt_text="".join(str(m.get("content") or "") for m in (messages or [])),
            completion_text=out_text,
        )
    except Exception as e:  # noqa: BLE001
        # 纵深防御：record_usage 自身已全量 try，这里再兜一层 —— 计量绝不影响摄取主链路
        logger.warning(f"[ingestion] 用量记录失败 scene={scene}: {type(e).__name__}: {e}")


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
        clip = (content if len(content) <= 20000 else
                content[:14000] + "\n…（中略）…\n" + content[-6000:])   # 2026-09-17 放开
        messages = [
            {"role": "system", "content": "\n\n".join(sys_parts)},
            {"role": "user", "content": f"第{chapter.chapter_no}章 {chapter.title or ''}\n\n{clip}"},
        ]
        _t0 = time.monotonic()
        adapter = None
        raw_out = None
        try:
            adapter = get_adapter(m.vendor, _model_config(m))
            raw_out = adapter.chat(messages)
            _meter(db, scene="ingest_extract", project_id=project_id, model=m,
                   adapter=adapter, t0=_t0, ok=True, messages=messages,
                   out_text=raw_out or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ingestion] 记忆抽取调用失败: {type(e).__name__}: {str(e)[:150]}")
            _meter(db, scene="ingest_extract", project_id=project_id, model=m,
                   adapter=adapter, t0=_t0, ok=False, messages=messages, out_text=None)
        else:
            # 解析不在计量语义内：解析失败 ≠ 调用失败（走规则兜底，调用本身已计 ok=True）
            parsed = parse_json_loose(raw_out or "")
            if parsed:
                extracted = normalize_extract(parsed)
                if not extracted.get("summary"):
                    extracted = None  # 摘要都空，等于没抽出来

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

    # ---------- 2.7 关系/势力回注（任务③批次2）----------
    # 补齐另一条「最后一米」：relations/new_entities 此前只存进章级记忆 JSON、
    # 从不写实体表。这里把本章抽到的关系自动落 relations 表（subject/object 必须
    # 同时满足「在本章 characters 里」+「能解析到库内角色」双条件，防幻觉防脏边）；
    # new_entities 里的 faction 自动落 factions 表（character/location 仍走人工确认）。
    # 幂等（重跑同一章不重复建），失败静默不阻断后续步骤。
    try:
        from app.services import faction_crud, relation_crud
        from app.services import item_crud, skill_crud
        result["entity_sync"] = {
            "relations": relation_crud.sync_from_extract(
                db, project_id, extracted.get("relations"),
                chapter_characters=extracted.get("characters"),
                chapter_no=chapter.chapter_no,
            ),
            "factions": faction_crud.sync_from_extract(
                db, project_id, extracted.get("new_entities")),
            # A4（docs/09 M8）：新物品/新技能自动落库（标 ai_generated；重名不覆盖）
            # A7（2026-10-01 拍板）：AI 填 owner 时顺带连「持有物品/掌握技能」边
            "items": item_crud.sync_from_extract(
                db, project_id, extracted.get("new_entities"),
                chapter_characters=extracted.get("characters"),
                chapter_no=chapter.chapter_no),
            "skills": skill_crud.sync_from_extract(
                db, project_id, extracted.get("new_entities"),
                chapter_characters=extracted.get("characters"),
                chapter_no=chapter.chapter_no),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ingestion] 关系/势力回注失败: {type(e).__name__}: {e}")

    # ---------- 2.8 角色主观项变化 → 待审修订（B18×S3，2026-09-17） ----------
    # 抽到的 `char_changes` 只写 **pending** 修订（character_revisions），**绝不直接改角色卡**；
    # 作者在角色详情页确认后才生效 —— 这正是 S3 的两档分工（客观项自动更新、主观项只提示）。
    # 只处理「能解析到库内角色」的变化（库外角色由"新实体确认→建卡"链负责）；失败静默不阻断。
    try:
        from app.models.orm import CharacterORM as _Char
        from app.services import character_revision_crud as _rev
        _proposed, _skipped = [], []
        for _ch in (extracted.get("char_changes") or []):
            _nm = str(_ch.get("name") or "").strip()
            _fields = _ch.get("fields") or {}
            if not _nm or not _fields:
                _skipped.append(_nm or "?")
                continue
            _row = db.query(_Char).filter_by(project_id=project_id, name=_nm).first()
            if _row is None:
                _skipped.append(_nm)      # 未建卡 → 走确认建卡链，不进修订表
                continue
            _r = _rev.propose(db, project_id, _row.id, _fields,
                              chapter_no=chapter.chapter_no,
                              note=f"第 {chapter.chapter_no} 章："
                                   f"{_ch.get('change') or '角色有变化迹象'}")
            if _r and _r.get("id"):
                _proposed.append(_nm)
        result["char_changes"] = {"proposed": _proposed, "skipped": _skipped}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ingestion] 角色变化待审修订写入失败（不影响摄取）: "
                       f"{type(e).__name__}: {e}")

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


def _llm_compress_summary(db: Session, heading: str, child_summaries: list[str],
                          target_words: int, model,
                          project_id: str | None = None) -> str | None:
    """用模型把若干子级摘要压成一段约 target_words 字的连贯概览。失败返回 None。

    `db` 仅用于用量记账（08-B5）；调用方（aggregate_overview）均在非流式上下文，
    不触犯「流式生成器内禁用请求级 session」铁律（04-C1）。
    """
    bullet = "\n".join(f"- {s}" for s in child_summaries if s and str(s).strip())
    if not bullet:
        return None
    prompt = (
        f"你是小说编辑，把下面「{heading}」下属各部分的摘要压成一段连贯的概览，"
        f"约 {target_words} 字。要求：只写主线进展与关键变化，不要评价、不要分点、"
        f"不要标题、不要以「好的 / 以下是」开头。\n{bullet}"
    )
    messages = [{"role": "user", "content": prompt}]
    _t0 = time.monotonic()
    adapter = None
    try:
        cfg = dict(_model_config(model))
        cfg["max_tokens"] = 600
        adapter = get_adapter(model.vendor, cfg)
        text = adapter.chat(messages)
        _meter(db, scene="ingest_aggregate", project_id=project_id, model=model,
               adapter=adapter, t0=_t0, ok=True, messages=messages, out_text=text or "")
        return _strip_text(text) or None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[aggregation] LLM 压缩失败({heading}): {type(e).__name__}: {str(e)[:150]}")
        _meter(db, scene="ingest_aggregate", project_id=project_id, model=model,
               adapter=adapter, t0=_t0, ok=False, messages=messages, out_text=None)
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
            s = _llm_compress_summary(db, f"篇《{art.name}》", child, 200, model,
                                      project_id=project_id)
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
            s = _llm_compress_summary(db, f"卷《{vol.name}》", child, 250, model,
                                      project_id=project_id) or _concat_summary(child, cap=600)
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
            s = _llm_compress_summary(db, "小说总览", child, 300, model,
                                      project_id=project_id) or _concat_summary(child, cap=800)
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
        _t0 = time.monotonic()
        stage_messages = [
            {"role": "system", "content": STAGE_SYSTEM},
            {"role": "user", "content": material[:12000]},
        ]
        adapter = None
        try:
            adapter = get_adapter(m.vendor, _model_config(m))
            out = adapter.chat(stage_messages)
            _meter(db, scene="ingest_stage", project_id=project_id, model=m,
                   adapter=adapter, t0=_t0, ok=True, messages=stage_messages,
                   out_text=out or "")
            data = parse_json_loose(out or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ingestion.compress_stage] 模型调用失败: {str(e)[:150]}")
            _meter(db, scene="ingest_stage", project_id=project_id, model=m,
                   adapter=adapter, t0=_t0, ok=False, messages=stage_messages, out_text=None)

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
