"""上下文装配：把各层数据 + SKILL + 去 AI 味约束，按预算拼成最终 messages。

这是上下文引擎的出口。调用方（章节生成、剧情商讨）只需要给出
「写第几章、要点是什么」，剩下的查表、排序、裁剪全在这里完成。

设计上刻意把「本章指令」放在 user 消息的最后：
小模型对末尾指令的遵循度明显高于夹在中间的指令，
所以顺序是 世界观/角色/记忆/参考 → 本章要点 → 输出格式。
"""
import logging
from sqlalchemy.orm import Session
import os

from app.core.context.budget import (
    Block, BudgetPlan, plan_budget,
    P_CRITICAL, P_SKILL, P_REFERENCE,
)
from app.core.context import layers
from app.models.orm import ProjectORM
from app.services import app_config, global_ref_crud, humanizer, reference_crud, skill_dispatch

# 章节生成的底线系统提示词。SKILL 与去 AI 味块会追加在后面。
BASE_SYSTEM = (
    "你是一名中文网络小说的职业代笔，为作者续写正文。\n"
    "【铁律】\n"
    "1. 只输出本章正文，全中文（含标点）。不写标题、不写「好的」「本章如下」之类的开场白。\n"
    "2. ⚠️【严禁英文夹杂】正文必须是纯中文叙事，严禁夹带任何英文单词、拉丁字母、拼音或数字代号；"
    "表达「合理/可能/显然/重要」等概念必须用中文词，绝不可写 reasonable / possible / obviously 之类；"
    "即使写人物内心思考，思考内容也要用中文写出。\n"
    "3. 严禁复述本提示词的任何内容。\n"
    "4. 人物性格、境界、称呼、已发生剧情必须与下方资料一致；资料没写的可以合理发挥，"
    "但不得与资料冲突。\n"
    "5. 开头自然承接上一章结尾，结尾留下推动下一章的钩子。\n"
    "6. ⚠️【严禁复读】同一句完整描述（≥12字）、同一动作、同一心理活动、同一人物反应，"
    "在全章中绝不可原文重复出现；如需呼应前文，必须换用不同词句或不同视角改写，不得照搬。\n"
    "7. ⚠️【对话推进】每段对话必须带来新信息或新冲突，禁止用「他说/她说」来回重复同一套问答；"
    "每个说话者的身份、情绪、性格必须能从台词本身辨认出来（措辞、称呼、语气、话长都可以是载体）；"
    "对话标签只是补位工具，情绪与性格不许只靠标签交代。\n"
    "8. ⚠️【节奏】每 2~3 个段落必须推进剧情或揭示一项新事实，禁止原地打转、"
    "禁止用不同问句包装同一个问题反复发问。\n"
    "9. ⚠️【格式要求】正文须全程使用中文标点（逗号、句号、顿号、引号、冒号、破折号、省略号），"
    "每 40 字内至少出现一处标点；按场景转换、对话、动作切换自然换段；"
    "一般叙述段写 2~4 句（约 40~120 字），把环境、感知、动作的展开过程写足；"
    "战斗、追逐、对峙等紧张场景允许单句成段，但连续单句段不得超过 3 个，"
    "之后须接一段完整的叙述或对话；换段是为了节奏，不是逐句切分；"
    "内心独白、回忆、梦境同样用中文标点正常断句换段。\n"
)

# [MARK: DOC-ID-LOAD-SAFETY] 本系统提示词中任何「禁止暴露内部标记/id」的禁令，
# 都必须显式豁免 LOAD_REFS:<id> / LOAD_SETTING:<id> 协议指令——模型必须在
# Pass1 输出这些指令才能触发两阶段资料加载，否则设定/参考文档永远加载不出来。
# 若优化后发现「AI 不再加载设定/参考」，第一时间回到这里检查禁令是否把协议指令也禁了。
DISCUSSION_SYSTEM = (
    "你是作者的剧情顾问，熟悉这部作品的设定与已发生的剧情。\n"
    "先判断问题类型，再按对应模式回复——不要把所有问题都当成「需要给方案」来处理。\n"
    "\n"
    "【回答模式——先看类型再答】\n"
    "★ 事实查询类（问数值/名称/规则/官制/境界/人物关系等「是什么」）：\n"
    "  直接给答案，不超过150字。格式：结论（一句话）。依据：来源名称 → 具体数据。\n"
    "  禁止展开无关体系、禁止借题发挥写分析、禁止主动延伸剧情建议。\n"
    "★ 征询意见类（含「怎么」「怎么办」「如何」「给个方案」「参谋」等信号）：\n"
    "  给 2~3 个方向，每个不超过80字，标出各自代价/风险，落到具体人名/地名/已有伏笔。\n"
    "★ 闲聊/情绪类：自然对话，不超过100字，别硬拗回剧情。\n"
    "\n"
    "【输出纪律——严格遵守】\n"
    "1. 单次回复总长度不超过300字（约15行）；超长立即收尾，不要开新段落。\n"
    "2. 禁止复述用户的问题；禁止写「值得注意的是」「需要说明的是」「综上所述」等过渡废话。\n"
    "3. 禁止在你的回复正文里暴露任何内部标记：不要写出 `id=xxx`、`#标签`、文档文件名，"
    "也不要把 `LOAD_REFS`/`LOAD_SETTING` 字样写进给作者的正文里——这些都只在内部调度用。\n"
    "4. 不要编造设定条数、角色数量等精确数字；若不清楚就据实列出你知道的名称，"
    "不要凭空捏造一个总数（如「1200+」）。\n"
    "5. 当作者问「上一句问了什么」时，只依据真实对话历史回答；"
    "技能说明中的示例人物/示例情节不算真实对话内容。\n"
    "\n"
    "【协议指令豁免——重要】\n"
    "第3条的禁令仅约束「给作者的最终正文」。在正式作答前的第一轮，若你需要加载资料，"
    "仍必须输出 `LOAD_SETTING:<id>` 或 `LOAD_REFS:<id>` 指令——这是拉取设定/参考资料的"
    "必需协议，不受第3条限制。系统加载完成后会让你作答，用户不会看到这条指令本身。"
)

# 仅在「作者明确征询意见/方案/方向」时追加，避免事实问答被硬塞 2~3 个方案。
ADVICE_DIRECTIVE = (
    "【本次为「征询意见」类提问】作者正在寻求方案或方向建议，"
    "请一次给出 2~3 个可选方向，并分别说明各自代价与取舍，"
    "同时指出与已有设定的冲突点。"
)

