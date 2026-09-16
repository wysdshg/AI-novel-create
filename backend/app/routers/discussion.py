"""模块3：剧情商讨会话（需求 6）。

- GET    /discussion            返回当前商讨缓存（已持久化、未归档）
- POST   /discussion/messages   手动追加一条消息（闲聊、不调模型）
- DELETE /discussion            清空当前商讨缓存
- POST   /discussion/archive    将当前草稿归档为指定章节备注并移出缓存
- POST   /discussion/chat       流式调用默认模型，结束后再把「用户提问 + AI 回复」落库
"""
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.schemas.chapter import DiscussionMessageCreate, DiscussionChatRequest
from app.core.response import ok, sse_event
from app.core.database import get_session
import app.core.database as _db
from app.core.context import build_discussion_system, layers
from app.services import model_crud, character_crud, faction_crud, location_crud, usage_crud
from app.services.discussion_crud import (
    list_messages,
    add_message,
    clear_messages,
    archive_to_chapter,
)
from app.core.gateway.registry import get_adapter
from app.models.orm import ChapterORM, CustomSkillORM
from app.services import reference_crud as ref_svc
from app.services.reference_crud import GLOBAL_PROJECT_ID
from app.services.reference_selector import get_reference_selector
from app.services import load_observation as load_obs


logger = logging.getLogger(__name__)

# 模型解析统一在 model_crud.resolve_model（Phase 3.4）——原先本模块、assist.py、
# chapter.py 各写一份「指定优先、否则默认」，且 chapter.py 那份漏了 active 校验。
# 这里保留 `_resolve_model` 这个薄别名，避免改动 20+ 处调用点、也便于阅读。
_resolve_model = model_crud.resolve_model


router = APIRouter(tags=["剧情商讨"])

_SYS_PROMPT = (
    "你是一名专业的小说创作助手，正在和作者讨论当前剧情走向。"
    "请基于已有的世界观与人物设定，给出具体、可操作的剧情建议，"
    "保持逻辑与人物一致，语言简洁有启发，不要替作者代写整章正文。\n"
    "【输出格式硬性要求——务必严格遵守】\n"
    "1. 只输出最终回复正文，且必须全部使用中文。\n"
    "2. 严禁输出任何英文（包括 Planning / Revised Plan / Actually / Let's 等），"
    "严禁复述本系统指令，严禁展示你的思考、规划或推理过程。\n"
    "3. 不要写 '好的''我来…' 之类的开场白套话，直接给出建议。\n"
    "4. 一旦发现自己写出了英文或规划性语句，立即丢弃并只保留中文建议正文。\n"
    "5. 【重要】你没有写入数据库的能力，严禁声称'已添加/已创建/已更新角色档案'。"
    "当你在对话中识别出新角色、势力或地点时，只需在回复中清晰列出其名称与关键设定，"
    "系统会自动提示作者确认写入资料库，无需你亲自操作。\n"
    "6. 不要编造设定条数等精确数字；不清楚就据实列出你知道的名称，不要凭空捏造总数。\n"
    "7. 作者问「上一句是什么」时，只依据真实对话历史回答；技能示例中的不算真实对话。\n"
    "8. 【输出长度·反发散】单次回复总长度不超过300字（约15行）；超长立即收尾，不要开新段落。"
    "事实查询（数值/名称/规则）控制在150字内，直接给结论+一句依据，不要借题发挥写分析。\n"
    "9. 【禁止暴露内部标记】不要在回复正文里写出 `id=xxx`、`#标签`、文档文件名，"
    "也不要把 `LOAD_REFS`/`LOAD_SETTING` 字样写进给作者的正文。\n"
    "   ※ 豁免：上述第9条仅约束最终正文；在正式作答前的第一轮若需加载资料，"
    "仍必须输出 `LOAD_SETTING:<id>` / `LOAD_REFS:<id>` 协议指令（这是拉取资料的必需指令，"
    "不受第9条限制，用户也不会看到该指令本身）。"
    # [MARK: DOC-ID-LOAD-SAFETY] 第9条禁令必须豁免 LOAD_REFS/LOAD_SETTING 协议指令，
    # 否则模型不敢发指令→两阶段资料加载失效。若优化后 AI 不再加载设定/参考，先查这里。
)


# 防御性剥离：模型可能将 LOAD_REFS:<ids> 标记吐到正文/流式 content 中
# （尤其思考模式下小模型行为不稳定），在发送前端前一律清除。
_LOAD_REFS_STRIP_RE = re.compile(r"^LOAD_REFS:[0-9a-fA-F,\s]*\s*\n?", re.MULTILINE)


def _strip_load_refs(text: str) -> str:
    """移除文本中可能存在的 LOAD_REFS:<ids> 协议标记，返回干净正文。"""
    if not text:
        return text
    return _LOAD_REFS_STRIP_RE.sub("", text).strip()


