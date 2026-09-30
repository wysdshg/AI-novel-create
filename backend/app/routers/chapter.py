"""模块2/3：章节生成控制器 + 剧情商讨缓存（需求 2、3、6）。

generate 接口：若已配置默认模型，则经 gateway 适配器流式生成真实正文（SSE chunk 事件），
失败或无可用的模型时优雅降级为占位文本，保证前端流程不中断。生成完成后落库 chapters。
4 级结构下：chapters 必隶属 article_id；列出时按 article 过滤。

SSE 事件序列：
  start → context（这次读了哪些资料）→ chunk* → validate（AI 味检测）
  → saved（章节已落库）→ ingest（记忆抽取 + 走向建议）→ done
"""
import logging
import os
import re
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.schemas.chapter import GenerateRequest, ChapterCreate, ChapterUpdate, PolishRequest
from app.core.response import ok, sse_event
from app.core.database import get_session
from app.core import database as _db
from app.core.context import build_chapter_messages
from app.services import (
    app_config, article_crud, chapter_crud, eval_crud, feedback_crud, humanizer,
    ingestion, model_crud, reference_crud, usage_crud,
)
from app.core.gateway.registry import get_adapter
from app.models.orm import ReferenceDocORM, ChapterORM

router = APIRouter(tags=["章节生成"])


# ──────────────────────────────────────────────
# 重复循环检测器（防小模型长文本复读）
# ──────────────────────────────────────────────
logger = logging.getLogger(__name__)


class _RepetitionGuard:
    """流式重复循环检测。

    小模型（8B 及以下）生成长文时容易进入「段落级死循环」——同一段落反复出现 3~5 遍。
    本守护在流式输出过程中实时监控，当检测到新内容与近期内容高度重叠时：
    - 停止向下游 yield 新 chunk（前端不再显示重复内容）
    - 标记 full 文本需要后处理去重

    算法：
      1. 维护滑动窗口（最近 _WINDOW_SIZE 字符）。
      2. 每次追加新 chunk 后，用最长公共子串检查重叠率。
      3. 重叠率 > _THRESHOLD 且重叠长度 > _MIN_OVERLAP_LEN 时判定为循环。
      4. 触发后停止 yield，但继续消费迭代器直到模型 done（避免连接泄漏）。
    """

    _WINDOW_SIZE = 600       # 滑动窗口大小（字符）
    _THRESHOLD = 0.55        # 近窗重叠率阈值
    _MIN_OVERLAP_LEN = 40    # 最小重叠长度（短巧合不算）
    _SENT_MIN_LEN = 12       # 句子级检测：短于该长度不计数（短句是风格口癖，不算复读）
    _SENT_MAX_COUNT = 3      # 同一完整句出现 3 次 → 判定循环（按用户实测反馈回调到 3）

    def __init__(self):
        self._buf = ""               # 已累积的全部正文
        self._triggered = False      # 是否已触发循环截断
        self._pending = ""           # 跨 chunk 拼接的未收尾句子
        self._sent_count = {}        # 完整句子 → 出现次数（句子级复读检测）

    def feed(self, chunk: str) -> bool:
        """传入新 chunk，返回是否应该继续 yield 给下游。

        返回 True = 正常，可以 yield；
        返回 False = 检测到循环，调用方必须立即中断上游生成（不能继续消费迭代器烧 token）。
        """
        if self._triggered:
            return False

        if not chunk:
            return True

        self._buf += chunk

        # —— 第一层：近窗重叠检测（小模型「整段复读」：新内容与近期窗口高度重叠）——
        if len(self._buf) >= self._WINDOW_SIZE:
            recent = self._buf[-self._WINDOW_SIZE:]
            overlap_len = self._longest_overlap(recent)
            if overlap_len >= self._MIN_OVERLAP_LEN:
                overlap_ratio = overlap_len / len(chunk) if chunk else 0
                if overlap_ratio >= self._THRESHOLD:
                    self._triggered = True
                    return False

        # —— 第二层：句子级重复检测（GLM 等大模型的「对话句复读」——
        # 同一句话变着花样重复，近窗重叠率低于阈值，第一层抓不到；
        # 同一完整句（≥18 字）出现 3 次即判定循环）——
        # 用 findall 提取「以句末标点结尾 + 尾随引号」的完整句，避免引号被留到下一段
        # 导致同一句在不同 chunk 里 key 不一致（"……旧案。" vs "”……旧案。"）。
        self._pending += chunk
        sentences = re.findall(r"[^。！？…]*[。！？…][”’」』]*", self._pending)
        if sentences:
            self._pending = self._pending[len("".join(sentences)):]
            for s in sentences:
                s = s.strip()
                if len(s) >= self._SENT_MIN_LEN:
                    self._sent_count[s] = self._sent_count.get(s, 0) + 1
                    if self._sent_count[s] >= self._SENT_MAX_COUNT:
                        self._triggered = True
                        return False

        return True

    @staticmethod
    def _longest_overlap(text: str) -> int:
        """计算 text 后缀与 text 前缀的最长公共子串长度。

        用于检测「刚生成的内容」是否与「不久前生成的内容」高度相似。
        简化实现：从短到长枚举后缀/前缀匹配。
        """
        n = len(text)
        best = 0
        # 只检查后 1/3 与前 2/3 的重叠（减少计算量）
        split = n * 2 // 3
        suffix = text[split:]
        prefix = text[:split]
        for length in range(min(len(suffix), len(prefix)), 0, -1):
            if suffix[-length:] == prefix[:length]:
                best = length
                break
        return best

    @property
    def full_text(self) -> str:
        return self._buf

    @property
    def is_triggered(self) -> bool:
        return self._triggered