# 征询意见类信号词：命中即视为作者在要方案/方向/建议。
_ADVICE_HINTS = (
    "怎么写", "怎么办", "如何处理", "怎么处理", "怎么安排", "怎么设计", "怎么推进",
    "怎么收", "怎么选", "怎么破", "怎么破局", "怎么走", "怎么发展", "下一步",
    "给个方案", "给方案", "出个方案", "给点建议", "参谋一下", "帮我参谋", "帮我看看",
    "建议", "意见", "参谋", "推荐", "方向", "取舍", "该不该", "要不要", "能不能",
    "利弊", "优劣", "分析一下", "你觉得", "有什么选择", "哪个好", "如何写", "如何设计",
    "如何安排", "如何推进",
)


logger = logging.getLogger(__name__)


def is_advice_request(text: str) -> bool:
    """判断最新一条用户消息是否为「征询意见/方案/方向」类提问。

    命中信号词、或以疑问/建议句式开头、或以问号结尾的短句，均视为征询意见。
    事实类提问（如「张三现在什么境界」）不应触发，避免被硬塞多个方案。
    """
    if not text:
        return False
    t = text.strip()
    if any(h in t for h in _ADVICE_HINTS):
        return True
    if t.startswith(("要不要", "该不该", "怎么", "如何", "是否", "能不能", "可不可以")):
        return True
    if (t.endswith("？") or t.endswith("?")) and len(t) <= 60:
        return True
    return False


def _mk(key: str, title: str, content: str, priority: int, order: int,
        min_chars: int = 0, required: bool = False) -> Block | None:
    if not (content or "").strip():
        return None
    return Block(key=key, title=title, content=content, priority=priority,
                 order=order, min_chars=min_chars, required=required)


def _resolve_level(db: Session, override: str | None) -> str:
    if override:
        return override
    return app_config.get(db, app_config.KEY_CONTEXT_BUDGET, "standard")


def _resolve_layer_mode(override: str | None) -> str:
    """章节上下文的「按需层级」开关。

    full（默认）= 当前行为，零回归；
    relevant = 设定描述/伏笔/关系按相关性或命中裁剪。
    优先级：函数参数 > 环境变量 NA_CHAPTER_LAYER_MODE > 默认 full。
    """
    if override:
        return override
    return os.environ.get("NA_CHAPTER_LAYER_MODE", "full")