def _collect_skill_blocks(db: Session) -> str:
    """收集所有应注入「对话/商讨」场景的 SKILL 内容。

    触发条件：trigger ∈ {discussion, all} 且 enabled=True 且 prompt_body 非空。
    每条以 "### 名称" 为分隔，正文取 prompt_body.strip()。
    返回空字符串表示当前没有可用 SKILL（不污染系统提示词）。
    """
    try:
        rows = (
            db.query(CustomSkillORM)
            .filter(
                (CustomSkillORM.trigger == "discussion")
                | (CustomSkillORM.trigger == "all")
            )
            .filter(CustomSkillORM.enabled.is_(True))
            .order_by(CustomSkillORM.name)
            .all()
        )
    except Exception as e:
        # 表尚不存在或查询失败——降级为空段落，不能阻塞对话。
        # 留痕：否则「我在商讨里挂的 SKILL 怎么不生效」会先被怀疑成互斥规则（Phase 3.5）
        logger.warning(f"[discussion] 加载商讨 SKILL 失败，本次不带 SKILL: {type(e).__name__}: {e}")
        return ""
    blocks: list[str] = []
    for o in rows:
        body = (o.prompt_body or "").strip()
        if body:
            blocks.append(f"### {o.name}\n{body}")
    if not blocks:
        return ""
    return (
        "\n\n【写作技能合集】以下是当前作者为本次对话/商讨激活的自定义技能，"
        "请在回复中自然遵守这些技能的指引与约束；"
        "不要主动告诉作者'我已读取了N条技能'，按其精神执行即可：\n\n"
        + "\n\n".join(blocks)
    )