def _dedup_trailing_repeats(text: str) -> str:
    """后处理：剪掉尾部**连续复读**段落（流式检测的兜底）。

    2026-09-09 重写。旧实现（单段字符集 Jaccard>0.8 即从此段全裁）在中文
    短段落风格下误杀率极高——实测 7 字的「沈砚看向水洼。」与 75 段之前的
    「沈砚看水洼。」Jaccard=0.857，导致后面 97 段（1482 字符）正常剧情被
    全部裁掉（超短段字符集差一个字相似度就爆表）。
    新规则（三条同时满足才裁）：
      1. 只检查长段（≥20 字）：短句呼应/口头禅是正常写作手法，不参与判定；
      2. 相似度用 difflib（内容敏感），阈值 0.8；字符集 Jaccard 仅作 0.3 以下的快速预筛；
      3. 必须**连续 ≥3 个长段**都与前文相似才判定为复读块（单段相似=呼应，连续相似=复读）。
    """
    paras = re.split(r'\n\n+', text.strip())
    if len(paras) < 6:
        return text

    MIN_LEN, TH, RUN = 20, 0.8, 3
    import difflib

    def _sim(pj: str, cur: str, cs: set) -> bool:
        if len(pj) < MIN_LEN:
            return False
        if len(cs & set(pj)) / len(cs | set(pj)) < 0.3:
            return False  # 用字面都不像，必不相似，跳过昂贵的 difflib
        return difflib.SequenceMatcher(None, cur, pj).ratio() > TH

    def _is_dup(idx: int) -> bool:
        cur = paras[idx].strip()
        if len(cur) < MIN_LEN:
            return False
        cs = set(cur)
        for j in range(idx):
            if _sim(paras[j].strip(), cur, cs):
                return True
        # 或与紧邻后一段相似：复读块的「块首」前面是正常文本，
        # 只向前文比较会让它永远判 False，导致正好 RUN 段的复读块裁不掉
        # （2026-09-09 单测骨架暴露：repeat×3 紧跟正文时 run 只能数到 2）。
        if idx + 1 < len(paras) and _sim(paras[idx + 1].strip(), cur, cs):
            return True
        return False

    # 从末尾向前找「连续 RUN 个长段 dup」的块；短段中性（不计入也不打断）。
    run, cut_start = 0, -1
    for i in range(len(paras) - 1, -1, -1):
        if len(paras[i].strip()) < MIN_LEN:
            continue
        if _is_dup(i):
            run += 1
            if run >= RUN:
                cut_start = i
        else:
            if run >= RUN:
                break
            run = 0

    if cut_start < 0:
        return text
    return '\n\n'.join(paras[:cut_start])