def build_chapter_messages(
    db: Session,
    project_id: str,
    *,
    chapter_no: int,
    article_id: str | None = None,
    volume_id: str | None = None,
    prompt_hint: str | None = None,
    word_range: dict | None = None,
    trigger_foreshadow_ids: list[str] | None = None,
    from_discussion: bool = True,
    budget_level: str | None = None,
    layer_mode: str | None = None,
    chapter_id: str | None = None,
    conversation_id: str | None = None,
) -> tuple[list[dict], dict]:
    """组装章节生成的 messages。

    返回 (messages, meta)。meta 里带着预算调试信息和参考文档命中明细，
    路由层可以把它当 SSE 事件推给前端——作者能看见「AI 这次到底读了什么」，
    这比让他猜为什么人设崩了要有用得多。
    """
    trigger_foreshadow_ids = trigger_foreshadow_ids or []
    word_range = word_range or {"min": 3000, "max": 5000}
    level = _resolve_level(db, budget_level)
    lmode = _resolve_layer_mode(layer_mode)

    # ---------- 1. 先识别本章可能涉及的实体 ----------
    # 用「本章要点 + 最近商讨」当线索，命中的角色/势力/地点会拿到全量注入
    hint_text = prompt_hint or ""
    disc_block = (
        layers.layer_discussion(db, project_id, chapter_id=chapter_id, conversation_id=conversation_id)
        if from_discussion else None
    )
    mention_src = [hint_text]
    if disc_block:
        mention_src.append(disc_block.content)
    mentions = layers.extract_mentions(db, project_id, *mention_src)

    # ---------- 1.5 实体图一跳扩展（A5 GraphRAG）----------
    # 要点提到配角 B → 把 B 的师父/同门/所属宗门/关联地点提升为「本章重点实体」，
    # 交给下面各层按全量渲染（否则它们只是一行简写，AI 写「回宗门搬救兵」时只能现编）。
    graph_trace: dict = {}
    try:
        from app.services import entity_graph
        if entity_graph.enabled(db):
            mentions, graph_trace = entity_graph.expand(db, project_id, mentions)
        else:
            graph_trace = {"enabled": False, "reason": "配置关闭"}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[builder] 实体图扩展跳过: {type(e).__name__}: {str(e)[:80]}")
        graph_trace = {"enabled": True, "error": f"{type(e).__name__}: {str(e)[:80]}"}
    # ---------- 1.6 GraphRAG 注入块（docs/09 §3，阶段 A3/A5）----------
    # 新关系层（entity_relations：角色-技能-物品-势力-地点 任意实体对）一跳装配，
    # 产出**独立注入块**（技能/物品/势力/地点 + 关系网）；死亡角色只给「名字+关系+已死亡」。
    # 与上面的 mentions 扩展互补：那边扩的是"要全量渲染的实体"，这边补"关系与关联物"。
    #
    # 🔴 blocks / _add 必须**先于**本块定义（2026-09-20 修复）：
    # 原先它们定义在下方「2. 逐层取数」处，而本块提前调用 _add → Python 把 _add 当作
    # 局部变量、访问时尚未绑定 → UnboundLocalError → 被本块 except Exception 静默吞掉，
    # 只在 error.log 留一行「[builder] GraphRAG 块跳过」，肉眼极易忽略。
    # 后果：GraphRAG 块（P_CRITICAL，order=45）长期静默丢失，每次章节生成都少一块上下文。
    blocks: list[Block] = []

    def _add(b: Block | None):
        if b is not None:
            blocks.append(b)

    try:
        from app.services import graph_rag
        if graph_rag.enabled(db):
            seed_names = sorted(mentions.get("characters") or set())[:10]
            _inj = graph_rag.build_injection(db, project_id, names=seed_names)
            if _inj:
                _add(_mk(key="graphrag", title="关联实体与关系网（一跳）",
                         content=_inj, priority=P_CRITICAL, order=45))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[builder] GraphRAG 块跳过: {type(e).__name__}: {str(e)[:80]}")


    # ---------- 2. 逐层取数 ----------
    recent_n = int(app_config.get(db, app_config.KEY_RECENT_MEMORY_N, 3) or 3)

    _add(disc_block)
    query_text = hint_text + " " + (disc_block.content if disc_block else "")
    _add(layers.layer_world(db, project_id, volume_id=volume_id, article_id=article_id,
                            mode=lmode, query_text=query_text))
    _add(layers.layer_characters(db, project_id, focus_names=mentions.get("characters")))
    _add(layers.layer_entities(db, project_id, focus=mentions, mode=lmode, query_text=query_text))
    _add(layers.layer_foreshadows(db, project_id, trigger_ids=trigger_foreshadow_ids,
                                  mode=lmode, query_text=query_text))
    _add(layers.layer_stage_summaries(db, project_id, chapter_no))
    _add(layers.layer_recent_memories(db, project_id, chapter_no, limit=recent_n))
    # Phase 7.2：作者已确认的篇规划（本章任务卡，P_CRITICAL 级）—— 放在指令组之前
    _add(layers.layer_chapter_plan(db, project_id, article_id=article_id, chapter_no=chapter_no))
    _add(layers.layer_prev_chapter(db, project_id, chapter_no))

    # ---------- 3. 参考文档：按相关性挑，不再一股脑全塞 ----------
    entity_names: set[str] = set()
    for v in mentions.values():
        entity_names |= v
    ref_text, ref_detail = reference_crud.pick_relevant(
        db, project_id, article_id,
        query_text=hint_text + " " + (disc_block.content if disc_block else ""),
        entity_names=entity_names,
    )
    _add(_mk("references", "【参考资料】", ref_text, P_REFERENCE, order=70, min_chars=500))

    # ---------- 4. 本章指令（永不裁剪） ----------
    task_lines = [f"现在创作第 {chapter_no} 章。"]
    if hint_text.strip():
        task_lines.append(f"本章要点（作者指定，必须完成）：{hint_text.strip()}")
    else:
        task_lines.append("作者未指定要点，请依据上方商讨记录与前情，推进最合理的下一步剧情。")
    task_lines.append(
        f"本章正文不少于 {word_range.get('min', 3000)} 字（硬性要求，写不满即不合格）；"
        "写透场景=写足细节与展开过程，不等于提前收束。"
    )
    task_lines.append(
        "写作纪律（违反即作废）：① 不得原文重复任何≥12字的句子/动作/心理描写；"
        "② 对话须每段推进新信息，禁止用同一套问答反复拉扯；"
        "③ 每 2~3 段必须推进剧情或揭示新事实，不得原地打转；"
        "④ 必须正确使用中文标点并自然分段；一般叙述段 2~4 句（约 40~120 字），"
        "紧张场景允许单句成段但不得连续超过 3 个，换段≠逐句切分；"
        "⑤ 正文须为纯中文，严禁夹带英文单词/拉丁字母（如 reasonable、possible 之类），否则作废。"
    )
    # 2026-09-20 加（作者实测指出）：新角色出场时，模型总用「看一眼他身上的关键道具」来建立关联
    #   ——实测一章内 "布包" 被 8 次提及，其中两处是不同新角色「目光停在他腰间的布包上」。
    # 🔴 2026-09-21 作者进一步纠正（推翻上一版）：上一版让模型「换着打量」（老茧/鞋/口音）依然是错的——
    #   **正常人路过正常人，符合常理就瞟一眼过去了，根本不会仔细打量**。
    #   人的下意识是排除危险：有戒心看的是武器与埋伏，不是研究对方的手。
    #   只有「违反常理」的东西才值得注意；且注意力必须服务剧情（后文用不到就连"注意到"都不写）。
    task_lines.append(
        "【人物相互观察的写法纪律（硬性）】\n"
        "人只注意「违反预期」的东西。写一个新人物看主角（或看任何陌生人）时：\n"
        "· **默认就是瞟一眼**——正常人路过正常人，符合常理就不细看；直接写他自己的目的"
        "（赶路、问话、做生意），不要停下来打量对方。\n"
        "· 🔴 **禁止仔细打量陌生人**：「注意到他手上的老茧」「指甲缝里的泥」「鞋的磨损」"
        "「衣衫的补丁」这类句子一律不许出现——谁好端端的看一个赶路人的手？\n"
        "· **有戒心时**，人看的是**有没有武器、环境有没有埋伏**（这关系到他的命），"
        "不是研究对方的细节。\n"
        "· 只有**违反常理**的东西才值得细看：背着棺材赶路、当街亮刀、浑身是血。\n"
        "· 🔴 **注意力必须服务剧情**：这个细节后文用不到，就连「注意到」都不要写。\n"
        "  （门口躺着个乞丐、面前摆个碗——若后文没有乞丐的戏，就只写「门口躺着个乞丐」，"
        "不要写「注意到他面前的碗」。乞丐摆碗是常理，常理不值得写。）\n"
    )
    # 2026-09-13 加：分段形态 few-shot 示范——Qwen3.8-Flash 对「网文=一句一段」先验极强，
    # 抽象指令（2~4 句/40~120 字）实测无效（段均 12.0→12.6），模型对「照示例模仿」的遵循
    # 远高于「遵守规则」，故给整段排版形态的正反示范，压在 user 消息末尾（注意力最高位）。
    # 🔴 2026-09-20 只换语料、教学点一字未改：原示范用的是古装/仙侠语境（照壁·水缸·里屋），
    #   而实测**示范的语体会被一起迁移**——隔离实验（outputs/古风示范隔离实验.html）证实形态被
    #   完整照搬（连引号形态 100% 迁移），作者反馈"章节读起来像十几年前金庸那代"。
    # 🔴 2026-09-20 只换语料、教学点一字未改（初版换成现代校园语料）——
    #   2026-09-21 作者实测发现**语境错配**：故事是古代修仙背景，现代校园示范被模型判定"不适用"
    #   → 打折执行 → 回落书面语默认腔（金庸味依旧）。B19 铁律补全：
    #   示范语料的语境不仅要匹配"目标文风"，还要匹配"**故事背景**"。
    #   现改为「当代网文口语 × 仙侠场景」（作者指定的《没钱修什么仙？》配方：修仙背景+网文语感）。
    task_lines.append(
        "【分段形态示范——硬性要求，分段照此逐字模仿（只学分段形态，不学词句内容）】\n"
        "正确形态（一般叙述段 2~5 句、约 60~150 字一段）：\n"
        "他绕过后山灵田，看守的灯火已经灭了大半。木架上的告示被雨泡起了边，名字糊成一片，"
        "只有头一名的朱印还看得清。他伸手按了按那张纸，指尖沾上湿掉的墨，半天没舍得擦。"
        "山道上传来脚步声，由远及近，到拐角又没了动静。他把手背到身后，装作在看别的。\n"
        "短段也合法（紧张场景专用）：值事堂的门开了一条缝。门缝后的人盯着他看了半晌，"
        "才侧身让开，门轴吱呀一声。\n"
        "错误形态（逐句分段），一个字都不许模仿：\n"
        "他绕过后山灵田。\n"
        "看守的灯火已经灭了大半。\n"
        "木架上的告示被雨泡起了边。\n"
        "本章正文里，非战斗/追逐/对峙处的段落一律按正确形态；单句成段只许出现在紧张场景，连续不超过 3 个。"
    )
    # 2026-09-13 加：对话形态 few-shot 示范——AI 台词条均仅 7.3 字（真实 18.1）、语气词密度
    # 只有真实 1/3、审讯式乒乓 7/20 串、全员同腔（根因：对话指令全是删减型，0 条正面教）。
    # 与分段示范同款打法：自写当代示例防污染 + 正反形态对照 + 只学形态不学词句。
    task_lines.append(
        "【对话形态示范——硬性要求，照此模仿说话形态（只学形态，不学词句内容）】\n"
        "正确形态一（交锋戏：台词有长短、有潜台词、有称呼变化、情绪在措辞里）：\n"
        "“内门的名额，你从哪儿弄来的？”周野把那块令牌推回他手边，声音压得很低。\n"
        "“捡的。”\n"
        "“捡的？”李承冷笑一声，“我盯了你三天，你连藏经阁的门都没进过，上哪儿捡考核名单？”\n"
        "“李哥说笑了。”周野把令牌塞进怀里，“也许是长老看我笨，特意搁在我看得见的地方。”\n"
        "“你——”李承指着他，半天没把话说下去，走到门口又停住，“三日后复测，你自己掂量。”\n"
        "正确形态二（日常戏：台词有拉扯、有性格、有生活逻辑）：\n"
        "“五块灵石，不能再少了。”摊主把那张符纸翻来覆去看了三遍，“画成这样，贴门上鬼都嫌。”\n"
        "“道友，早上刚画的，朱砂还润着呢。”\n"
        "“润着？”摊主斜他一眼，“我在这条街摆了六年摊，还看不出这线是你描过两遍的？”"
        "他把符放回去，手却没缩回来，“两块灵石，我拿回去给我孙子练手。”\n"
        "“……四块，再送我两张净尘符。”\n"
        "“三块，符归我，下次给你留好的。”\n"
        "“成。”\n"
        "错误形态（审讯式乒乓），一个字都不许模仿：\n"
        "“符哪来的？”\n"
        "“买的。”\n"
        "“谁卖的？”\n"
        "“摊上。”\n"
        "“哪个摊？”\n"
        "“街口。”\n"
        "本章正文里，除刑讯/紧急盘问外，对话一律按正确形态：一条台词一般 8~25 字，"
        "倾诉、解释、情绪爆发可到 40~60 字；连续两条不超过 6 字的对话之后，必须接一条完整台词或叙述破局；"
        "台词里可以有语气词（哼/啧/呢/吧/罢了）和半截话，让每个人说话的方式各不相同。"
        "对话轮次服务剧情，不为凑字数反复拉扯；砍掉重复轮次省下的字数，"
        "用叙述和更完整的台词补回来，全章字数硬要求不变。"
        "示例里出现的任何词句（含“捡的”“说笑了”“成”等短语）都不得原样写进正文，只许学说话的形态。"
    )
    # 2026-09-21 加（POC-1c 验证有效，作者拍板）：同一件事，不同性格的人说法完全不同。
    # 治「最中性的那句台词」（谁都能说=没写人）。注意：正例 4 条 vs 反面只点名 1 次（防 priming 反噬）。
    task_lines.append(
        "【对话形态补充——同一件事，不同的人说法完全不同（硬性）】\n"
        "要表达「我没偷」这件事，四种性子的人会这样说：\n"
        "· 急脾气、不服软的：“你可别血口喷人！铺里半钱药材我都没碰过！”\n"
        "· 冷性子、懒得辩的：“丢了什么你查账去，跟我有什么关系。”\n"
        "· 会来事、嘴皮子滑的：“哎哟掌柜的，这话可冤死我了——我哪来的胆子。”\n"
        "· 闷葫芦、憋着火不说的：半天才挤出一句“……我没拿。”\n"
        "🔴 反面：**“我没偷。”** —— 意思没错，可谁都能这么说，这句话写不出这个人。\n"
        "写对话前先想清楚：**这个角色是什么性子**，让他用符合性子的方式说，"
        "不要写一句放之四海皆准的中性话。\n"
    )
    # 2026-09-21 加（POC-1d 验证有效，作者拍板）：反 AI 味「对照示范」——覆盖四类重灾区 + 动作带意图。
    # 设计原则：每条反例配 1~2 条正例（正例数 ≥ 反例，防 priming 反噬）；不写规则只给对照。
    task_lines.append(
        "【描写对照示范——硬性要求，照此自查（只学判断标准，不学词句内容）】\n"
        "下面几类描写是“读起来像机器写的”重灾区。每类先给不要写成这样（一个字都不许模仿），\n"
        "再给可以怎么写（照此模仿）。\n"
        "一、环境描写要挂在人身上\n"
        "  ✗ 油灯芯爆了个花，两个人影在墙上晃了一下。外头巷子里有狗叫，远远的，一声两声。\n"
        "  ✓ 灯芯噼啪一响，周管事的手跟着抖了一下。\n"
        "  ✓ 巷子里的狗叫起来，他往门口看了一眼，又转回来。\n"
        "  （环境只在它影响到人的时候写；写它是为了写人，不是为了写它。）\n"
        "二、比喻要能用，拿不准就照实说\n"
        "  ✗ 他重复了一遍，嗓子里像卡了粒石子。\n"
        "  ✓ 他又问了一遍，声音比头一回低。\n"
        "  ✓ 他又问了一遍，问完自己先把眼睛移开了。\n"
        "  （比喻只在真能让人看见那个东西时才用；拿不准就写声音、写速度、写动作。）\n"
        "四、不要给读者递暗号\n"
        "  ✗ 他没让周德贵看见自己指尖那一点极淡的青光。\n"
        "  ✓ 他把手往袖子里缩了缩。\n"
        "  （该藏的东西就让它藏着——读者没被提醒，比被提醒了更好看。）\n"
        "五、动作要带着意图，不是流水账\n"
        "  ✗ 他推开偏房的门，摸黑找到床底下的布包。残卷还在。他把它揣进怀里，又摸到枕头底下"
        "那几枚铜板，塞进袖口。然后他走出来，经过掌柜身边，没停。\n"
        "  ✓ 他摸回偏房，先把布包从床底抠出来塞进怀里——残卷没了才是真要命；铜板倒不急，"
        "可他还是顺手抄了。经过掌柜身边时，他脚步没慢。\n"
        "  （每个动作都该有它在意的理由；只记录“先做什么再做什么”的段落要重写。）\n"
    )
    # 2026-09-25 加（作者实测指出第一稿的两大病：① 掺杂无关描述 ② 不符逻辑）。
    # 设计原则同 1d：**对照示范**（反例 + 正例），不堆禁令。
    task_lines.append(
        "【无关段落自查（硬性）】\n"
        "一段话要么推进剧情，要么写人；**两样都不占的段落不要写**。\n"
        "  ✗ 天色已经暗下来了。远处传来几声狗叫，风从巷口吹进来，吹得门帘轻轻晃动。"
        "他把茶盏端起来，又放下，茶水已经凉了。\n"
        "  ✓ 天色暗下来，他没点灯。掌柜的还没回来，他得赶在那之前把账对完。\n"
        "  （✗ 那段没有剧情、没有人物变化，删掉不影响任何东西；"
        "✓ 把同一段时间接上了人物此刻的目的。）\n"
    )
    task_lines.append(
        "【逻辑合理性自查（硬性）】\n"
        "人物做的事，要符合他**当下知道什么、想要什么、做得到什么**；"
        "一件事发生了，后面的反应要接得上。\n"
        "  ✗ 班头一把扯出布包，解开，五块灰白石头滚在掌心。他捏了捏，眉头皱起。"
        "“这石头烫手？” “雨后回潮，捂热的。” 班头把石头丢回布包，扔进他怀里。\n"
        "    ——（搜出了可疑物件，却只看一眼就还回去："
        "**他若起疑就该追问，若不起疑就不该搜**，两头都不占。）\n"
        "  ✓ 班头把石头翻来覆去看了几遍，又抬眼盯他：“山里捡的？” “塌方处捡的。”"
        "班头没再问，把石头塞回布包，却把药篓扣下了。\n"
        "    ——（追问 + 留一手：既合他的身份，也让下一场有东西可续。）\n"
        "写下一句前问自己一句：**他为什么要这么做**；答不上来就换个写法。\n"
    )
    # 🔴 2026-09-25 加（作者最初就提的核心诉求："读起来像十几年前金庸那代"）。
    # 前面的示范管的是「分段/对话/描写/注意力/逻辑」，**没有直接管语体**——
    # 实测新稿仍出现「三角眼」「目光如钩子」「却在」「不由得心中一凛」这类上一代网文的腔。
    # 原则同 1d：只给对照（反例 + 正例），不堆禁令。
    task_lines.append(
        "【语体表对照（硬性）——要写当代网文的白话，不是武侠/评书/古典白话】\n"
        "下面这些一出现，整段就会“像上一代人写的”：\n"
        "一、面相与目光\n"
        "  ✗ 他那双三角眼眯成一条缝，目光如钩子般钉在陈峰身上，上下打量了一圈。\n"
        "  ✓ 周德贵从柜台后抬起头，盯了他两秒：“这批药误了时辰，把你卖了都赔不起。”\n"
        "二、招式化的动作与比喻\n"
        "  ✗ 他冷哼一声，身形一闪，已掠至丈许之外，目光如鹰隼般扫过全场。\n"
        "  ✓ 他哼了一声，转身就走，步子迈得很大，出门前又回头看了一眼。\n"
        "三、评书腔的转场与心理\n"
        "  ✗ 却说这陈峰回到铺中，只见掌柜面色铁青，不由得心中一凛。\n"
        "  ✓ 陈峰回到铺里，周德贵的脸已经沉下来了。\n"
        "🔴 判断标准：**这句话能不能让人物自己说出来**。\n"
        "   只在古装剧、评书、武侠里听得到的说法（三角眼、目光如X、身形一闪、却说、\n"
        "   不由得心中一凛、冷哼、面色铁青、XX般），一律换成当代人会说的话。\n"
    )
    # 🔴🔴 2026-09-25 作者点出的**根因**（比前面所有单点修复都深）：
    #   AI 对「正常的感觉与行为」没有正确认知——不知道摸到温热的东西是什么触感、
    #   不知道累是什么感觉、不知道看见人的第一反应是什么，于是只能靠「堆描写 + 生造比喻」凑，
    #   这就是 AI 味的来源。作者原话：
    #   「AI 对于正常的触感、观感、听感、语感、情感、动作这类正常的感觉/行为都没有正确的认知，
    #     所以导致会有 AI 味，few-shot 和提示词应该用这些来告诉它。」
    #
    # 🔴 正例一律取自**真书原文**（《没钱修什么仙？》，作者指定的文风基准）——作者明确指出：
    #   「正例你去真书里找原句，不要自己造；反例你自己根据原正句造；类别可以更全面。」
    #   反例 = 把真句改成模型最容易写出的那种。
    task_lines.append(
        "【感官与行为常识（硬性）——写“正常人真会有的感觉”，不要写“看起来有文采的感觉”】\n"
        "🔴 **写完一个正确的感觉，就停手**：不要追加解释、不要升级、不要再补一个比喻。\n"
        "   （追加的那句几乎必然是比喻，几乎必然是 AI 味——作者原话：\n"
        "    「写到胸腔剧烈起伏就够了，后面的就 AI 味明显了。」）\n"
        "下面每组：✗ 是模型最容易写出的那种，✓ 是**真书原文**（《没钱修什么仙？》）。\n"
        "一、累与喘：写一个就够，不要叠\n"
        "  ✗ 他喘着粗气，胸腔剧烈起伏，每一次呼吸都带着铁锈味。\n"
        "  ✓ 只见他砰的一声坐倒在地上，整个人已经大口大口喘起气来。\n"
        "  ✓ 花了一个半小时，换了两部车之后，满身是汗的张羽终于挤下公交车。\n"
        "  （真书写累 = **一个声音或一个动作**；把同一个意思换三种说法写三遍，就是机器在凑。）\n"
        "二、触感：摸到什么就是什么，不要升级\n"
        "  ✗ 那股微温顺着掌心爬上来，像是一根烧红的细针扎进了肉里。\n"
        "  ✓ 摸了摸有些瘪的肚子，张羽干脆站了起来。\n"
        "  （温热**不是**刺痛。「顺着掌心爬上来」「扎进肉里」跟「摸到温石头」这件事本身是冲突的。）\n"
        "三、慌与怕：写**不同部位**的反应，不要重复同一个\n"
        "  ✗ 一股寒意从脊背窜上来，他的瞳孔骤然收缩，心脏仿佛被一只无形的手攥紧。\n"
        "  ✓ 冷汗从头上冒了出来，恐慌从心中涌起，不断书写的手掌开始发软，开始用不上力。\n"
        "  （真书也能写三句，但每句是**一个具体部位**（汗 / 心 / 手），不是同一个意思说三遍。）\n"
        "四、看见人：先看见整体，不要先看见细节\n"
        "  ✗ 面试官的目光如鹰隼般锁在他身上，上下打量，似乎要将他看穿。\n"
        "  ✓ 他看向三位面试官，露出了练习许久的礼貌笑容。\n"
        "五、听见：写**听见了什么**，不要写“声音像什么”\n"
        "  ✗ 那声音像钝刀刮过骨头，在寂静中格外刺耳。\n"
        "  ✓ 良久之后，前方传来一阵叫号声。\n"
        "六、语气：由**当下处境**定，不由“性格标签”定\n"
        "  ✗ 他冷冷地说：“滚。”（每次都冷）\n"
        "  ✓ 张羽老实道：“我想考上名牌大学，嵩阳高中是我能报考的学校中，大学录取率最高的一个。”\n"
        "  （真书让人**直接说出目的**，不写他怎么个冷法。）\n"
        "七、情绪：落在**动作和结果**上，不要解释情绪\n"
        "  ✗ 他心里又急又怕，却强作镇定，眼神中闪过一丝不易察觉的挣扎。\n"
        "  ✓ 说罢，他便将张羽的简历扔进了一旁的纸篓里，和另外几百份备选简历挤在了一起。\n"
        "八、钱与物：给**具体数字或具体动作**，不要写“很贵”\n"
        "  ✗ 这笔钱对他而言无疑是一笔天文数字，压得他喘不过气来。\n"
        "  ✓ 从两三千到七八千……最后一次让她直接打了两万块钱。\n"
        "九、时间过去：写**发生了什么**，不要写“时间在流逝”\n"
        "  ✗ 时间一分一秒地流逝，他的心一点点沉了下去。\n"
        "  ✓ 转眼便是两个小时过去，张羽深深吐出一口气来，只觉得此刻浑身酸痛。\n"
        "🔴 自检一句：**如果我把这段的感觉描写全删掉，人物还在不在？**\n"
        "   删掉后只剩一堆「感觉」，说明写的是机器凑出来的感觉，不是这个人的。\n"
    )
    # 🔴🔴 2026-09-25 作者第二次点出的根因（与感官常识同源，但方向不同）：
    #   AI 不懂**人情世故**——它不知道面对官差、上位者、债主时，一个正常人本能会先软下来。
    #   它把「主角人设」当成唯一驱动力，于是写出「语气恭敬却不卑微」这种**解说词**：
    #   作者原话：「普通人面对官爷，哪怕他再怎么不凡，也会表面上讨好的。」
    #   「即使主角再特殊，面对身份的巨大差异、或者需要别人帮忙时，人是会变通的。」
    task_lines.append(
        "【人情世故与身份差（硬性）——面对能决定自己处境的人，正常人先想的是把事办成】\n"
        "🔴 原理：主角「不凡」不体现在**当面怎么顶**，而体现在**事后怎么想、怎么做**。\n"
        "   面对官差、上级、债主、能卡住自己的人，一个正常人本能地**先软下来**，这不丢人设，是生存本能。\n"
        "下面每组：✗ 是模型最容易写出的那种，✓ 是**真书原文**。\n"
        "一、开口先报身份＋来意，必要时顺一句好话\n"
        "  ✗ 「回春堂的伙计，给巡检司送止血散。」他微微躬身，语气恭敬却不卑微。\n"
        "  ✓ 「在下柯宇，奉令来给三位前辈传达任务和带路的。」（《凡人修仙传》）\n"
        "  ✓ 「三位好，我是东阳初级中学的张羽。」（《没钱修什么仙？》）\n"
        "  （「语气恭敬却不卑微」是**解说词**不是写法——要让他**做一件讨好的事**，别给他贴标签。）\n"
        "二、示弱要带身体：笑、汗、小动作，不要写「不卑不亢」\n"
        "  ✗ 他神色如常，不卑不亢地答话，没有露出半点怯意。\n"
        "  ✓ 咽了一口唾沫，罗布抹了把额头上细密的冷汗，脸庞露出一抹难看的笑容，\n"
        "    他极为识相的干声道：「全依大人所言。」（《斗破苍穹》）\n"
        "  ✓ 老者眼珠微转几下后，先前异色荡然无存，竟一脸赔笑的说道。（《凡人修仙传》）\n"
        "  （真人的紧张是**咽唾沫、擦汗、笑得难看**；「不卑不亢」是人设标签，不是现场反应。）\n"
        "三、求人：话要留余地，给对方台阶\n"
        "  ✗ 「我需要你帮我一个忙，你答不答应？」\n"
        "  ✓ 「前辈手里是否还有和元玉，能否再多给晚辈几块。」（《凡人修仙传》）\n"
        "  （用「能否」「能不能」「方不方便」，让对方**有拒绝的余地**——这是求人的基本礼数。）\n"
        "四、被盘问、被刁难：先把眼下应付过去\n"
        "  ✗ 他冷冷地看了对方一眼，一言不发。\n"
        "  ✓ 毕竟是面对高一个年级的学长，赵天行知道任何高一新生面对这种情况，\n"
        "    都只能乖乖低头。（《没钱修什么仙？》）\n"
        "🔴 什么时候**才**可以硬（只有这三种）：\n"
        "   ① 对方是死敌、已经撕破脸；② 要他做违背底线的事（出卖人、背弃承诺）；\n"
        "   ③ 已经没有退路，横竖都是死。除此之外，心里再不服，表面也得把事圆过去。\n"
        "🔴 自检一句：**把这段的人名遮掉，还看得出谁的地位更高吗？**\n"
        "   看不出来就是没写出身份差——那写的是「两个人在交换信息」，\n"
        "   不是「一个有求于另一个」。\n"
    )
    # 🔴🔴 2026-09-25 作者第三次点破的**幻觉根因**：
    #   「AI 知道这是很重要的东西，就假想书中的所有人/某一个角色知道（即使不考虑符不符合剧情）」
    #   这是**知识泄漏**——把作者/读者掌握的信息塞给角色，于是主角凭空紧张、旁人无来由起疑、
    #   物品为了制造悬念自己跳动。作者给的判据：
    #   「主角起初并不知道，对它的感觉应该保持着**单纯的好奇**。就像家里一件旧物，
    #     不知道是古董时随手用；有人点破或自己意识到之后，才开始小心珍视。」
    #   ⇒ **珍视必须发生在「知道」之后**。
    task_lines.append(
        "【角色认知边界（硬性）——角色只能知道**他见识范围内**的事】\n"
        "🔴 最常见的错误是**知识泄漏**：你知道这是灵石/宝物，就假想角色也知道、也紧张、也患得患失。\n"
        "一、**你（作者）知道的 ≠ 角色知道的**\n"
        "  ✗ 还没人告诉他这是什么，他却「心头一悸」「仿佛有什么东西产生了共鸣」，紧张得手心出汗。\n"
        "  ✓ 他掂了掂，觉得这石头比寻常的沉，颜色也怪，就揣进怀里——想着拿回去垫桌脚正好。\n"
        "  （作者原话：「主角起初并不知道，对它的感觉应该保持着**单纯的好奇**。」）\n"
        "二、**情绪强度必须和「他掌握了多少信息」匹配**\n"
        "  🔴 判据：**他凭什么这么紧张？** 把理由找出来；找不到，就是你（作者）在替他紧张。\n"
        "  （就像家里一件旧物，不知道是古董时随手用；**等有人点破、或自己意识到之后**，\n"
        "    才开始小心珍视——**珍视必须发生在「知道」之后**，不能发生在之前。）\n"
        "三、**旁人起疑必须有剧情内的理由**\n"
        "  ✗ 班头盯着他怀里的布包看了两息，冷不丁问：「你怀里揣的什么？」\n"
        "  ✓ 班头是听说了西山的事才多问一句／他自己看见石头露出了一角。\n"
        "  （旁人不是算命的；他要起疑，得**看见了、听说了、认得成色**。）\n"
        "四、**不要替角色过度解读别人的眼神**\n"
        "  ✗ 那不是看伙计的眼神，那是看贼的眼神，或者说，是在寻找猎物的眼神。除非……\n"
        "  ✓ 周德贵从算盘后抬起头，只问他药送到了没有。\n"
        "  （把一个普通眼神解读成「看贼」「猎物」，是**被害妄想**；真人的默认判断是就事论事。）\n"
        "五、**心理活动一处就够**，不要反复揣测同一件事\n"
        "  （前面已经想到了，后面别再想一遍；反复揣测＝注水，而且显得角色疑神疑鬼。）\n"
        "  🔴 **好奇的节奏是「发现 → 好奇 → 放下」**：\n"
        "    「哦，这石头沉得有点怪」——然后就去干别的了。真人不会抱着一件没搞明白的东西反复推演，\n"
        "    那是悬疑小说主角才有的习惯，不是药铺伙计的。\n"
        "六、**角色说不出他没听过的名词**（术语边界）\n"
        "  ✗ 他心里犯嘀咕：如果真是**灵石**，掌柜的为什么还扣我工钱？\n"
        "  ✓ 他心里犯嘀咕：这石头要真是宝贝，掌柜的为什么还扣我工钱？\n"
        "  （一个药铺杂役知道「仙师」「仙人」，但**没听过「灵石」「灵气」「引气入体」**——\n"
        "   这些词只能从**别人嘴里**说出来，不能出现在他的心里和嘴上。）\n"
        "七、**不写在意，就是不在意**——不要表演随意（2026-09-25 作者点破）\n"
        "  ✗ 那五块石头依旧安静地躺在那里，既没有发热，也没有跳动，就像普通的顽石一样。\n"
        "  ✗ 「真是垫桌脚的。」他自言自语……看来，这真的只是一块普通的石头。也许是自己多心了。\n"
        "  ✓ 他把石头推到桌底，去忙别的了。（完。）\n"
        "  （反复强调「没有异常」「只是普通石头」，等于在向读者证明它**不寻常**——\n"
        "   真正的不在意是**提完就忘**：描写一结束，人和镜头都走，不再回头看。🚫 不要自证清白。）\n"
        "🔴 **章末钩子不是每章必须**（2026-09-25 作者拍板）：\n"
        "  本章主线是小事（办完差、回了家、聊了天），**平静收尾完全成立**。\n"
        "  🔴 不要为了钩子硬造事件——物品自己异动、突然的感悟、无来由的不安，都是硬造。\n"
        "  如果本章确需钩子，让它来自**人**：\n"
        "  ✓ 隔壁的瞎眼老头忽然在院里喊了一句：「谁动了西山的土？」\n"
        "  ✓ 周德贵今晚第一次亲自到后院来看他碾药，站了一会儿，什么也没说就走了。\n"
        "🔴 **主线由章要点决定**：要点写什么，本章就写什么。\n"
        "   上面的纪律只管「怎么写」，不管「写什么」——\n"
        "   不要把要点里顺带一提的东西写成主线，更不要围着它加戏设钩。\n"
    )
    _add(_mk("task", "【本章任务】", "\n".join(task_lines),
             P_CRITICAL, order=90, required=True))

    plan: BudgetPlan = plan_budget(blocks, level)

    # ---------- 5. 系统提示词：底线 + SKILL + 去 AI 味 ----------
    sys_parts = [BASE_SYSTEM]

    skill_block = skill_dispatch.build_block(db, "chapter")
    if skill_block:
        sys_parts.append(skill_block)

    # ---------- 5.5 E4 全局条目库注入（docs/03 阶段E）----------
    # 通用词候选 + 造物尺度进 system：AI 取名/造物优先用惯例词，防"伐骨丹"式硬造。
    # 回滚开关 app_configs globalref.inject_on_generate（默认开，置 False 即关）。
    # 条目库为空/查询异常一律跳过，不阻断生成。
    if app_config.get(db, app_config.KEY_GLOBALREF_INJECT, True):
        try:
            _proj = db.query(ProjectORM).filter_by(id=project_id).first()
            _ref_block = global_ref_crud.build_injection_block(
                db, genre=getattr(_proj, "genre", None))
            if _ref_block:
                sys_parts.append(_ref_block)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[builder] 全局条目库注入跳过: {type(e).__name__}: {str(e)[:80]}")

    if app_config.get(db, app_config.KEY_HUMANIZE_INJECT, True):
        # 2026-09-13 拍板：normal 档的禁令+写法准则与「去AI味·网文正文」SKILL 内容大量重复，
        # 双份「删减指令」把模型推向过度节俭（实测段均 12 字、句句成段）。
        # 统一降为 light：只保留最毒的 4 条句式禁令做双保险，完整纪律以 SKILL 为准。
        sys_parts.append(humanizer.build_prompt_block(scene="novel", level="light"))

    system = "\n\n".join(p for p in sys_parts if p and p.strip())

    user = plan.render()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    meta = {
        "budget": plan.debug(),
        "references": ref_detail,
        "mentions": {k: sorted(v) for k, v in mentions.items()},
        "entity_graph": graph_trace,
        "skills": skill_dispatch.explain(db, "chapter"),
        "layer_mode": lmode,
        "system_chars": len(system),
        "user_chars": len(user),
        "total_chars": len(system) + len(user),
    }
    return messages, meta