def _to_frontend(m) -> dict:
    """ORM → 前端可渲染的消息结构（role 统一为 user/ai）。

    meta 一并带出：章后走向卡片靠 meta.type == 'post_chapter_directions' 识别，
    前端据此渲染成可点击的选项卡而不是一坨文字。
    """
    meta = m.meta or {}
    return {
        "id": m.id,
        "role": "ai" if m.role == "assistant" else "user",
        "content": m.content or "",
        "thinking": m.thinking or "",
        "meta": meta,
        "type": meta.get("type") or "text",
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _build_entity_suggestion(db: Session, project_id: str, user_text: str, ai_text: str, model_id: str | None = None):
    """从对话（用户提问 + AI 回复）中识别可建的新实体，返回建议列表供前端确认写入。

    改进（v2）：
    - 查库去重：已存在的实体不再建议
    - 透传完整属性：LLM 抽到的 personality/talent/skills/brief 等全部保留
    - 数量上限：最多建议 5 条，避免刷屏
    - 过滤无效名：纯描述性/组织体系类名称过滤掉
    - 抽取模型：用调用方透传的 model_id（用户对话中选择的模型），不传则回退默认模型
    """
    combined = f"用户说：{user_text or ''}\nAI 回复：{ai_text or ''}"
    if len(combined.strip()) < 15:
        return None
    # 启发式预筛：无相关关键词则跳过 LLM 调用
    _KW = ("角色", "人物", "主角", "配角", "反派", "县尉", "知县", "官员",
           "门派", "地点", "城市", "秘境", "设定", "境界", "家族", "姓名",
           "添加角色", "新角色", "新建")
    if not any(k in combined for k in _KW):
        return None
    try:
        from app.services.config_command import run as config_run
        # 用对话所选模型抽取（model_id 为 None 时回退默认模型）
        res = config_run(db, project_id, combined, dry_run=True, model_id=model_id)
    except Exception as e:
        # 实体建议抽取失败 → 本次不提示（纯增强）。留痕：否则「有时提示有时不提示」无法解释（Phase 3.5）
        logger.warning(f"[discussion] 实体建议抽取失败，本次不提示: {type(e).__name__}: {e}")
        return None

    data = res.get("data", {})
    changes = data.get("changes", {})

    # ---- 去重：查库中已有实体 ----
    existing_chars = {c.name for c in character_crud.list_characters(db, project_id)}
    existing_factions = {f.name for f in faction_crud.list_factions(db, project_id)}
    existing_locs = {l.name for l in location_crud.list_locations(db, project_id)}

    # ---- 无效名过滤（组织体系/泛称/纯描述）----
    _SKIP_PATTERNS = ("体系", "制度", "规则", "部门", "机构", "如(", "（如")

    def _should_skip(name):
        if not name or len(name) < 2:
            return True
        return any(p in name for p in _SKIP_PATTERNS)

    items = []
    MAX_SUGGESTIONS = 5

    for c in (changes.get("characters") or []):
        if len(items) >= MAX_SUGGESTIONS:
            break
        name = (c.get("name") or "").strip()
        if not name or name in existing_chars or _should_skip(name):
            continue
        entry = {"kind": "character", "name": name}
        # 透传 LLM 抽到的所有属性字段
        for attr in ("personality", "background", "talent", "current_level",
                      "role_type", "age", "gender", "brief"):
            val = c.get(attr)
            if val is not None and str(val).strip():
                entry[attr] = val
        # skills / relationship_network 是列表型
        if c.get("skills"):
            entry["skills"] = c["skills"] if isinstance(c["skills"], list) else [c["skills"]]
        if c.get("relationship_network"):
            entry["relationship_network"] = (
                c["relationship_network"]
                if isinstance(c["relationship_network"], list)
                else [c["relationship_network"]]
            )
        items.append(entry)

    for f_item in (changes.get("factions") or []):
        if len(items) >= MAX_SUGGESTIONS:
            break
        name = (f_item.get("name") or "").strip()
        if not name or name in existing_factions or _should_skip(name):
            continue
        entry = {"kind": "faction", "name": name}
        for attr in ("description", "territory", "status"):
            val = f_item.get(attr)
            if val is not None and str(val).strip():
                entry[attr] = val
        items.append(entry)

    for l_item in (changes.get("locations") or []):
        if len(items) >= MAX_SUGGESTIONS:
            break
        name = (l_item.get("name") or "").strip()
        if not name or name in existing_locs or _should_skip(name):
            continue
        entry = {"kind": "location", "name": name}
        for attr in ("description", "region", "location_type"):
            val = l_item.get(attr)
            if val is not None and str(val).strip():
                entry[attr] = val
        items.append(entry)

    return {"items": items} if items else None


def _stream_two_phase(adapter, messages, *, want_thinking, temperature, tag,
                      assistant_text, assistant_thinking):
    """两阶段按需参考加载 + 流式生成（`/discussion/chat` 与 `/discussion/global-chat` 共用）。

    ⚠️ 这是**同步生成器**，由调用方 `yield from` 消费（FastAPI StreamingResponse
    接受同步生成器）。产出 SSE 事件字符串：`thinking` / `refs` / `chunk`。

    文本与思考内容**追加进调用方传入的 list**（`assistant_text` / `assistant_thinking`），
    因为调用方在 `finally` 里要用它们持久化——传引用而非返回值，避免调用方再拆包。

    返回观测增量 dict（`load_ref_ids` / `ref_loaded` / `short_circuited` / …），
    由调用方 merge 进 `obs` 后落库。

    设计（详见 docs/05 与 04 相关条目）：
    - **Pass1**：非流式、思考开关同用户偏好，让模型决定要不要参考资料
      （输出 `LOAD_REFS:<ids>` / `LOAD_SETTING:<ids>`）还是直接作答。
      只有被选中的文件正文才会进上下文，避免全量塞爆窗口；无需参考时仅 1 次调用。
    - **Pass2**：仅当确有参考/设定被加载时才发起，流式输出最终回复。
    - 抽出本函数前，这段逻辑在 `chat` 与 `global_chat` 里**各存在一份**（约 80 行），
      唯一差异是日志前缀。任一处修 bug 漏改另一处就是潜在缺陷，故合并。
    """
    def _emit_thinking():
        """按用户偏好流式产出思考内容（适配器不支持则跳过）。"""
        if want_thinking and hasattr(adapter, "stream_thinking"):
            for t in adapter.stream_thinking(messages, temperature=temperature, enable_thinking=want_thinking):
                assistant_thinking.append(t)
                yield sse_event("thinking", {"text": t})

    def _emit_text():
        """流式产出正文；`_strip_load_refs` 拦掉模型偶发漏出的 LOAD_REFS 指令行。"""
        for delta in adapter.stream(messages, temperature=temperature, enable_thinking=want_thinking):
            clean = _strip_load_refs(delta)
            if clean:
                assistant_text.append(clean)
                yield sse_event("chunk", {"text": clean})

    # ---- Pass1：非流式，探询是否需要加载参考 ----
    first_text = ""
    if hasattr(adapter, "chat"):
        try:
            first_text = adapter.chat(messages, temperature=temperature, enable_thinking=want_thinking)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{tag}] Pass1 失败，降级单次流式: {e}")
            first_text = ""

    ref_selector = get_reference_selector()
    selected_ids = ref_selector.select(first_text) if first_text else None
    setting_ids = ref_svc.parse_load_setting(first_text) if first_text else None

    if selected_ids or setting_ids:
        load_ref_ids = list(dict.fromkeys(selected_ids)) if selected_ids else []
        load_set_ids = list(dict.fromkeys(setting_ids)) if setting_ids else []
        # 自建 session 取参考/设定正文（铁律：流式生成器内不得用请求级 session）
        load_db = _db.SessionLocal()
        try:
            ref_blocks = ref_svc.fetch_refs_by_ids(load_db, load_ref_ids) if load_ref_ids else []
            set_blocks = ref_svc.fetch_settings_by_ids(load_db, load_set_ids) if load_set_ids else []
        finally:
            load_db.close()

        if ref_blocks or set_blocks:
            loaded_parts = []
            for fn, txt in ref_blocks:
                loaded_parts.append(f"【参考资料：{fn}】\n{txt}")
            for nm, det in set_blocks:
                loaded_parts.append(f"【设定库详情：{nm}】\n{det}")
            messages.append({
                "role": "user",
                "content": f"已按你的请求加载以下资料/设定详情，请据此作答：\n\n" + "\n\n".join(loaded_parts),
            })
            yield sse_event("refs", {
                "loaded": [fn for fn, _ in ref_blocks] + [nm for nm, _ in set_blocks],
                "ids": load_ref_ids + load_set_ids,
            })
            yield from _emit_thinking()
            yield from _emit_text()
            return {"load_ref_ids": load_ref_ids, "load_setting_ids": load_set_ids,
                    "ref_loaded": bool(ref_blocks), "setting_loaded": bool(set_blocks)}
        # 选中 id 全部无效：提示后直接作答，避免无限循环
        messages.append({"role": "user", "content": "你请求的参考资料/设定未能加载（id 无效），请直接作答。"})
        yield from _emit_thinking()
        yield from _emit_text()
        return {"load_ref_ids": load_ref_ids, "load_setting_ids": load_set_ids}

    if first_text:
        # 模型判断无需参考：Pass1 即最终回答，短路省一次调用
        clean = _strip_load_refs(first_text)
        if clean:
            assistant_text.append(clean)
            yield sse_event("chunk", {"text": clean})
        return {"short_circuited": True}

    # adapter 无 chat（Pass1 不可用）：退回单次流式（旧行为）
    yield from _emit_thinking()
    yield from _emit_text()
    return {"pass1_failed": True}