def _similarity(a: str, b: str) -> float:
    """简单的字符级 Jaccard 相似度（用于段落去重判断）。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def _can_persist(full: str, error_notes: list[str] | None = None) -> tuple[bool, str]:
    """本轮生成是否可以落库。返回 (可落库, 不可落库时的原因)。

    铁律（问题 1.2）：**错误/占位提示绝不能变成小说正文**。
    - 有真实正文 → 可落库（错误提示若同时存在也不影响，因为它压根不在 full 里）；
    - 正文为空 → 不可落库，返回原因供前端提示。此时若走更新路径，
      会把已有章节正文覆盖成空/错误文案 —— 静默数据丢失，必须拦住。
    """
    if (full or "").strip():
        return True, ""
    reason = "；".join(n for n in (error_notes or []) if n).strip()
    return False, reason or "模型未返回任何正文"


def _content_only_stream(stream):
    """把纯字符串流包装成 (kind, text) 元组流（kind 恒为 "content"）。

    用 yield from 委托而非普通 for 循环：这样外层生成器 close() 时，
    GeneratorExit 会确定性传播到上游 stream 的 with 块，HTTP 连接被立即关闭。
    """
    yield from (("content", d) for d in stream)


def _next_chapter_no(db, project_id: str, article_id: str) -> int:
    """本篇内下一个章节序号 = 该篇已有最大章号 + 1（而非小说全局递增）。

    修复问题6：原来用 currentNovel.chapterCount+1（全局），导致序号跨篇混乱、
    且每次生成都让全局计数 +1。这里改为按 article_id 局部计算，新建章才 +1。
    """
    try:
        rows = (
            db.query(ChapterORM.chapter_no)
            .filter_by(project_id=project_id, article_id=article_id)
            .all()
        )
        return (max((r[0] for r in rows if r[0] is not None), default=0)) + 1
    except Exception as e:  # noqa: BLE001
        # 算出下一章号失败 → 兜底 1（保证仍能生成，不至于卡住用户）。
        # 留痕：若库里有章却总从 1 开始，说明这里一直在异常（Phase 3.5）
        logger.warning(f"[chapter] 计算下一章号失败，回退为 1: {type(e).__name__}: {e}")
        return 1


def _maybe_load_refs(db, project_id, messages, ctx_meta, default, body) -> tuple[list, dict]:
    """hybrid 模式：在 pick_relevant 已注入的基线上，按需把最相关的全局参考拉进来。

    设计取舍（章节生成路径）：
    - 不采用「AI 两阶段 LOAD_REFS 选择」，改为**确定性相关性 top-up**：
      复用 pick_relevant 同款 score_reference 对全局池打分，只注入 top-2 且分数>0 的全局资料。
      按需、有界、零额外延迟、可解释；
    - pick_relevant 永远是安全底线（项目内相关参考必在），本函数只补「项目没有但全局相关的」；
    - 任何异常降级为「不额外加载」，不打断生成。

    🔴 **取舍理由（2026-09-15 更正）**：本注释原写「**本地 4b 模型**在该协议上不稳定，
    时而全不加载、时而一股脑全加载」—— **该前提已失效**（作者早已不用本地 4b，主力为云端模型）。
    **结论不变，理由换成独立成立的这条**：章节生成链路要的是**稳定与可解释，不是自主性**；
    把「取哪些资料」交给模型自由发挥，等于把已验证的确定性行为换成不可控行为
    （同款判断见 docs/03 §2 的 🔴 边界约束：确定性取数一律后端预取，
    FC 只用于「步数不确定的探索型取数」）。
    另：**商讨链路**仍保留 AI 两阶段（`reference_selector.py` 的 A 策略），原型见 docs/04 B9。
    """
    try:
        baseline_ids = {d.get("id") for d in ctx_meta.get("references", [])}
        ids = _global_topup_ids(db, project_id, body.prompt_hint, baseline_ids, top_k=5)  # 放开：2 → 5
        if not ids:
            return messages, ctx_meta

        load_ids = list(dict.fromkeys(ids))
        extra = reference_crud.fetch_refs_by_ids(db, load_ids)
        if not extra:
            return messages, ctx_meta

        extra_text = "\n\n".join(f"【参考资料（按需加载）：{fn}】\n{ct}" for fn, ct in extra)
        messages[1]["content"] += "\n\n" + extra_text
        ctx_meta = {**ctx_meta, "refs_loaded_extra": [fn for fn, _ in extra], "ref_mode": "hybrid"}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[generate_chapter] 按需参考加载失败，降级基线: {type(e).__name__}: {e}")
    return messages, ctx_meta


def _global_topup_ids(db, project_id, hint: str | None, exclude_ids: set[str], top_k: int = 2) -> list[str]:
    """确定性按需 top-up：对全局资料池按相关性打分，返回分数>0 的 top_k 个 id（排除基线）。

    章节生成 hybrid 模式的主路径（不依赖模型选择，稳定可控）：只把最相关的全局资料
    按需拉进上下文，避免把整个全局池一股脑塞爆窗口。相关性用与 pick_relevant 相同的
    score_reference，且要求分数>=4（标签/文件名真实命中），排除弱重叠噪声，保证「按需」名副其实。
    """
    try:
        from app.services.reference_crud import GLOBAL_PROJECT_ID, score_reference
        rows = (
            db.query(ReferenceDocORM)
            .filter_by(project_id=GLOBAL_PROJECT_ID)
            .filter(ReferenceDocORM.article_id.is_(None))
            .all()
        )
        scored = []
        for o in rows:
            if o.id in exclude_ids:
                continue
            s = score_reference(o, hint or "", set())
            # 阈值 >=4：只接受「标签/文件名真实命中」（score_reference 里标签×5、文件名×4），
            # 排除 2-gram 弱重叠产生的 0.x 噪声分，避免无关全局资料被误加载。
            if s >= 4:
                scored.append((s, o.id))
        scored.sort(key=lambda x: -x[0])
        return [oid for _, oid in scored[:top_k]]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[generate_chapter] 全局 top-up 失败: {e}")
        return []


def _background_ingest(project_id: str, chapter_id: str,
                       push_chapter_id: str | None = None,
                       push_conversation_id: str | None = None,
                       ingest_level: str | None = None):
    """后台线程执行写后摄取：独立 DB 会话，绝不碰请求级 session（线程隔离铁律）。

    摄取完成只落库、不推 SSE（流已在 done 后关闭）；失败只打日志，不影响正文。
    push_chapter_id / push_conversation_id：走向建议归位的对话线程（与生成时所在线程一致）。
    ingest_level：写后摄取档位（只传字符串，线程安全）——
      full/None = 全跑（默认，读全局配置）；
      lite      = 跳过概览聚合（省 1 次 LLM，走向/伏笔/记忆全保留）；
      none      = 整段跳过（只验正文时用，省 2 次）。
    """
    try:
        t_db = _db.SessionLocal()
        try:
            ch = t_db.query(ChapterORM).filter_by(id=chapter_id).first()
            if ch is None:
                logger.info(f"[generate_chapter] 后台摄取：章节不存在 id={chapter_id}")
                return
            lvl = (ingest_level or "full").lower()
            do_extract = (lvl != "none")
            do_aggregate = (lvl not in ("none", "lite"))
            res = ingestion.ingest_chapter(
                t_db, project_id, ch,
                push_chapter_id=push_chapter_id,
                push_conversation_id=push_conversation_id,
                extract=do_extract,
                aggregate=do_aggregate,
            )
            t_db.commit()  # 保险：个别 CRUD 未内嵌 commit
            logger.info(
                f"[generate_chapter] 后台摄取完成: level={lvl} memory={res.get('memory_id')} "
                f"fallback={res.get('fallback')}({res.get('fallback_reason') or 'llm'}) "
                f"dirs={res.get('directions_pushed', 0)}"
            )
            # 概览向上聚合（问题1）：写回 Article/Volume/Project.summary，
            # 概览页自动显示，不再「暂无 AI 概览」占位。失败不影响正文。
            if not do_aggregate:
                logger.info("[generate_chapter] 概览聚合已按 ingest_level 跳过")
                return
            try:
                agg = ingestion.aggregate_overview(t_db, project_id, ch.article_id, auto=True)
                logger.info(
                    f"[generate_chapter] 概览聚合: 篇={agg.get('articles')} "
                    f"卷={agg.get('volumes')} 小说={agg.get('project')}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[generate_chapter] 概览聚合失败（不影响正文）: {type(e).__name__}: {str(e)[:150]}")
        finally:
            t_db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[generate_chapter] 后台摄取异常: {type(e).__name__}: {str(e)[:200]}")


@router.post("/projects/{project_id}/chapters/generate")
def generate_chapter(project_id: str, body: GenerateRequest, db: Session = Depends(get_session)):
    """单章生成（流式 SSE）。

    落库后触发写后摄取：抽章级记忆、写篇章摘要、把走向建议推到对话区。
    """
    chapter_no = body.chapter_no
    # 4 级结构约束：章节必须归属某一篇，缺失 article_id 会造成「孤儿章」
    # （不挂在任何篇下、侧栏树不显示）。前端已防呆，此处为后端兜底。
    if not body.article_id:
        raise HTTPException(
            status_code=400,
            detail="生成章节必须指定所属篇（article_id）。请先在左侧选择一篇或一章后再生成。",
        )

    # 上下文引擎：角色/设定/伏笔/记忆/商讨/参考文档/SKILL/去AI味 一次性组装
    volume_id = None
    try:
        art = article_crud.get_article(db, project_id, body.article_id)
        volume_id = getattr(art, "volume_id", None) if art else None
    except Exception as e:  # noqa: BLE001
        # 查篇失败 → volume_id 保持 None（上下文引擎少一层层级提示，不阻断生成）。
        # 留痕，便于区分「篇确实无归属卷」与「查询报错」（Phase 3.5）
        logger.warning(f"[chapter] 取篇的 volume_id 失败，将不带卷级上下文: {type(e).__name__}: {e}")

    # 章节序号：重新生成沿用原章号；新建按「本篇内最大序号 +1」计算（修复问题6）。
    chapter_no = body.chapter_no
    if body.chapter_id:
        existing = chapter_crud.get_chapter(db, project_id, body.chapter_id)
        if existing:
            chapter_no = existing.chapter_no
    elif body.article_id:
        chapter_no = _next_chapter_no(db, project_id, body.article_id)

    try:
        messages, ctx_meta = build_chapter_messages(
            db, project_id,
            chapter_no=chapter_no,
            article_id=body.article_id,
            volume_id=volume_id,
            prompt_hint=body.prompt_hint,
            word_range=body.word_range,
            trigger_foreshadow_ids=body.trigger_foreshadow_ids,
            from_discussion=body.from_discussion,
            chapter_id=body.thread_chapter_id,
            conversation_id=body.thread_conversation_id,
        )
    except Exception as e:  # noqa: BLE001
        # 上下文引擎挂了也得让作者能写字——退回最小提示词
        logger.warning(f"[generate_chapter] 上下文组装失败，降级: {type(e).__name__}: {e}")
        messages = [
            {"role": "system", "content": "你是中文网络小说代笔，只输出本章正文，全中文。"},
            {"role": "user", "content": f"创作第 {chapter_no} 章。要点：{body.prompt_hint or '自行推进剧情'}"},
        ]
        ctx_meta = {"error": str(e)[:200]}

    # 模型选择：统一走 model_crud.resolve_model（Phase 3.4）。
    # ⚠️ 原先这里是 `db.query(ModelConfigORM).filter_by(id=body.model_id).first()`——**绕过了
    # active 校验**，前端若提交一个已停用的 model_id 就会拿它去真实调用并失败；
    # 现在与商讨/辅助共用同一语义（指定且 active 才用，否则回退默认）。
    default = model_crud.resolve_model(db, body.model_id)
    # resolve_model 已保证返回的一定是 active，故 use_model 等价于「有没有取到模型」。
    use_model = default is not None

    # ---- 章节生成 token 上限：2026-09-20 用户拍板「彻底放开」，不再按目标字数换算 ----
    #
    # 【历史】早期按目标字数换算（中文 1 字 ≈ 1.5 token，×1.2+256），上限 8192（约 5300 字）。
    # 【为什么废掉】开思考时 reasoning 与正文**共用同一个 max_tokens 预算**：
    #   3000 字目标 → 3856 上限，思考先吃掉大半 → 正文只剩几百 token，永远写不满。
    #   （2026-09-10 魔搭 GLM-5.3-Flash 已踩过同款：reasoning 吃 1300~2200，
    #    ×1.2+256=3256 档正文被挤到逼近上限 → 末段无标点连排 411 字、字数不足 1464。）
    # 【为什么安全】max_tokens 只是**上限**，实际按真实生成量计费，给满不多花钱；
    #   非思考场景模型会提前 stop；_RepetitionGuard + gen.close() 兜底防失控拖时。
    #   32768 档已在魔搭 GLM 系实测可行（finish=stop 正常）。可用 NA_CHAPTER_MAX_TOKENS 覆盖。
    _target_words = (body.word_range or {}).get("max", 5000) or 5000
    chapter_max_tokens = int(os.environ.get("NA_CHAPTER_MAX_TOKENS", "32768"))
    # 按目标字数算出的理论值仅作日志留痕，便于事后对照「实际用了多少 / 是否逼近上限」
    _tokens_by_words = int(_target_words * 1.2) + 256

    scan_enabled = bool(app_config.get(db, app_config.KEY_HUMANIZE_SCAN, True))
    ingest_enabled = bool(app_config.get(db, app_config.KEY_INGEST_ENABLED, True))

    # 按需参考加载（hybrid 模式）：在 pick_relevant 基线上让 AI 额外选装参考。
    # 默认 pick_relevant 模式不进入此分支，行为完全不变（零回归）。
    ref_mode = os.environ.get("NA_CHAPTER_REF_MODE", "pick_relevant")
    if ref_mode == "hybrid" and use_model:
        messages, ctx_meta = _maybe_load_refs(db, project_id, messages, ctx_meta, default, body)

    def event_stream():
        _t0 = time.time()   # Phase 4.1/4.2：为版本留档与用量计量计总耗时
        # Phase 4.2：本次生成的链路 id。会写进用量记录 + 日志 + done 事件，
        # 使「前端看到的那次生成」↔「后台日志」↔「用量明细」三者可以互相对上。
        _trace_id = uuid.uuid4().hex[:12]
        # 这几个变量只在 use_model 分支内被赋值；先置 None，保证留档/计量时能安全取用
        report = None
        _temp = None
        adapter = None
        yield sse_event("start", {"chapter_no": chapter_no})
        yield sse_event("context", ctx_meta)
        if ctx_meta.get("refs_loaded_extra"):
            yield sse_event("refs", {"loaded": ctx_meta["refs_loaded_extra"]})
        content_parts = []
        # 错误/占位提示只用于**推给前端展示**，绝不进入 content_parts——
        # 否则会被拼进正文落库，错误文案变成小说内容（问题 1.2，2026-09-10 复核仍是活 bug）。
        error_notes: list[str] = []
        loop_stopped = False
        if use_model:
            # 章节生成温度兜底 0.75：搜索与实践结论——低温(0.4)+无重复惩罚是小模型长文复读主因。
            # 前端可显式传 temperature 覆盖（GenerateChapterDialog 已同步改为 0.75）。
            _temp = body.temperature if body.temperature is not None else 0.75
            config = {
                "api_base": default.api_base,
                "api_key": default.api_key,
                "model_name": default.model_name,
                "temperature": _temp,
                "top_p": default.top_p,
                "max_tokens": chapter_max_tokens,
                "enable_thinking": default.enable_thinking if body.enable_thinking is None else body.enable_thinking,
            }
            # 重复惩罚：与「防复读·长文循环终结」SKILL（指令层）+ _RepetitionGuard（检测截断层）
            # 组成三层防线，这里从解码参数层抑制重复：ollama 原生 repeat_penalty，
            # 其余 OpenAI 兼容厂商用 frequency/presence_penalty。NA_CHAPTER_REP_PENALTY 可整体覆盖。
            _vendor = (default.vendor or "").lower()
            _model = (default.model_name or "").lower()
            _api_base = (default.api_base or "").lower()
            # 实测（2026-08-14 长输出 A/B 对照）：frequency/presence_penalty=0.4 在
            # ModelScope Qwen3.5-122B-A10B 上会在生成中后段触发采样退化——正文变
            # 无标点长段、人名退化成拼音（Biao Yuan Zhou / Suo You Wei）、夹英文词
            # （torches）、重复 4 字词暴增；惩罚清零后同一管线（同提示词同温度）
            # 输出完全正常（标点密度 0.167、最长无标点段 18、零拉丁字母）。
            # → 该组合默认惩罚归零；其它厂商保留 0.4。env 可显式覆盖。
            # 2026-09-09 放宽：Qwen3.8-Flash-Next 上线后 "qwen3.5" 字面匹配失配，
            # 惩罚回落 0.4/0.4 实测复现退化（标点密度 0.013、最长无标点段 1870 字）。
            # 改为正则匹配 Qwen3.x 全系（qwen3.5 / qwen3.8 / 后续小版本）。
            _modelscope_qwen35 = (
                bool(re.search(r"qwen3\.\d", _model, re.IGNORECASE))
                and ("modelscope" in _api_base)
            )
            _rep_env = os.environ.get("NA_CHAPTER_REP_PENALTY")
            if _rep_env:
                _rep = float(_rep_env)
            elif _vendor == "ollama":
                _rep = 1.3
            elif _modelscope_qwen35:
                _rep = 0.0
            else:
                _rep = 0.4
            if _vendor == "ollama":
                config["repeat_penalty"] = _rep
            else:
                config["frequency_penalty"] = _rep
                config["presence_penalty"] = float(
                    os.environ.get("NA_CHAPTER_PRES_PENALTY", "0.0" if _modelscope_qwen35 else "0.4")
                )
            want_thinking = body.enable_thinking if body.enable_thinking is not None else default.enable_thinking


            _vendor = (default.vendor or "").lower()
            _model = (default.model_name or "").lower()
            _api_base = (default.api_base or "").lower()
            # ModelScope 上的 Qwen3.x 开 thinking 后，流式下 reasoning_content 挤占 token 预算，
            # 或把英文 reasoning 直接塞进 content（正文变无标点长段 + harmless/helpful 等推理词）。
            # 强制关闭 thinking（用户显式开启也覆盖），保证正文 content 是干净中文叙事。
            # 命中条件：「model 匹配 qwen3.x（正则）且 api_base 含 modelscope」，不限 vendor——
            # 默认模型标的是 vendor="custom"，若仍要求 vendor=="qwen" 会被完全绕过。
            # 2026-09-09 放宽：qwen3.5 字面匹配 → qwen3.x 正则（Qwen3.8-Flash-Next 实测同病）。
            _broken_thinking_combo = (
                bool(re.search(r"qwen3\.\d", _model))
                and "modelscope" in _api_base
            )

            # GLМ-5.2 等云端推理模型关思考写 3000+ 字长文必复读（实测两次，烧 token 到报爆）→
            # 章节生成强制开思考。本地 ollama 除外：其 think=true 时正文为空（回答含在 thinking 里），
            # 不适合章节正文。可用 NA_CHAPTER_FORCE_THINKING=0 关闭强制。
            # 注意：broken combo 必须用 elif 排除，否则会被上面的强制逻辑重新打开 thinking。
            # 2026-09-09 实验调整：Qwen3.8-Flash-Next 关 thinking 后单次生成偏短（1493 字，
            # 同参数下 Qwen3.5-122B 为 2566~2805）。改为**尊重显式指定**：
            # 请求体 enable_thinking=True 时不强关（路由层 _content_only_stream 已保证
            # reasoning 不进正文，开思考的代价只剩首字延迟与 token 预算）；未显式指定则维持默认关。
            if _broken_thinking_combo:
                want_thinking = True if body.enable_thinking is True else False
            elif (
                want_thinking is not True
                and os.environ.get("NA_CHAPTER_FORCE_THINKING", "1") == "1"
                and _vendor != "ollama"
            ):
                want_thinking = True

            try:
                adapter = get_adapter(default.vendor, config)
                guard = _RepetitionGuard()
                loop_stopped = False
                # 2026-09-20 前：思考内容一律不接收、不透传（只取 content，reasoning 直接丢弃）。
                # 该策略是为了躲「厂商把 reasoning 塞进 content」的污染坑，但它同时让思考阶段
                # 全程 0 字节 → 见下方双通道改造的【为什么改】。
                # ⚠️ 不变的是：**reasoning 永远不进正文**（content_parts）。变的只是它现在会
                # 以独立的 thinking 事件发给前端，用于保活与进度展示。
                # ── 思考/正文双通道（2026-09-20 改）──────────────────────────────
                # 【为什么改】旧链路在思考阶段把 reasoning 只 buffer 不 yield
                # （openai_compat.stream 的 _reasoning_buffer），前端这段时间**一个字节都收不到**。
                # Qwen3.8-Flash-Next 开思考首字实测 407s > vite 代理 300s 空闲上限
                # → 连接被掐断 → 浏览器只看到 "network error"（真实原因被完全掩盖）。
                #
                # 【为什么安全 —— 回应历史上的 reasoning 污染坑】
                # 旧坑是「厂商把 reasoning 塞进 content 字段」，正文因此混入英文推理词。
                # 本改动**没有**把 reasoning 接回正文：thinking 帧走**独立事件**，
                # 只做保活与进度展示，**永不 append 进 content_parts、永不落库**。
                # 正文依旧只认 kind == "content" 的帧，与旧行为一致。
                #
                # 【节流】思考帧可能上千条，全量转发会打爆前端。累积到 40 帧或满 3 秒发一次，
                # 3 秒的节奏足以让代理/浏览器认为连接活跃（vite 上限 300s）。
                # 优先级：stream_dual（无 reasoning 兜底，最安全）> stream_with_thinking
                # （ollama 版同样无兜底，可用）> stream（claude 等，无思考帧可透传）。
                # 🔴 绝不能用 openai_compat 的 stream_with_thinking：它有 reasoning→正文兜底，
                # 中文思考的模型一旦命中就把思考当正文落库（2026-09-09 老坑）。
                _dual = getattr(adapter, "stream_dual", None) or getattr(adapter, "stream_with_thinking", None)
                if _dual is not None:
                    gen = _dual(
                        messages,
                        temperature=_temp,
                        enable_thinking=want_thinking,
                        max_tokens=chapter_max_tokens,
                    )
                else:
                    # claude 等未提供双通道的适配器：退回纯正文流（无思考帧可透传）
                    gen = _content_only_stream(
                        adapter.stream(
                            messages,
                            temperature=_temp,
                            enable_thinking=want_thinking,
                            max_tokens=chapter_max_tokens,
                        )
                    )
                _think_buf: list[str] = []
                _think_sent_at = time.time()
                try:
                    for kind, delta in gen:
                        # 🔴 reasoning 只推前端，绝不进正文、绝不落库
                        if kind == "thinking":
                            _think_buf.append(delta)
                            if len(_think_buf) >= 40 or (time.time() - _think_sent_at) >= 3.0:
                                yield sse_event("thinking", {"text": "".join(_think_buf)})
                                _think_buf.clear()
                                _think_sent_at = time.time()
                            continue
                        if guard.feed(delta):
                            content_parts.append(delta)
                            yield sse_event("chunk", {"text": delta})
                        else:
                            # 检测到重复循环：立即中断，不再消费迭代器。
                            # 旧实现「continue 消费剩余 chunk」会让模型继续生成、云端继续计费
                            # 直到 token 报爆；现在 break 后由 finally 的 gen.close() 关闭底层
                            # HTTP 连接 → 模型立即停止、计费停止。
                            loop_stopped = True
                            logger.warning(f"[generate_chapter] ⚠️ 检测到重复循环，中断生成（已生成 {len(guard.full_text)} 字符）")
                            break
                    # 流正常结束：把未凑满一批的思考余量发掉（纯展示/日志价值，不进正文）
                    if _think_buf:
                        yield sse_event("thinking", {"text": "".join(_think_buf)})
                        _think_buf.clear()
                finally:
                    # 无论正常结束 / 复读中断 / 客户端断开（GeneratorExit），都关闭上游生成器，
                    # 确保底层 HTTP 连接释放，模型不会继续空转计费。
                    try:
                        gen.close()
                    except Exception as e:  # noqa: BLE001
                        # 关上游生成器失败：连接可能未释放（极端下模型继续空转）。
                        # 留痕不抛出——finally 里抛会掩盖真正的原始异常（Phase 3.5）
                        logger.debug(f"[chapter] 关闭上游生成器失败（连接可能未释放）: {type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                # 生成中途失败：降级为占位正文，保证前端不空白。服务端必须留堆栈（Phase 3.5）
                logger.exception(f"[generate_chapter] 章节生成失败，已降级为占位 project={str(project_id)[:8]}")
                _note = f"[模型调用失败，已降级为占位：{str(e)[:200]}]"
                error_notes.append(_note)
                yield sse_event("chunk", {"text": "\n" + _note})  # 仅展示，不落库
        else:
            _placeholder = "[章节正文占位 — 未配置可用模型，请在「模型配置」中添加并设为默认]"
            error_notes.append(_placeholder)
            yield sse_event("chunk", {"text": _placeholder})

        if loop_stopped:
            # 复读检测截断：给前端明确信号（否则看起来像"卡住"）
            yield sse_event("stopped", {"reason": "detected_repetition", "chars": len("".join(content_parts))})

        full = "".join(content_parts)

        # 后处理：尾部重复段落去重（流式检测的兜底 + 防止漏检）
        if len(full) > 200:
            cleaned = _dedup_trailing_repeats(full)
            if len(cleaned) < len(full):
                trimmed = len(full) - len(cleaned)
                logger.info(f"[generate_chapter] 后处理去重：裁掉 {trimmed} 字符重复内容")
                full = cleaned

        # AI 味检测：纯正则、零 token，不改写正文，只报告
        if scan_enabled and use_model and full.strip():
            try:
                report = humanizer.scan(full, scene="novel")
                yield sse_event("validate", report)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[generate_chapter] AI 味检测失败: {e}")
                yield sse_event("validate", {"issues": []})
        else:
            yield sse_event("validate", {"issues": []})

        # 未配置模型时的占位文本：不得覆盖已有章节正文（否则静默丢数据）。
        # 重新生成场景直接跳过落库，提示用户先配置模型。
        if not use_model and body.chapter_id:
            yield sse_event("chunk", {"text": "\n\n[未配置可用模型，已保留原章节正文。请在「模型配置」中添加并设为默认后重试]"})
            yield sse_event("done", {"chapter_id": body.chapter_id, "word_count": 0})
            return

        # ⚠️ 正文为空 → 绝不落库（问题 1.2）。
        # 两类场景：① 模型调用失败（只产出错误提示，已收进 error_notes，不在 full 里）；
        # ② 未配置模型且是新建章。落库会让错误提示变成小说正文；更新场景还会**静默抹掉
        # 已有正文**（数据丢失）。此时只发 error 事件说明原因，不发 saved。
        can_save, block_reason = _can_persist(full, error_notes)
        if not can_save:
            yield sse_event("error", {"message": block_reason})
            yield sse_event("done", {"chapter_id": body.chapter_id, "word_count": 0, "saved": False})
            return

        # 落库（4 级结构下 article_id 从 body 透传）
        # 重新生成（body.chapter_id 非空）→ 覆盖原章（保持章号，不新建、不递增计数）；
        # 否则新建，标题优先用用户输入、再用「第N章」兜底。
        if body.chapter_id:
            _upd = {"content": full, "word_count": len(full)}
            if body.title:
                _upd["title"] = body.title
            chapter = chapter_crud.update_chapter(
                db, project_id, body.chapter_id, ChapterUpdate(**_upd),
            )
            if chapter is None:
                # 目标章不存在（被删等异常）→ 退回新建
                chapter = chapter_crud.create_chapter(
                    db, project_id,
                    ChapterCreate(
                        chapter_no=chapter_no,
                        title=body.title or f"第{chapter_no}章",
                        content=full,
                        word_count=len(full),
                        article_id=body.article_id,
                    ),
                )
        else:
            # 幂等保护（2026-09-17）：chapter_id 丢失（重开弹窗/重试/批量重跑）时，
            # 同篇内已有**同章号或同标题**的章 → 覆盖那一章，不再新建（治"废脉异变×2"）。
            _dup = chapter_crud.find_existing_chapter(
                db, project_id, body.article_id, chapter_no, body.title)
            if _dup is not None:
                chapter = chapter_crud.update_chapter(
                    db, project_id, _dup.id,
                    ChapterUpdate(content=full, word_count=len(full),
                                  **({"title": body.title} if body.title else {})),
                )
            else:
                chapter = chapter_crud.create_chapter(
                    db,
                    project_id,
                    ChapterCreate(
                        chapter_no=chapter_no,
                        title=body.title or f"第{chapter_no}章",
                        content=full,
                        word_count=len(full),
                        article_id=body.article_id,
                    ),
                )
        yield sse_event("saved", {"chapter_id": chapter.id, "word_count": chapter.word_count})

        # ── Phase 4.1 最小 eval：把本次生成留档为一个「版本」 ──
        # 目的：让「改了配置之后是变好还是变坏」可被回答 ——
        # 留档 = 配置快照 + 正文 + 自动指标，事后人工打 1~5 分，同章多版本横向对比。
        # **旁路设计**：整段包在 try 里，任何异常都只少一条记录，绝不影响生成主链路。
        try:
            eval_crud.record_variant(
                db, project_id, chapter.id,
                article_id=chapter.article_id,
                title=chapter.title,
                content=full,
                config_snapshot={
                    "vendor": getattr(default, "vendor", None),
                    "model_name": getattr(default, "model_name", None),
                    "temperature": _temp,
                    "max_tokens": chapter_max_tokens if use_model else None,
                    "ref_mode": ref_mode,
                    "humanize_scan": scan_enabled,
                    "ingest_level": body.ingest_level or "full",
                    "word_range": body.word_range,
                    "enable_thinking": body.enable_thinking,
                },
                metrics={
                    "duration_ms": int((time.time() - _t0) * 1000),
                    "word_count": len(full),
                    "humanize_score": (report or {}).get("score"),
                    "humanize_issues": len(((report or {}).get("issues")) or []),
                    "loop_stopped": loop_stopped,
                    "error_notes": list(error_notes),
                    # 上下文只留可读摘要（ctx_meta 全量可能很大，且键名会随实现变动）
                    "ctx": {
                        "ref_mode": ctx_meta.get("ref_mode"),
                        "refs_extra": ctx_meta.get("refs_loaded_extra"),
                        "keys": sorted(str(k) for k in ctx_meta.keys()),
                    },
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[generate_chapter] 版本留档失败（不影响生成）: {type(e).__name__}: {e}")

        # ── Phase 4.2 观测消费端：记录本次模型调用的 token 用量 ──
        # 流式调用多数厂商不回 usage（last_usage=None）→ 传 prompt/completion 文本，
        # 由 usage_crud 按字符估算并标 estimated=True，避免把"没数据"记成"零消耗"。
        try:
            usage_crud.record_usage(
                db,
                scene="chapter",
                vendor=getattr(default, "vendor", None),
                model_name=getattr(default, "model_name", None),
                usage=getattr(adapter, "last_usage", None),
                project_id=project_id,
                duration_ms=int((time.time() - _t0) * 1000),
                ok=not error_notes,
                trace_id=_trace_id,
                prompt_text="".join(str(m.get("content") or "") for m in (messages or [])),
                completion_text=full,
            )
            logger.info(
                f"[generate_chapter] 用量已记录 trace_id={_trace_id} "
                f"model={getattr(default, 'model_name', None)} 字数={len(full)}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[generate_chapter] 用量记录失败（不影响生成）: {type(e).__name__}: {e}")

        # 写后摄取：挪到后台线程（独立 DB 会话），不阻塞 SSE。
        # done 立即发出——正文已落库、前端马上显示完成；摄取结果（记忆/摘要/走向）
        # 异步落库，下一章自然读到，作者无需干等。
        if ingest_enabled and use_model:
            try:
                # 走向建议归位（问题2）：推回到「被生成的这一章」自己的对话线程，
                # 而不是「生成时所在线程」(body.thread_chapter_id)。
                # 旧逻辑用 thread_chapter_id 会导致两类错位：
                #   - 从篇/卷视图（无 currentChapter）新建章 → 落到小说级默认线程（用户看到的「第一篇」）；
                #   - 从某章工作区新建「另一章」→ 落到源章线程，而非被生成章。
                # 统一改成 chapter.id（被生成章），保证「第N章的走向出现在第N章的对话框」。
                # 注意：push_conversation_id 必须传 None——conversation_id 优先级高于
                # chapter_id，传了会把卡片塞进某个会话线程而非章线程。
                # （INPUT 侧的商讨打包仍用 body.thread_chapter_id，不受影响。）
                threading.Thread(
                    target=_background_ingest,
                    args=(project_id, chapter.id, chapter.id, None, body.ingest_level),
                    daemon=True,
                ).start()
                yield sse_event("ingest_start", {"chapter_id": chapter.id, "async": True})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[generate_chapter] 启动后台摄取失败: {e}")

        yield sse_event("done", {"chapter_id": chapter.id, "word_count": chapter.word_count,
                                 "trace_id": _trace_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _chapter_to_dict(o):
    return {
        "id": o.id,
        "project_id": o.project_id,
        "article_id": o.article_id,
        "chapter_no": o.chapter_no,
        "title": o.title,
        "content": o.content,
        "note": o.note,
        "word_count": o.word_count,
        "created_at": o.created_at,
        "updated_at": o.updated_at,
    }


@router.post("/projects/{project_id}/chapters")
def create_chapter(project_id: str, body: ChapterCreate, db: Session = Depends(get_session)):
    o = chapter_crud.create_chapter(db, project_id, body)
    return ok(_chapter_to_dict(o))


@router.get("/projects/{project_id}/chapters")
def list_chapters(project_id: str, article_id: str | None = None, db: Session = Depends(get_session)):
    return ok([_chapter_to_dict(c) for c in chapter_crud.list_chapters(db, project_id, article_id)])


@router.get("/projects/{project_id}/chapters/{chapter_id}")
def get_chapter(project_id: str, chapter_id: str, db: Session = Depends(get_session)):
    o = chapter_crud.get_chapter(db, project_id, chapter_id)
    if not o:
        return ok({"id": chapter_id, "content": "", "note": ""})
    return ok(_chapter_to_dict(o))


@router.put("/projects/{project_id}/chapters/{chapter_id}")
def update_chapter(project_id: str, chapter_id: str, body: ChapterUpdate, db: Session = Depends(get_session)):
    # Phase 4.3 反馈回流：正文被改动时记一笔（对比 AI 原文）。
    # 必须先取旧值 —— 更新后就拿不到"作者改之前是什么样"了。
    before = None
    if body.content is not None:
        _old = chapter_crud.get_chapter(db, project_id, chapter_id)
        if _old is not None:
            before = _old.content or ""

    o = chapter_crud.update_chapter(db, project_id, chapter_id, body)
    if not o:
        return ok({"updated": False, "id": chapter_id})

    if before is not None and (o.content or "") != before:
        # 旁路：内部已 try/except，失败只少一条记录，不影响保存
        feedback_crud.record_chapter_edit(db, project_id, chapter_id, before, o.content or "")

    return ok(_chapter_to_dict(o))


# ---------------------------------------------------------------------------
# 段落写法打磨（2026-09-21 作者拍板）：一次请求打包全部选中段 + 各自 top-5 参考。
# 🔴 后台任务 + 轮询 —— E10 教训：魔搭开思考一轮 5~10 分钟，同步响应会被 vite 代理 300s 空闲上限掐掉。
# 🔴 C1 铁律：后台线程用自己的 SessionLocal，不碰请求级 session。
# ---------------------------------------------------------------------------
_POLISH_TASKS: dict[str, dict] = {}


@router.post("/projects/{project_id}/chapters/{chapter_id}/polish")
def start_polish(project_id: str, chapter_id: str, body: PolishRequest,
                 db: Session = Depends(get_session)):
    o = chapter_crud.get_chapter(db, project_id, chapter_id)
    if not o or not (o.content or "").strip():
        raise HTTPException(404, "章节不存在或正文为空")

    chapter_text = o.content or ""
    task_id = uuid.uuid4().hex
    _POLISH_TASKS[task_id] = {"status": "running", "result": None, "error": None,
                              "chapter_id": chapter_id, "started_at": time.time()}

    def _run():
        db2 = _db.SessionLocal()
        try:
            from app.services import para_polish, embedding_client as ec
            from app.services.plot_import import _ds_post, _ms_post, ds_key, ms_key
            emb_key = ec.get_api_key(db2)

            if body.provider == "ds":
                def call_llm(p):
                    return _ds_post(ds_key(db2), p, max_tokens=4000,
                                    temperature=0.8, timeout=600)
            else:
                mt, tmo = (6000, 900) if body.thinking else (2000, 600)
                def call_llm(p):
                    return _ms_post(ms_key(db2), p, max_tokens=mt,
                                    temperature=0.8, timeout=tmo, thinking=body.thinking)

            result = para_polish.polish_paragraphs(
                db2, chapter_text=chapter_text, para_ids=body.para_ids,
                index_db=body.index_db, api_key=emb_key,
                call_llm=call_llm, top_k=body.top_k)
            result["provider"] = body.provider
            result["thinking"] = body.thinking
            _POLISH_TASKS[task_id].update(status="done", result=result)
        except Exception as e:  # noqa: BLE001
            logger.exception("[polish] 任务失败")
            _POLISH_TASKS[task_id].update(status="error",
                                          error=f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            db2.close()

    threading.Thread(target=_run, daemon=True).start()
    return ok({"task_id": task_id, "status": "running"})


@router.get("/projects/{project_id}/chapters/{chapter_id}/polish/{task_id}")
def get_polish(project_id: str, chapter_id: str, task_id: str):
    t = _POLISH_TASKS.get(task_id)
    if not t or t.get("chapter_id") != chapter_id:
        raise HTTPException(404, "任务不存在")
    return ok(t)


@router.delete("/projects/{project_id}/chapters/{chapter_id}")
def delete_chapter(project_id: str, chapter_id: str, db: Session = Depends(get_session)):
    ok_flag = chapter_crud.delete_chapter(db, project_id, chapter_id)
    return ok({"deleted": chapter_id, "ok": ok_flag})