def build_discussion_system(
    db: Session,
    project_id: str,
    *,
    chapter_id: str | None = None,
    chapter_no: int | None = None,
    article_id: str | None = None,
    budget_level: str | None = None,
    query_text: str = "",
) -> tuple[str, dict]:
    """组装剧情商讨的系统提示词。

    改造前商讨是个「瞎子」——只有一句通用人设，作者问「张三现在什么境界」它只能编。
    这里把世界观、角色、伏笔、最近记忆一并注入，商讨才谈得上有依据。
    商讨不需要上一章全文，预算比生成低一档。
    """
    level = _resolve_level(db, budget_level)
    if level == "loose":
        level = "standard"   # 商讨吃不下那么多，也没必要

    ref_no = chapter_no if chapter_no is not None else 10 ** 6  # 未指定则视作「最新」
    blocks: list[Block] = []

    def _add(b: Block | None):
        if b is not None:
            blocks.append(b)

    # ---------- 0. 实体识别 + 一跳扩展（08-B11：A5 GraphRAG 扩展到商讨路径）----------
    # 章节生成路径早有此能力（本文件 :184），商讨此前一直没接：作者问「张三的师父是谁」，
    # 张三的师父在 layer_characters 里只是一行简写 → 顾问只能现编。
    # 现在把**作者问题里提到的实体**做图扩展（师父/同门/所属宗门/关联地点提升为 focus），
    # 这些实体就会拿到全量人设/势力描述。仅对 query_text 生效（用户在问什么才扩展什么）。
    mentions: dict[str, set[str]] = {
        "characters": set(), "factions": set(), "locations": set(),
    }
    graph_trace: dict = {}
    if (query_text or "").strip():
        mentions = layers.extract_mentions(db, project_id, query_text)
        try:
            from app.services import entity_graph
            if entity_graph.enabled(db):
                mentions, graph_trace = entity_graph.expand(db, project_id, mentions)
            else:
                graph_trace = {"enabled": False, "reason": "配置关闭"}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[context.builder] 商讨实体图扩展跳过: {type(e).__name__}: {str(e)[:80]}")
            graph_trace = {"enabled": True, "error": f"{type(e).__name__}: {str(e)[:80]}"}

    # 设定库改为 B 方案：目录 + 按需加载，不再把 description 常驻注入 system。
    # 目录由 build_setting_catalog 生成并附在 system 末尾，详情按 LOAD_SETTING 拉取。
    _add(layers.layer_characters(db, project_id, focus_names=mentions.get("characters")))
    _add(layers.layer_entities(db, project_id, focus=mentions))
    _add(layers.layer_foreshadows(db, project_id))
    _add(layers.layer_stage_summaries(db, project_id, ref_no))
    _add(layers.layer_recent_memories(db, project_id, ref_no, limit=5))

    plan = plan_budget(blocks, level)

    sys_parts = [DISCUSSION_SYSTEM]
    body = plan.render()
    if body:
        sys_parts.append(
            "以下是这部作品的现有资料（角色/势力/伏笔/记忆等），回答必须以此为准：\n\n"
            "⚠️ 重要：当问题涉及官制品级、境界等级、货币体系等世界观设定时，"
            "若你已通过 LOAD_SETTING 加载了对应体系的完整说明，必须严格以其数据作答，"
            "不得使用你训练数据中的其他朝代或通用知识替代；"
            "若尚未加载，请先输出 LOAD_SETTING 加载后再答（见下方【设定库目录】）。\n\n"
            + body
        )

    skill_block = skill_dispatch.build_block(db, "discussion")
    if skill_block:
        sys_parts.append(skill_block)

    # 触发式顾问指令：仅当作者最新一条消息是「征询意见/方案/方向」类提问时才追加，
    # 事实类问答不强制给 2~3 个方案，避免硬凑。
    if is_advice_request(query_text):
        sys_parts.append(ADVICE_DIRECTIVE)

    # 设定库目录（B 方案）：仅列 id + 名称，详情按需 LOAD_SETTING 加载，
    # 避免把全部设定描述常驻塞爆窗口。风格与参考文档目录一致（只给标识，靠模型按需请求）。
    try:
        setting_catalog = layers.build_setting_catalog(db, project_id)
        if setting_catalog:
            sys_parts.append(setting_catalog)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[context.builder] 设定目录构造失败，跳过: {e}")

    system = "\n\n".join(p for p in sys_parts if p and p.strip())
    meta = {
        "budget": plan.debug(),
        "skills": skill_dispatch.explain(db, "discussion"),
        "system_chars": len(system),
        # 观测一致性：与章节生成路径同键（08-B11），前端/日志可看「这次商讨扩展了谁」
        "entity_graph": graph_trace,
    }
    return system, meta

    # ---------- 1.6 GraphRAG 注入块（docs/09 §3，阶段 A3/A5）----------
    # 新关系层（entity_relations：角色-技能-物品-势力-地点 任意实体对）一跳装配，
    # 产出**独立注入块**（技能/物品/势力/地点 + 关系网）；死亡角色只给「名字+关系+已死亡」。
    # 与上面的 mentions 扩展互补：那边扩的是"要全量渲染的实体"，这边补"关系与关联物"。
    try:
        from app.services import graph_rag
        if graph_rag.enabled(db):
            seed_names = sorted(mentions.get("characters") or set())[:10]
            _inj = graph_rag.build_injection(db, project_id, names=seed_names)
            if _inj:
                _add(_mk(key="graphrag", title="关联实体与关系网（一跳）",
                         content=_inj, priority=P_CRITICAL, order=45))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[builder] GraphRAG 块跳过: {type(e).__name__}: {str(e)[:80]}")