@router.get("/projects/{project_id}/discussion")
def get_discussion(
    project_id: str,
    chapter_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    db: Session = Depends(get_session),
):
    """返回当前商讨缓存（已持久化、未归档）消息列表。

    线程优先级：conversation_id > chapter_id > 小说级默认线程。
    """
    return ok([_to_frontend(m) for m in list_messages(db, project_id, chapter_id=chapter_id, conversation_id=conversation_id)])


@router.post("/projects/{project_id}/discussion/messages")
def append_message(
    project_id: str,
    body: DiscussionMessageCreate,
    chapter_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    db: Session = Depends(get_session),
):
    """手动追加一条商讨消息（闲聊、不调模型）。"""
    m = add_message(
        db,
        project_id,
        body.role,
        body.content,
        meta=body.meta,
        chapter_id=chapter_id,
        conversation_id=conversation_id,
    )
    return ok(_to_frontend(m))


@router.delete("/projects/{project_id}/discussion")
def clear_discussion(
    project_id: str,
    chapter_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    db: Session = Depends(get_session),
):
    """清空当前商讨缓存。线程优先级：conversation_id > chapter_id > 小说级默认线程。"""
    n = clear_messages(db, project_id, chapter_id=chapter_id, conversation_id=conversation_id)
    return ok({"cleared": project_id, "count": n})


@router.post("/projects/{project_id}/discussion/archive")
def archive_discussion(
    project_id: str,
    chapter_id: str,
    conversation_id: Optional[str] = None,
    db: Session = Depends(get_session),
):
    """将当前商讨草稿归档为指定章节备注，并移出当前缓存。"""
    chapter = db.query(ChapterORM).filter_by(id=chapter_id, project_id=project_id).first()
    if chapter is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    result = archive_to_chapter(db, project_id, chapter_id, conversation_id=conversation_id)
    return ok(result)


@router.post("/projects/{project_id}/discussion/chat")
def chat(
    project_id: str,
    body: DiscussionChatRequest,
    chapter_id: Optional[str] = None,
    db: Session = Depends(get_session),
):
    """剧情商讨：用默认模型流式回复用户（SSE）。结束后把本轮对话落库。

    线程归属：conversation_id 给定 → 归属该会话线程；否则 chapter_id 给定 → 归属章线程；
    否则归属小说级默认线程。
    """
    default = _resolve_model(db, body.model_id)
    # _resolve_model 已保证返回的一定是 active（或 None），故 use_model 等价于「取到模型没」
    use_model = default is not None

    # 在请求级 session 仍打开时，把 ModelConfigORM 的标量字段提取为普通 dict。
    # 流式生成器在线程池运行，届时请求 session 已关闭，直接访问 default.* 会触发
    # DetachedInstanceError 并使 StreamingResponse 断流（BodyStreamBuffer was aborted）。
    model_cfg: dict | None = None
    if default is not None:
        model_cfg = {
            "id": default.id,
            "vendor": default.vendor,
            "api_base": default.api_base,
            "api_key": default.api_key,
            "model_name": default.model_name,
            "temperature": default.temperature,
            "top_p": default.top_p,
            "max_tokens": default.max_tokens,
            "enable_thinking": default.enable_thinking,
        }

    # ▼▼▼ 请求日志（仅无内容元信息）▼▼▼
    # ⚠️ 曾逐条打印 messages 正文前 160 字 —— 用户输入/作品内容进服务端日志，
    # 属隐私泄漏，已移除（问题 1.1）。排查只保留「条数 / 角色序列」这类无内容信息。
    logger.info("=" * 60)
    logger.info(f"[discussion/chat] 收到请求 project_id={project_id} chapter_id={chapter_id}")
    logger.info(f"  请求体 model_id={body.model_id} enable_thinking={body.enable_thinking} conversation_id={body.conversation_id}")
    logger.info(f"  消息条数={len(body.messages)} 角色序列={[ (m or {}).get('role', '?') for m in body.messages ]}")
    logger.info(f"  use_model={use_model} default_model={default.model_name if default else None}")
    logger.info("=" * 60)
    # ▲▲▲ 请求日志结束 ▲▲▲

    # 取最近一条 user 消息，用于持久化
    last_user_content = ""
    for m in reversed(body.messages):
        if (m or {}).get("role") == "user":
            last_user_content = (m or {}).get("content", "") or ""
            break

    # 用户消息立即落库（不等流结束），防止中途退出时消息丢失
    if last_user_content:
        try:
            conv_id = body.conversation_id
            add_message(db, project_id, "user", last_user_content, chapter_id=chapter_id, conversation_id=conv_id)
        except Exception as e:
            # 落库失败不阻断流式生成，但留痕：这正是"重进后消息消失"的根因所在（Phase 3.5）
            logger.warning(f"[discussion] 用户消息提前落库失败（可能导致重进后消息消失）: {type(e).__name__}: {e}")

    def event_stream():
        if not use_model:
            yield sse_event("chunk", {"text": "[未配置可用模型，请在「模型配置」中添加并设为默认]"})
            yield sse_event("done", {})
            return

        # 流式生成器在线程池运行，请求级 session 已关闭。自建独立 session 供内部所有
        # DB 操作使用，避免 DetachedInstanceError 导致流中断。
        # 注意：跨模块直接 import SessionLocal 会在导入时绑定到 None（get_engine 在启动时
        # 才赋值），必须用模块引用 _db.SessionLocal() 在运行时取最新值。
        gen_db = _db.SessionLocal()
        config = {
            "api_base": model_cfg["api_base"],
            "api_key": model_cfg["api_key"],
            "model_name": model_cfg["model_name"],
            "temperature": model_cfg["temperature"],
            "top_p": model_cfg["top_p"],
            "max_tokens": model_cfg["max_tokens"],
            "enable_thinking": model_cfg["enable_thinking"] if body.enable_thinking is None else body.enable_thinking,
        }
        # 上下文引擎：把世界观/角色/势力/伏笔/记忆注入商讨，
        # 顾问才能回答「张三现在什么境界」而不是现编。
        # SKILL 注入已在 build_discussion_system 内按 priority + 分类互斥完成。
        # 取最新一条 user 消息作为设定库相关性检索的 query，让 verbose 描述按需展开。
        _latest_user = ""
        for _m in reversed(body.messages or []):
            if (_m or {}).get("role") == "user":
                _latest_user = (_m or {}).get("content", "") or ""
                break
        try:
            sys_prompt, ctx_meta = build_discussion_system(
                gen_db, project_id, chapter_id=chapter_id, query_text=_latest_user
            )
            yield sse_event("context", ctx_meta)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[discussion/chat] 上下文组装失败，降级: {e}")
            sys_prompt = _SYS_PROMPT + _collect_skill_blocks(gen_db)

        # 按需加载目录：把「可用参考文件清单」作为稳定前缀挂到 system（配合 Ollama cache_prompt 缓存，
        # 后续轮次前缀 KV 复用）。AI 仅在需要时通过 LOAD_REFS:<ids> 请求加载具体正文，避免全量塞爆窗口。
        try:
            # 目录按「篇」维度过滤；章线程需先从章反查所属篇（孤儿章则不过滤）
            article_id = None
            if chapter_id:
                ch = gen_db.query(ChapterORM).filter_by(id=chapter_id).first()
                if ch:
                    article_id = ch.article_id
            catalog = ref_svc.build_catalog(gen_db, project_id, article_id=article_id, include_global=True)
            catalog_text = ref_svc.format_catalog_prompt(catalog, include_global=True)
            if catalog_text:
                sys_prompt = (
                    sys_prompt
                    + catalog_text
                    + "\n\n## 参考加载协议\n"
                    "当你判断回答需要用到上面「可用参考文件目录」或【设定库目录】中的资料时，"
                    "**先只输出一行**加载指令然后停止，我会把对应正文注入后再请你继续回答。\n"
                    "支持一次请求多个文件/设定，用逗号分隔：\n"
                    "  LOAD_REFS:<id1>,<id2>  或  LOAD_SETTING:<id1>,<id2>\n"
                    "【重要】如果你的回答需要多份资料联动（如同时涉及官制+俸禄+货币），"
                    "必须把所有相关 id 写在同一行一次性请求，不要只请求一个——其余信息你无法准确猜测。\n"
                    "若不需要任何参考资料或设定，直接正常回答即可。\n"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[discussion/chat] 目录构造失败，跳过: {e}")

        messages = [{"role": "system", "content": sys_prompt}]
        for m in body.messages:
            content = (m or {}).get("content", "")
            if content:
                role = (m or {}).get("role", "user")
                messages.append({"role": role, "content": content})

        want_thinking = body.enable_thinking if body.enable_thinking is not None else model_cfg["enable_thinking"]
        temperature = body.temperature or model_cfg["temperature"]

        # ---- P0 观测：记录本轮实际加载了哪些设定/参考（不阻塞主流程）----
        obs = {
            "question": _latest_user,
            "load_ref_ids": [],
            "load_setting_ids": [],
            "ref_loaded": False,
            "setting_loaded": False,
            "short_circuited": False,
            "pass1_failed": False,
        }

        assistant_text: list[str] = []
        assistant_thinking: list[str] = []
        # adapter 可能创建失败（vendor 配置错等）→ finally 里的用量计量要引用它，先置 None
        adapter = None
        _t0 = time.monotonic()
        try:
            adapter = get_adapter(model_cfg["vendor"], config)
            logger.info(f'[discussion] 模型={model_cfg["model_name"]} ({model_cfg["vendor"]}) | api_base={(model_cfg["api_base"] or "")[:50]} | thinking={want_thinking}')

            # ---- 按需参考加载（两阶段）---- 详见 _stream_two_phase 文档
            obs.update((yield from _stream_two_phase(
                adapter, messages,
                want_thinking=want_thinking, temperature=temperature,
                tag="discussion/chat",
                assistant_text=assistant_text, assistant_thinking=assistant_thinking,
            )))
        except Exception as e:  # noqa: BLE001
            # 流式失败前端只见短语，服务端必须留堆栈（Phase 3.5）
            logger.exception(f"[discussion] 项目商讨流式失败 project={str(project_id)[:8]}")
            yield sse_event("chunk", {"text": f"[模型调用失败：{str(e)[:200]}]"})
        finally:
            yield sse_event("done", {})
            # 持久化 AI 回复（用户消息已在流开始前落库）
            try:
                full = "".join(assistant_text)
                conv_id = body.conversation_id
                if full:
                    add_message(
                        gen_db,
                        project_id,
                        "assistant",
                        full,
                        thinking="".join(assistant_thinking) or None,
                        meta={"enable_thinking": bool(want_thinking)},
                        chapter_id=chapter_id,
                        conversation_id=conv_id,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[discussion/chat] 持久化失败: {e}")
            # 抽取对话中建议的新实体，推送给前端供作者确认写入资料库
            # 复用用户本轮对话选择的模型（default 即 _resolve_model 解析结果），不固定 Ollama/默认
            try:
                suggestion = _build_entity_suggestion(
                    gen_db, project_id, last_user_content, "".join(assistant_text),
                    model_id=model_cfg["id"] if model_cfg else None,
                )
                if suggestion:
                    yield sse_event("entity_suggestion", suggestion)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[discussion/chat] 实体建议抽取失败: {e}")
            # P0 观测落库（记录本轮设定/参考加载情况，失败静默）
            try:
                load_obs.record(
                    gen_db,
                    project_id=project_id,
                    conversation_id=body.conversation_id,
                    chapter_id=chapter_id,
                    model_id=model_cfg["id"] if model_cfg else None,
                    vendor=model_cfg["vendor"] if model_cfg else None,
                    **obs,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[discussion/chat] 观测落库失败: {e}")
            # ── 用量计量（08-B5）：商讨回合此前不记账 → 观测页统计偏低。Pass1+Pass2 合记一条 ──
            # last_usage 取值优先级：Pass2 流式若厂商回传则是 Pass2 的；否则是 Pass1 非流式的真实值；
            # 两者都没有 → record_usage 按字符估算（thinking 也计入 completion —— 思考同样烧 token）。
            try:
                if adapter is not None:
                    usage_crud.record_usage(
                        gen_db,
                        scene="discussion",
                        vendor=model_cfg.get("vendor"),
                        model_name=model_cfg.get("model_name"),
                        usage=getattr(adapter, "last_usage", None),
                        project_id=project_id,
                        duration_ms=int((time.monotonic() - _t0) * 1000),
                        ok=True,
                        prompt_text="".join(str(m.get("content") or "") for m in messages),
                        completion_text="".join(assistant_text) + "".join(assistant_thinking),
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[discussion/chat] 用量记录失败: {e}")
            # 关闭流式生成器自建的 session（finally 保证正常/异常都释放）
            gen_db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ===========================================================================
# 全局对话（无需选择小说，类似豆包/ChatGPT 通用助手模式）
# ===========================================================================

# 全局对话携带的历史上文字符预算。system prompt 约 6k 字符，
# 叠加后需留足生成空间，控制在 Ollama 适配器 num_ctx=16384 之内。
_GLOBAL_HISTORY_CHAR_BUDGET = 8000

_GLOBAL_SYS = (
    "你是一名专业的小说创作助手（全局模式）。\n"
    "你可以回答关于写作技巧、世界观设定、角色设计、古代官制、修仙体系等任何问题。\n"
    "当问题涉及特定世界观（如官制、境界体系）时，若你已通过 LOAD_SETTING 加载了对应体系的完整说明，"
    "回答必须以其数据为准，不得使用训练数据中的其他朝代或通用知识替代；"
    "若尚未加载，请先输出 LOAD_SETTING 加载后再答（见下方【设定库目录】）。\n"
    "【回答方式】\n"
    "1. 事实查询（官制/境界/数值/名称）：直接给答案，不超过150字，结论一句话+一句依据，"
    "不要展开无关体系。\n"
    "2. 征询意见（含「怎么」「怎么办」「如何」等信号）：给 2~3 个方向，每条不超过80字，标出代价。\n"
    "3. 单次回复不超过300字；超出立即收尾。\n"
    "4. 【禁止暴露内部标记】不要在回复正文里写出 `id=xxx`、`#标签`、文档文件名，"
    "也不要把 `LOAD_REFS`/`LOAD_SETTING` 字样写进给作者的正文。\n"
    "   ※ 豁免：上述第4条仅约束最终正文；在正式作答前的第一轮若需加载资料，"
    "仍必须输出 `LOAD_SETTING:<id>` / `LOAD_REFS:<id>` 协议指令（拉取资料的必需指令，"
    "不受第4条限制，用户也不会看到该指令本身）。\n"
    "5. 作者问「上一句是什么」时，只依据真实对话历史回答；技能示例中的不算真实对话。"
    # [MARK: DOC-ID-LOAD-SAFETY] 第4条禁令必须豁免 LOAD_REFS/LOAD_SETTING 协议指令，
    # 否则模型不敢发指令→两阶段资料加载失效。若优化后 AI 不再加载设定/参考，先查这里。
)


def _build_global_system(db: Session) -> str:
    """构建全局对话的 system prompt（仅含全局设定库目录 + 全局 SKILL，不含任何小说数据）。

    B 方案：设定库走「目录 + 按需加载」（build_setting_catalog），
    global 项目无 setting_ids 过滤 → 全部设定体系展示在目录中，详情按 LOAD_SETTING 拉取，
    不再把全部设定描述常驻注入，避免撑爆全局对话的 system 窗口。
    """
    parts = [_GLOBAL_SYS]

    # 注入全局设定库目录（SettingORM 无 project_id，天然全局；
    # GLOBAL_PROJECT_ID 通常无 setting_ids → 全部展示）
    try:
        setting_catalog = layers.build_setting_catalog(db, GLOBAL_PROJECT_ID)
        if setting_catalog:
            parts.append(setting_catalog)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[discussion/global-chat] 设定目录构造失败，跳过: {e}")

    # 注入全局 SKILL
    skill_block = _collect_skill_blocks(db)
    if skill_block:
        parts.append(skill_block)

    return "\n\n".join(parts)


@router.post("/discussion/global-chat")
def global_chat(
    body: DiscussionChatRequest,
    db: Session = Depends(get_session),
):
    """全局对话：不依赖任何小说，使用全局设定库+SKILL 作为上下文。

    适用场景：用户未选择小说时的通用问答（如询问官制、境界体系等）。
    消息持久化到 project_id="__global__" 的默认线程，刷新/重进后仍保留记忆。
    """
    default = _resolve_model(db, body.model_id)
    # _resolve_model 已保证返回的一定是 active（或 None），故 use_model 等价于「取到模型没」
    use_model = default is not None

    # 在请求级 session 仍打开时，把 ModelConfigORM 的标量字段提取为普通 dict，
    # 避免流式生成器（线程池运行）访问已关闭 session 触发 DetachedInstanceError。
    model_cfg: dict | None = None
    if default is not None:
        model_cfg = {
            "id": default.id,
            "vendor": default.vendor,
            "api_base": default.api_base,
            "api_key": default.api_key,
            "model_name": default.model_name,
            "temperature": default.temperature,
            "top_p": default.top_p,
            "max_tokens": default.max_tokens,
            "enable_thinking": default.enable_thinking,
        }

    last_user_content = ""
    for m in reversed(body.messages):
        if (m or {}).get("role") == "user":
            last_user_content = (m or {}).get("content", "") or ""
            break

    # 【关键】用户消息立即落库（不等流结束）。
    # 原因：流式回复可能耗时较长，若用户中途退出/刷新页面，前端内存消息丢失，
    # 而 finally 中的持久化还未执行 → 重进后消息消失。
    # 提前写入后，loadDiscussion 能立刻拉到这条消息。
    if last_user_content:
        try:
            add_message(db, GLOBAL_PROJECT_ID, "user", last_user_content, conversation_id=body.conversation_id)
        except Exception as e:
            # 持久化失败不阻断主流程，但留痕：这正是"重进后消息消失"的根因所在（Phase 3.5）
            logger.warning(f"[discussion] 全局对话用户消息提前落库失败（可能导致重进后消息消失）: {type(e).__name__}: {e}")

    def event_stream():
        if not use_model:
            yield sse_event("chunk", {"text": "[未配置可用模型，请在「模型配置」中添加并设为默认]"})
            yield sse_event("done", {})
            return

        # 流式生成器在线程池运行，请求级 session 已关闭。自建独立 session 供内部所有
        # DB 操作使用，避免 DetachedInstanceError 导致流中断。
        # 注意：跨模块直接 import SessionLocal 会在导入时绑定到 None（get_engine 在启动时
        # 才赋值），必须用模块引用 _db.SessionLocal() 在运行时取最新值。
        gen_db = _db.SessionLocal()
        config = {
            "api_base": model_cfg["api_base"],
            "api_key": model_cfg["api_key"],
            "model_name": model_cfg["model_name"],
            "temperature": model_cfg["temperature"],
            "top_p": model_cfg["top_p"],
            "max_tokens": model_cfg["max_tokens"],
            "enable_thinking": model_cfg["enable_thinking"] if body.enable_thinking is None else body.enable_thinking,
        }

        try:
            sys_prompt = _build_global_system(gen_db)
            ctx_meta = {"mode": "global", "global_chat": True}
            yield sse_event("context", ctx_meta)
        except Exception as e:
            logger.warning(f"[discussion/global-chat] 上下文组装失败，降级: {e}")
            sys_prompt = _SYS_PROMPT + _collect_skill_blocks(db)

        # 全局参考文档目录（可选注入）
        try:
            catalog = ref_svc.build_catalog(gen_db, GLOBAL_PROJECT_ID, include_global=True)
            catalog_text = ref_svc.format_catalog_prompt(catalog, include_global=True)
            if catalog_text:
                sys_prompt = (
                    sys_prompt
                    + catalog_text
                    + "\n\n## 参考加载协议\n"
                    "当你判断需要用到上面「可用参考文件目录」或【设定库目录】中的资料时，"
                    "**先只输出一行**加载指令然后停止，我会把对应正文注入后再让你继续回答。\n"
                    "支持一次请求多个文件/设定，用逗号分隔：\n"
                    "  LOAD_REFS:<id1>,<id2>  或  LOAD_SETTING:<id1>,<id2>\n"
                    "【重要】如果你的回答需要多份资料联动（如同时涉及官制+俸禄+货币），"
                    "必须把所有相关 id 写在同一行一次性请求，不要只请求一个——其余信息你无法准确猜测。\n"
                    "若不需要任何参考资料或设定，直接正常回答即可。\n"
                )
        except Exception as e:
            logger.warning(f"[discussion/global-chat] 目录构造失败，跳过: {e}")

        # 历史裁剪：system prompt 已占约 6k 字符。全局对话现在有记忆（落库后每轮都会
        # 带上完整历史），若不限长，连续对话很快顶爆模型上下文窗口 —— 而 Ollama 超窗
        # 是「从最老 token 静默丢弃」，最先被丢的正是 system 里的世界观设定，
        # 表现为「聊几轮之后 AI 突然不认识官制了」。故只保留预算内的最近若干轮。
        kept: list[dict] = []
        used = 0
        for m in reversed(body.messages or []):
            content = (m or {}).get("content", "")
            if not content:
                continue
            if kept and used + len(content) > _GLOBAL_HISTORY_CHAR_BUDGET:
                break
            kept.append({"role": (m or {}).get("role", "user"), "content": content})
            used += len(content)
        kept.reverse()

        messages = [{"role": "system", "content": sys_prompt}] + kept

        want_thinking = body.enable_thinking if body.enable_thinking is not None else model_cfg["enable_thinking"]
        temperature = body.temperature or model_cfg["temperature"]

        # ---- P0 观测：记录本轮实际加载了哪些设定/参考（不阻塞主流程）----
        obs = {
            "question": last_user_content,
            "load_ref_ids": [],
            "load_setting_ids": [],
            "ref_loaded": False,
            "setting_loaded": False,
            "short_circuited": False,
            "pass1_failed": False,
        }

        assistant_text: list[str] = []
        assistant_thinking: list[str] = []
        # adapter 可能创建失败 → finally 里的用量计量要引用它，先置 None
        adapter = None
        _t0 = time.monotonic()
        try:
            adapter = get_adapter(model_cfg["vendor"], config)
            logger.info(f'[discussion] 模型={model_cfg["model_name"]} ({model_cfg["vendor"]}) | api_base={(model_cfg["api_base"] or "")[:50]} | thinking={want_thinking}')

            # ---- 按需参考加载（两阶段）---- 详见 _stream_two_phase 文档
            obs.update((yield from _stream_two_phase(
                adapter, messages,
                want_thinking=want_thinking, temperature=temperature,
                tag="discussion/global-chat",
                assistant_text=assistant_text, assistant_thinking=assistant_thinking,
            )))
        except Exception as e:  # noqa: BLE001
            logger.exception("[discussion] 全局对话流式失败")
            yield sse_event("chunk", {"text": f"[模型调用失败：{str(e)[:200]}]"})
        finally:
            yield sse_event("done", {})
            # 持久化 AI 回复到全局线程（用户消息已在流开始前落库）。
            try:
                full = "".join(assistant_text)
                if full:
                    add_message(
                        gen_db,
                        GLOBAL_PROJECT_ID,
                        "assistant",
                        full,
                        thinking="".join(assistant_thinking) or None,
                        meta={"enable_thinking": bool(want_thinking), "global_chat": True},
                        conversation_id=body.conversation_id,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[discussion/global-chat] 持久化失败: {e}")
            # P0 观测落库（记录本轮设定/参考加载情况，失败静默）
            try:
                load_obs.record(
                    gen_db,
                    project_id=GLOBAL_PROJECT_ID,
                    model_id=model_cfg["id"] if model_cfg else None,
                    vendor=model_cfg["vendor"] if model_cfg else None,
                    **obs,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[discussion/global-chat] 观测落库失败: {e}")
            # ── 用量计量（08-B5）：全局对话同商讨（project_id = __global__，见 04-C12 豁免语义）──
            try:
                if adapter is not None:
                    usage_crud.record_usage(
                        gen_db,
                        scene="discussion",
                        vendor=(model_cfg or {}).get("vendor"),
                        model_name=(model_cfg or {}).get("model_name"),
                        usage=getattr(adapter, "last_usage", None),
                        project_id=GLOBAL_PROJECT_ID,
                        duration_ms=int((time.monotonic() - _t0) * 1000),
                        ok=True,
                        prompt_text="".join(str(m.get("content") or "") for m in messages),
                        completion_text="".join(assistant_text) + "".join(assistant_thinking),
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[discussion/global-chat] 用量记录失败: {e}")
            # 关闭流式生成器自建的 session（finally 保证正常/异常都释放）
            gen_db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/discussion/load-logs")
def list_load_logs(
    project_id: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_session),
):
    """P0 观测查询：返回设定/参考加载日志（可按小说过滤，默认最近 200 条）。

    数据来自 discussion_load_logs 表：每轮商讨记录模型请求了哪些 LOAD_SETTING/LOAD_REFS、
    是否真的注入、是否短路。后续做关联边/BM25 阈值/频率预载都以此为准。
    """
    return ok(load_obs.list_logs(db, project_id=project_id, limit=limit))
