"""小说导入管线（Phase 7.1）：给定小说目录 → 逐章概括 → 情节段切分 → 情节分类。

**模型**：硅基流动 `Qwen/Qwen3-8B`（``enable_thinking=false``，思考会吃光输出预算并拖慢 60s+）。
Key 复用 `app_configs.retrieval.siliconflow_key`（与向量检索同一把）。

**防限流（用户明确要求）**：
- 串行请求 + 每次请求最小间隔（默认 2s）；
- 429/5xx 指数退避（2/4/8/16/32s，最多 5 次）；
- 逐章概括按 (book_name, chapter_no) 幂等，中断重跑自动跳过已完成章节。

**版权**：只入库概括与结构模式，正文原文留在磁盘不进数据库。

三个入口（供 CLI / 未来向导页调用）：
- `import_chapters`   步骤 1-2：切章（文件名即章节）+ 逐章概括入库；
- `segment_chapters`  步骤 3：情节段切分（连续章归并为段 + 段概括）；
- `label_segments`    步骤 4：段打情节类型标签（"学院大比"这类，供跨书聚类参考）。
步骤 5「人工合并 + 凝练模板」需要人审，走向导页（下一轮）。
"""
import difflib
import json
import os
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.orm import ChapterSummaryORM
from app.services.ingestion import parse_json_loose

logger = logging.getLogger(__name__)

SF_BASE = "https://api.siliconflow.cn/v1"
SF_MODEL = "Qwen/Qwen3-8B"
KEY_CONFIG_KEY = "retrieval.siliconflow_key"

# DeepSeek V4.1 Flash：arc 归并专用（见 merge_arcs 说明）
DS_BASE = "https://api.deepseek.com"
DS_MODEL = "deepseek-flash"
DS_KEY_CONFIG = "llm.deepseek_key"

# 弧归并的可选 provider（2026-09-15 加）：魔搭 Qwen3.8-Flash-Next。
# 为什么只在弧归并用：arc 每本仅 1~5 批（全库 7 本 = 21 次调用），而魔搭免费额度 200 次/天
# → 用免费额度干最贵的活（distill 1 组 1 次，131 组会吃掉 65% 日额度，故不接）。
# vendor 必须给 "qwen"：openai_compat 的思考字段白名单按 vendor 分派；
# 但管线不走 gateway，这里直接自己拼 chat_template_kwargs（见 _ms_post）。
MS_BASE = "https://api-inference.modelscope.cn/v1"
MS_MODEL = "Qwen/Qwen3.8-Flash-Next"
MS_KEY_CONFIG = "llm.modelscope_key"

# ── 本地统一网关（MyAPI，OpenAI 兼容，http://localhost:9377，2026-09-25 接入）──
# 开关：app_configs `llm.use_gateway`=true 时，sf/ms/ds 三路 chat 全部改走网关，
# 项目侧只需保存一把统一 Key（app_configs `llm.gateway_key`，sk-myapi-…）。
# 网关侧对外模型名：qwen3.8-flash-next / glm-5.3-flash（魔搭）、qwen3-8b（硅基）、
# deepseek-flash（DeepSeek，api.yaml 深度求索渠道，2026-09-25 加）。
# 实测（2026-09-25）：enable_thinking / chat_template_kwargs 均原样透传上游
# （网关请求模型 extra="allow" + model_dump(exclude_unset=True)），关思考 1.4~2.2s 出干净正文。
# 注意：网关模式下 ms_key() **忽略 NA_MS_KEY**（否则一把直连 key 绕过网关，额度分裂）；
# 多进程并行不再需要按进程拆 key —— 网关内部自带多 Key 轮换 + 排队（2026-09-19 并行铁律更新）。
GW_BASE = "http://127.0.0.1:9377/v1"
GW_FLAG_CONFIG = "llm.use_gateway"
GW_KEY_CONFIG = "llm.gateway_key"
GW_SF_MODEL = "qwen3-8b"            # 硅基上游对外名
GW_MS_MODEL = "qwen3.8-flash-next"  # 魔搭上游对外名
GW_DS_MODEL = "deepseek-flash"      # DeepSeek 上游对外名
GW_ACTIVE = False  # 进程级缓存：由各 key getter 经 _refresh_gw_mode() 刷新；post 层只读

# 🔴 思考模式的 HTTP 超时（2026-09-16 加，用户提醒后补）：
# 文档实测「**开思考时首字延迟可达 407s**」（04-B2），而默认 timeout=300s
# → 一旦首字超过 300s，请求会被判超时→进重试→再超时，**白等一轮才失败**。
# 思考模式另有"整段推理很慢"的特点（实测 Pass2 每弧 ~90s），故给到 900s。
MS_THINKING_TIMEOUT = 900

# 可用 provider 名（CLI --arc-provider 的取值）
ARC_PROVIDERS = ("deepseek", "modelscope")

MIN_INTERVAL_S = 2.0     # 请求最小间隔（防限流）
MAX_RETRY = 5            # 429/5xx 重试次数
MAX_CHAPTER_CHARS = 20000  # 送入 LLM 的单章正文上限（2026-09-17 放开：6000 → 20000，
                           # 长章不再被截；成本靠批量打包自适应控制，不靠截正文）

# ---- token 额度预算（2026-09-16 加，见 docs/08-B17）----
# 实测：并发 4 + 批量 10 章 → 硅基流动 429 `TPM limit reached`。
# 额度是**输入+输出合计**的每分钟上限（用户提供 5 万/分钟）；**贴边极易触发**
# （输出也计入、估算有误差、思考若开也计入）→ 自设上限原取 80%（40000）。
# 🔴 2026-09-19 用户拍板调到 45000：实测 3并发下每分钟实际消耗常卡在 26~28K + 下一批 in-flight
# ≈15K，40000 窗口塞不下第 3 路并发导致频繁整窗等待（吞吐反而低）；45000（90%）实测可行。
TPM_QUOTA = 50000
TPM_BUDGET = 45000
TPM_WINDOW_S = 60.0
TOKEN_PER_CHAR = 0.7            # 中文粗估（Qwen 系约 0.6~0.8 token/字）
OUT_TOKENS_PER_CHAPTER = 200    # 逐章概括的输出预估（80~120 字摘要 + JSON 结构）


def est_tokens(chars: int) -> int:
    """按字符数粗估 token —— 只用于**发送前**的额度预算，不求精确。"""
    return int(max(0, chars) * TOKEN_PER_CHAR)


def sf_limiter(db: Session | None = None) -> "RateLimiter":
    """硅基流动专用限流器：带 token 窗口预算（直连 45000；网关模式按 Key 数放大，见 `sf_tpm_budget`）。"""
    return RateLimiter(min_interval=MIN_INTERVAL_S, tpm_budget=sf_tpm_budget(db))


# 进程级缓存：网关硅基渠道的 Key 数（加/减 Key 属低频运维，重启脚本进程即刷新）
_GW_SF_KEY_COUNT: int | None = None


def _fetch_gw_sf_key_count(db: Session | None) -> int:
    """查网关硅基渠道的 Key 数（GET /v1/quota-status）。

    🔴 失败一律返回 1（退化为单 Key 预算，**宁小勿爆**——放大预算是提速，
    小了只是慢，大了会真的撞 429）。注意本机 curl/urllib 必须绕过系统代理（B17）。
    """
    try:
        gk = _gw_key_or_none(db) if db is not None else None
        if not gk:
            logger.warning("[plot_import] 网关模式下取不到网关 Key，硅基预算按 1 把 Key 计")
            return 1
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(GW_BASE + "/quota-status",
                                     headers={"Authorization": f"Bearer {gk}"})
        with _opener.open(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for p in data.get("providers") or []:
            if p.get("provider") == "siliconflow":
                n = len(p.get("keys") or [])
                if n:
                    logger.info(f"[plot_import] 网关硅基渠道 Key 数 = {n}")
                    return n
        return 1
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plot_import] 查询网关硅基 Key 数失败，按 1 把计: {type(e).__name__}: {e}")
        return 1


def sf_tpm_budget(db: Session | None = None) -> int:
    """硅基路的客户端 token 预算（每分钟）。

    - **直连**：`TPM_BUDGET`（45000 = 单 Key 5 万/min 的 90%，见 docs/08-B17）。
    - **网关**（2026-09-25）：**Key 数 × TPM_BUDGET**。网关对每把上游 Key 各自维护
      5 万/min 滑动窗口并**自动轮换/排队**（客户端**不要**用 `provider_key_index`
      钉死某把 Key——那会绕过网关的窗口感知调度，还得自己复刻一套窗口跟踪），
      因此客户端预算按 Key 数线性放大，否则第 2 把 Key 的容量被白白浪费。
      Key 数从 `/v1/quota-status` 动态查询（进程内缓存一次），失败退化单 Key 预算。
    """
    if not GW_ACTIVE:
        return TPM_BUDGET
    global _GW_SF_KEY_COUNT
    if _GW_SF_KEY_COUNT is None:
        _GW_SF_KEY_COUNT = _fetch_gw_sf_key_count(db)
    return _GW_SF_KEY_COUNT * TPM_BUDGET


def per_request_token_cap(concurrency: int = 1, budget: int | None = None) -> int:
    """并发下**单次请求**的 token 上限（用于"估算超预算就减一章"）。

    取 `预算 / 并发数`，下限 3000（避免把批次切得过碎反而增加调用数）。
    `budget=None` 时按 `sf_tpm_budget()`（直连/网关自动适配）解析。
    """
    c = max(1, int(concurrency or 1))
    b = sf_tpm_budget() if budget is None else int(budget)
    return max(3000, int(b / c))


def chapter_est_tokens(path: str) -> int:
    """单章概括请求的 token 预估 = 正文估算 + 输出预留。

    用**文件字节数**估算字符数（UTF-8 中文 3 字节/字），避免为估算再读一遍正文。
    这一步的意义：**爆更章**（作者把 2~3 章当一章发）会在这里被自然吸收 ——
    这正是"按章数控流"失效、必须按 token 控流的场景。
    """
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    # 🔴 用**实际会送进模型**的字符数估（正文截到 MAX_CHAPTER_CHARS），否则打包估算与
    #    发送时的估算不一致 → 打包器以为塞得下，实际超预算 → 并发下互相堵死（2026-09-17 实测）
    chars = min(int(size / 3), MAX_CHAPTER_CHARS)
    return est_tokens(chars) + OUT_TOKENS_PER_CHAPTER


def _pack_batches_by_tokens(pending: list[dict], max_items: int,
                            max_tokens: int) -> list[list[dict]]:
    """按 token 预算贪心打包：**估算超预算就少放一章**（用户方案，2026-09-16）。

    为什么不能只用固定章数：**爆更章**（作者把 2~3 章当一章发）体积能差 3 倍，
    固定章数会在这种书上反复撞 TPM 限流。单章超预算时独占一批 ——
    不丢内容，靠 token 限流器替它排队。
    """
    out: list[list[dict]] = []
    cur: list[dict] = []
    cur_tok = 0
    for c in pending:
        est = chapter_est_tokens(c["path"])
        if cur and (len(cur) >= max_items or cur_tok + est > max_tokens):
            out.append(cur)
            cur, cur_tok = [], 0
        cur.append(c)
        cur_tok += est
    if cur:
        out.append(cur)
    return out

_CH_FILE_RE = re.compile(r"^(?P<no>\d{2,6})\s*[_\- ]\s*(?P<title>.*)\.txt$", re.IGNORECASE)

_SEGMENT_SYS = "你是网文情节结构分析助手。只输出 JSON，不要输出任何其他文字。"


# ---------------------------------------------------------------------------
# 硅基流动直连（管线是离线批处理子系统，刻意不走 gateway：限速/退避/关思考全自控）
# ---------------------------------------------------------------------------
class RateLimiter:
    """全局限速器：① 相邻请求**发起**间隔 ≥ min_interval；② （可选）**token 额度窗口**。

    🔴 token 窗口（2026-09-16 加，见 `docs/08-B17`）：实测「并发 4 + 批量 10 章」打爆硅基流动
    `429 TPM limit reached` —— 旧实现只控"请求发起间隔"，**完全不控 token 量**。三条要点：

    1. 额度是**输入 + 输出合计**的每分钟上限（用户提供：**5 万/分钟**）；
    2. **不能贴边跑**：输出也计数、思考若开也计数、估算本身有误差 → 自设上限取额度的 **80% = 4 万**；
    3. 并发下用「**在途预估占额**」防止多个线程同时穿过判定，请求结束后再用厂商回传的
       **真实 usage** 回填纠正（预估只用于"能不能发"，真实值用于"还剩多少"）。

    无 `tpm_budget` 时行为与旧版完全一致（纯间隔限速），所以其它 vendor 的调用不受影响。
    """

    def __init__(self, min_interval: float = MIN_INTERVAL_S,
                 tpm_budget: int | None = None, window: float = TPM_WINDOW_S):
        self._min = max(0.0, float(min_interval))
        self._last = 0.0
        self._lock = threading.Lock()
        self._budget = int(tpm_budget) if tpm_budget else None
        self._window = float(window)
        self._spent: list[tuple[float, int]] = []      # (完成时间, 真实 token)
        self._inflight = 0                              # 在途请求的预估占额

    # ---------------- token 预算 ----------------
    @property
    def budget(self) -> int | None:
        return self._budget

    def used_last_window(self) -> int:
        """最近一个窗口内的已用量（真实 + 在途预估），供观测与测试。"""
        with self._lock:
            self._prune(time.time())
            return sum(n for _, n in self._spent) + self._inflight

    def _prune(self, now: float) -> None:
        cut = now - self._window
        self._spent = [(t, n) for (t, n) in self._spent if t > cut]

    def acquire(self, est: int = 0) -> int:
        """申请一次请求的配额：先按 token 预算排队，再套最小发起间隔。返回占额（交回 `record`）。"""
        est = max(0, int(est))
        warned = False
        if self._budget:
            if est > self._budget:
                # 单次请求本身就超预算 —— 等待永远等不出来，放行并告警（避免死等）
                logger.warning(f"[plot_import] 单次请求预估 {est} token 超过预算 "
                               f"{self._budget}，直接放行（请减小批次）")
                with self._lock:
                    self._inflight += est
                self.wait()
                return est
            while True:
                with self._lock:
                    now = time.time()
                    self._prune(now)
                    spent_sum = sum(n for _, n in self._spent)
                    if spent_sum + self._inflight + est <= self._budget:
                        self._inflight += est
                        break
                    # 超预算的成因不同，等待策略也不同：
                    #  · 历史用量占着 → 等最早那笔滑出窗口；
                    #  · 只是"在途请求"占着 → 它们完成时 record() 会放额，短轮询即可
                    #    （若按窗口等，会白等 60 秒，甚至与在途互相等成死锁）
                    sleep_s = 0.5 if spent_sum == 0 else (self._spent[0][0] + self._window - now)
                if not warned:
                    warned = True
                    logger.warning(f"[limiter] 等待额度：spent={spent_sum} inflight={self._inflight} "
                                   f"est={est} budget={self._budget}（线程 {threading.current_thread().name}）")
                time.sleep(max(0.2, min(sleep_s, 5.0)))
        self.wait()
        return est

    def record(self, leased: int, actual: int | None = None) -> None:
        """归还占额，并把**真实**用量（厂商 usage）记入窗口；`actual=None` 时按占额计。"""
        with self._lock:
            if self._budget:
                self._inflight = max(0, self._inflight - max(0, int(leased)))
                self._spent.append((time.time(), max(0, int(leased if actual is None else actual))))

    # ---------------- 间隔（旧行为，保留兼容） ----------------
    def wait(self) -> None:
        with self._lock:
            now = time.time()
            gap = now - self._last
            if gap < self._min:
                time.sleep(self._min - gap)
            self._last = time.time()


def usage_total(usage) -> int | None:
    """从厂商 usage 里取总 token（优先 total_tokens，退回 prompt+completion）。"""
    if not isinstance(usage, dict):
        return None
    t = usage.get("total_tokens")
    if isinstance(t, int) and t > 0:
        return t
    p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(p, int) or isinstance(c, int):
        return int(p or 0) + int(c or 0)
    return None


def sf_key(db: Session) -> str | None:
    """取硅基流动 Key。

    网关模式（app_configs llm.use_gateway=true，2026-09-25）下返回统一网关 Key
    （llm.gateway_key）；网关开启但未配置网关 Key 时回落厂商直连并告警。
    """
    _refresh_gw_mode(db)
    if GW_ACTIVE:
        gk = _read_key(db, GW_KEY_CONFIG)
        if gk:
            return gk
        logger.warning("[plot_import] use_gateway=true 但未配置 llm.gateway_key，回落硅基直连")
    from app.services import app_config
    v = app_config.get(db, KEY_CONFIG_KEY, None)
    if isinstance(v, str):
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return v or None


def _chat_post(url: str, key: str, body: dict, *, timeout: int,
               rate: RateLimiter | None = None, label: str = "LLM",
               on_usage=None, token_hint: int | None = None) -> str:
    """带限速 + 429/5xx 指数退避的 chat 调用（**厂商无关**，不含 db 依赖）。

    并发路径专用：Key 由调用方在主线程取好传进来（Session 非线程安全）。

    `on_usage(usage: dict, duration_ms: int, ok: bool)`（2026-09-13 加）：把厂商回传的
    `usage` 交给调用方记账。**用回调而不是直接 write** —— 本函数是线程无关的纯 HTTP 层，
    持 Session 会破坏"Session 非线程安全"的既有约定；且记账失败绝不冒泡（回调内部自吞）。
    """
    rate = rate or RateLimiter()
    # 绕过系统代理（2026-09-15 加）：本机设了 HTTPS_PROXY（Clash 一类），urllib 会**继承**
    # 环境变量并把请求发给本地代理 → 实测 `Tunnel connection failed: 502 Bad Gateway`
    # （魔搭请求 2/2 批次全被打回，靠重试才侥幸成功，且留下 leftover）。
    # 离线管线是直连厂商的批处理子系统，语义上就该直连，与 curl --noproxy '*' 同理。
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # 发送前按 token 预算排队（`rate` 无预算时等价于旧的 rate.wait()）。
    # 预估 = 入参字符估算 + 输出上限（截到 2000，避免把 max_tokens 的保守预留全算进去）。
    _content = ""
    try:
        _msgs = body.get("messages") or []
        if _msgs:
            _content = str((_msgs[0] or {}).get("content") or "")
    except Exception:  # noqa: BLE001
        _content = ""
    _est = int(token_hint) if token_hint else (est_tokens(len(_content)) +
                                               min(int(body.get("max_tokens") or 0), 2000))
    last_err: Exception | None = None
    for attempt in range(MAX_RETRY):
        leased = rate.acquire(_est)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                     # 显式声明接受 gzip（2026-09-17 加）：CDN 会对大响应自作主张压缩，
                     # 而 urllib 不自动解压 → resp.read() 是二进制 → UTF-8 解码炸
                     # （实测 byte 0x81/0x80，Pass1 窗口连挂 5 次）。声明后服务端必回 gzip，下面统一解。
                     "Accept-Encoding": "gzip"},
        )
        _t0 = time.time()
        try:
            with _opener.open(req, timeout=timeout) as resp:
                _raw = resp.read()
                _enc = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in _enc:
                    import gzip as _gzip
                    _raw = _gzip.decompress(_raw)
                elif "deflate" in _enc:
                    import zlib as _zlib
                    _raw = _zlib.decompress(_raw)
                data = json.loads(_raw.decode("utf-8"))
            if on_usage:
                _safe_usage_cb(on_usage, data.get("usage"), int((time.time() - _t0) * 1000), True)
            rate.record(leased, usage_total(data.get("usage")))
            return (data["choices"][0]["message"].get("content") or "").strip()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "ignore")[:200]
            rate.record(leased, 0)      # 归还占额（失败请求不消耗额度，但也不能漏水）
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRY - 1:
                # 429 优先尊重 Retry-After（2026-09-25 加，MyAPI 网关接入：
                # 网关 429 时响应头带排队/额度恢复秒数，盲指数退避会过早重撞）；
                # 上限 150s 防御异常大值；无/非法 Retry-After 维持指数退避。
                backoff = 2.0 ** (attempt + 1)
                _ra = (getattr(e, "headers", None) or {}).get("Retry-After")
                if _ra:
                    try:
                        backoff = max(backoff, min(float(_ra), 150.0))
                    except (TypeError, ValueError):
                        pass
                logger.warning(f"[plot_import] {label} HTTP {e.code}，{backoff:.0f}s 后重试"
                               f"（{attempt + 1}/{MAX_RETRY}）: {err_body}")
                time.sleep(backoff)
                last_err = RuntimeError(f"HTTP {e.code}: {err_body}")
                continue
            if on_usage:
                _safe_usage_cb(on_usage, None, int((time.time() - _t0) * 1000), False)
            raise RuntimeError(f"{label} HTTP {e.code}: {err_body}") from e
        except Exception as e:
            last_err = e
            rate.record(leased, 0)      # 超时/网络异常同样要归还占额
            if attempt < MAX_RETRY - 1:
                backoff = 2.0 ** (attempt + 1)
                logger.warning(f"[plot_import] {label} 请求异常，{backoff:.0f}s 后重试"
                               f"（{attempt + 1}/{MAX_RETRY}）: {type(e).__name__}: {e}")
                time.sleep(backoff)
                continue
            if on_usage:
                _safe_usage_cb(on_usage, None, int((time.time() - _t0) * 1000), False)
    raise RuntimeError(f"{label} 调用失败（重试 {MAX_RETRY} 次）: {last_err}")


def _safe_usage_cb(cb, usage, duration_ms: int, ok: bool) -> None:
    """调用记账回调，**任何异常都吞掉** —— 记账永远不许影响模型调用主链路。"""
    try:
        cb(usage, duration_ms, ok)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plot_import] 用量回调失败（不影响调用）: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 流式调用（DEV-P3a ④）
# ---------------------------------------------------------------------------
# 🔴 铁律「走网关必须流式」：`plot_template_crud` / `plan_crud` 这些**批处理/规划链**
#    原本都用 `_chat_post` 阻塞读（body 无 stream:true）——P3 实测确认规划链 7.5s
#    静默返回整段 JSON，网关一旦排队/半开就会长时间无反馈甚至静默拒连。
#    帧格式与解析口径对齐章节链 `app/core/gateway/adapters/openai_compat.py::stream`：
#    逐行读 `data: {json}`，取 `choices[0].delta.content`，`[DONE]` 收尾，
#    **单帧畸形容错跳过但留痕**（Phase 3.5：厂商改结构时这是唯一线索）。
GW_STREAM_TIMEOUT = 300      # 网关排队最长 120s + 生成，须在同一窗口（见 MyAPI 接入说明）


def _stream_text(url: str, key: str, body: dict, *, timeout: int = GW_STREAM_TIMEOUT) -> tuple[str, dict | None]:
    """流式读一次 chat/completions，返回 (全文, usage)。

    🔴 **断连必须显式报错**，绝不静默返回空串（否则上层把「空」当「模型没话说」，
    P3 实测的「计划生成失败：模型未输出有效行」就会变成难以定位的假象）。
    """
    payload = dict(body)
    payload["stream"] = True
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "Accept": "text/event-stream", "Accept-Encoding": "identity"})
    # 绕过系统代理（与 _chat_post 同款：本机 HTTPS_PROXY 会把请求发给 Clash → 502）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    parts: list[str] = []
    usage = None
    try:
        with opener.open(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[plot_import.stream] 跳过无法解析的 SSE 帧: "
                                   f"{type(e).__name__}: {e}; data={data[:200]!r}")
                    continue
                # 部分厂商在**流式末帧**带 usage（此时 choices 为空）→ 先取 usage 再取 choices
                if obj.get("usage"):
                    usage = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    parts.append(piece)
                elif (delta.get("reasoning_content") or delta.get("reasoning")):
                    # 关思考的模型不该产出 reasoning；真产出了也不当正文（宁可空也不要污染）
                    logger.info("[plot_import.stream] 收到 reasoning 片段但无正文，忽略")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"网关/厂商返回 HTTP {e.code}: {detail}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"网关流式调用失败: {type(e).__name__}: {e}") from e
    text = "".join(parts)
    if not text.strip():
        raise RuntimeError("网关流式返回空正文（连接可能中途断开或模型未产出）")
    return text, usage


def _chat_post_stream(url: str, key: str, body: dict, *, timeout: int = GW_STREAM_TIMEOUT,
                      rate: RateLimiter | None = None, label: str = "LLM",
                      on_usage=None, attempts: int | None = None) -> str:
    """`_chat_post` 的流式孪生：限速排队 → `_stream_text` → 用量回调（失败静默）。

    429/5xx 指数退避与 `_chat_post` 同款；重试用尽后**抛错**（不返回空串）。

    🔴 `attempts`（DEV-P3a 加）：给「失败可退、不能拖主链」的调用方压到 1 次
       （检索层键分类用它，实测默认 5 次退避要白等 38s）。传 1 时**不 sleep**。
    """
    n = MAX_RETRY if not attempts or attempts < 1 else int(attempts)
    rate = rate or RateLimiter()
    content = ""
    try:
        msgs = body.get("messages") or []
        if msgs:
            content = str((msgs[0] or {}).get("content") or "")
    except Exception:  # noqa: BLE001
        content = ""
    est = int(est_tokens(len(content)) + min(int(body.get("max_tokens") or 0), 2000))
    last: Exception | None = None
    for attempt in range(n):
        try:
            rate.acquire(est)
        except Exception:  # noqa: BLE001
            pass
        t0 = time.time()
        try:
            text, usage = _stream_text(url, key, body, timeout=timeout)
            if on_usage:
                _safe_usage_cb(on_usage, usage, int((time.time() - t0) * 1000), True)
            return text
        except RuntimeError as e:
            last = e
            # 空正文/断连这类**重试无意义**的，直接上抛（让上层拿到明确错因）
            if "空正文" in str(e):
                raise
            logger.warning(f"[{label}] 流式第 {attempt + 1}/{n} 次失败: {e}")
            # 最后一次不再睡（没人会等下一次了），也不重试
            if attempt + 1 < n:
                time.sleep(min(2.0 * (attempt + 1), 8.0))
    raise RuntimeError(f"{label} 流式调用重试 {n} 次仍失败: {last}")


def _sf_post(key: str, user_content: str, *, max_tokens: int = 1024,
             temperature: float = 0.3, timeout: int = 300,
             rate: RateLimiter | None = None, on_usage=None,
             attempts: int | None = None) -> str:
    """硅基流动 Qwen3-8B（**关闭思考** —— Qwen3 思考默认开会吃光输出预算并拖慢 60s+）。

    2026-09-25：默认 timeout 120→300（网关模式排队最长 120s + 生成时间须在同一窗口，
    见 MyAPI 接入说明「客户端超时必须 ≥ 300 秒」；直连模式 300s 只是上限，无副作用）。
    网关模式下 base/model 切到统一网关（对外名 qwen3-8b）。

    🔴 `attempts`（DEV-P3a 加）：覆盖重试次数。给「锦上添花、失败可退」的调用方用 ——
       检索层的大类/子事件分类就是这种（网关挂了就退纯向量序），重试 5 次退避
       会让作者白等 38 秒才拿到本该立刻返回的结果（实测，见 outputs/p3_inject/
       P3a_红线实测.txt）。传 1 = 只试一次，失败立刻上抛给调用方自己降级。
    """
    base = GW_BASE if GW_ACTIVE else SF_BASE
    model = GW_SF_MODEL if GW_ACTIVE else SF_MODEL
    body = {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "enable_thinking": False,
    }
    url = base + "/chat/completions"
    # 🔴 铁律：走网关必须流式（DEV-P3a ④）。直连保持原非流式（不在本单改动范围）。
    if GW_ACTIVE:
        return _chat_post_stream(url, key, body, timeout=timeout, rate=rate,
                                 label="硅基流动", on_usage=on_usage, attempts=attempts)
    return _chat_post(url, key, body, timeout=timeout, rate=rate,
                      label="硅基流动", on_usage=on_usage, attempts=attempts)


def _refresh_gw_mode(db: Session) -> None:
    """解析「是否走统一网关」并写入进程级缓存 GW_ACTIVE。

    每次 key getter 都会调一次（与既有 app_configs 读取同量级，无额外开销顾虑）；
    读失败一律视为关闭（回落厂商直连），绝不因配置表异常拖垮调用主链路。
    """
    global GW_ACTIVE
    try:
        from app.services import app_config
        GW_ACTIVE = bool(app_config.get(db, GW_FLAG_CONFIG, False))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plot_import] 读取 {GW_FLAG_CONFIG} 失败，按直连处理: {type(e).__name__}: {e}")
        GW_ACTIVE = False


def _gw_key_or_none(db: Session) -> str | None:
    """网关模式下的统一 Key；未配置返回 None（调用方回落直连并告警）。"""
    return _read_key(db, GW_KEY_CONFIG)


def ds_key(db: Session) -> str | None:
    """取 DeepSeek Key（app_configs.llm.deepseek_key）。

    网关模式下返回统一网关 Key（llm.gateway_key）；未配置时回落直连并告警。
    """
    _refresh_gw_mode(db)
    if GW_ACTIVE:
        gk = _gw_key_or_none(db)
        if gk:
            return gk
        logger.warning("[plot_import] use_gateway=true 但未配置 llm.gateway_key，回落 DeepSeek 直连")
    return _read_key(db, DS_KEY_CONFIG)


def ms_key(db: Session) -> str | None:
    """取魔搭 ModelScope Key。

    网关模式下返回统一网关 Key（llm.gateway_key），**并忽略 NA_MS_KEY**
    （2026-09-25：否则一把直连 key 绕过网关，魔搭额度在网关外分裂，多进程并行策略
    也随之作废——网关内部自带多 Key 轮换 + 排队）。
    直连模式保持旧优先级：环境变量 `NA_MS_KEY` > app_configs.llm.modelscope_key。
    """
    _refresh_gw_mode(db)
    if GW_ACTIVE:
        gk = _gw_key_or_none(db)
        if gk:
            return gk
        logger.warning("[plot_import] use_gateway=true 但未配置 llm.gateway_key，回落魔搭直连")
    env_key = os.environ.get("NA_MS_KEY")
    if env_key:
        return env_key.strip()
    return _read_key(db, MS_KEY_CONFIG)


def _read_key(db: Session, config_key: str) -> str | None:
    """从 app_configs 读一个 key 配置，兼容「裸串」与「JSON 字符串」两种存法。"""
    from app.services import app_config
    v = app_config.get(db, config_key, None)
    if isinstance(v, str):
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return v or None


def _ds_post(key: str, user_content: str, *, max_tokens: int = 3000,
             temperature: float = 0.2, timeout: int = 300,
             rate: RateLimiter | None = None, on_usage=None) -> str:
    """DeepSeek V4.1 Flash（**关闭思考**）。

    实测（2026-09-11）：`deepseek-flash` 默认思考开，2000 token 预算全被 reasoning 吃光、
    content 返回空且耗时 10.7s；加 `thinking={"type":"disabled"}` 后 **1.7s** 拿到完整 JSON。

    `on_usage`（2026-09-13 加）：DeepSeek 回传 `usage.prompt_cache_hit_tokens` /
    `prompt_cache_miss_tokens` —— 这两档单价差 50 倍，必须落库才能做成本分析。
    """
    body = {
        "model": GW_DS_MODEL if GW_ACTIVE else DS_MODEL,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
    }
    base = GW_BASE if GW_ACTIVE else DS_BASE
    url = base + "/chat/completions"
    # 🔴 铁律：走网关必须流式（DEV-P3a ④ —— P3 实测规划链 7.5s 静默阻塞，
    #    网关排队/半开时无反馈）。直连保持原非流式（不在本单改动范围）。
    if GW_ACTIVE:
        return _chat_post_stream(url, key, body, timeout=timeout, rate=rate,
                                 label="DeepSeek", on_usage=on_usage)
    return _chat_post(url, key, body, timeout=timeout, rate=rate,
                      label="DeepSeek", on_usage=on_usage)


def _ms_post(key: str, user_content: str, *, max_tokens: int = 3000,
             temperature: float = 0.2, timeout: int = 300,
             rate: RateLimiter | None = None, on_usage=None,
             thinking: bool = False) -> str:
    """魔搭 ModelScope Qwen3.8-Flash-Next（默认**关思考**，2026-09-15 加）。

    为什么默认必须关：ModelScope 上的 Qwen3.x **默认思考开**（docs/04 §B2/B13）——
    实测无标点长段 1870 字、首字延迟 407s；而多数任务要的是干净 JSON，
    reasoning 会吃光 max_tokens 预算并污染 content。

    `thinking=True`（2026-09-16 加，用户提案）：**篇章划分是"读懂全局叙事结构"的强语义任务**，
    实测关思考时会为"凑长度"做出生硬边界（把"某决战"与"某建会"捆成一个篇章，或把一段拆碎）
    → 这类判断开思考质量更高。代价：更慢、且 **max_tokens 必须给足**（reasoning 与 content 共用预算，
    给少了 content 会是空的），故调用方需相应提高 `max_tokens`。

    字段名坑：魔搭认的是**非标准扩展** `chat_template_kwargs.enable_thinking`
    （见 docs/04 §B2 的厂商白名单：qwen 走 chat_template_kwargs；deepseek/zhipu/nvidia
    才走顶层 thinking.type）。管线不走 gateway，所以这里手工拼这个字段，
    与 `openai_compat.py` 的 `_broken_ms_qwen35` 分支保持同一语义。

    另注：arc 归并的 prompt **不含对话历史**（单条 user 消息）→ 无 prefix cache 顾虑。
    """
    body = {
        "model": GW_MS_MODEL if GW_ACTIVE else MS_MODEL,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": bool(thinking)},
    }
    base = GW_BASE if GW_ACTIVE else MS_BASE
    return _chat_post(base + "/chat/completions", key, body,
                      timeout=timeout, rate=rate, label="魔搭", on_usage=on_usage)


def make_usage_cb(scene: str, *, project_id: str | None = None):
    """构造一个**自带独立 Session** 的记账回调（离线管线专用，2026-09-13）。

    为什么不自带 Session：`_chat_post` 可能在**并发线程**里执行，而 SQLAlchemy Session
    非线程安全（见 docs/04）。回调在**调用它的那个线程**里现开现关一个 Session，
    天然线程安全，且不会与调用方的事务纠缠。

    失败一律静默：记账是旁路，永远不许影响模型调用。
    """
    # scene 前缀 → (vendor, model_name)：**必须显式映射，不能用 startswith("ds_") 二分**
    # （2026-09-15 修：新增 ms_arc 后，二分写法会把魔搭调用误记成 siliconflow/Qwen3-8B，
    #   成本分析会看错账）。
    _VENDOR_OF = {
        "ds_": ("deepseek", DS_MODEL),
        "ms_": ("modelscope", MS_MODEL),
        "sf_": ("siliconflow", SF_MODEL),
    }
    _vendor, _model = "siliconflow", SF_MODEL        # 兜底
    for _prefix, (_v, _m) in _VENDOR_OF.items():
        if scene.startswith(_prefix):
            _vendor, _model = _v, _m
            break

    def _cb(usage, duration_ms: int, ok: bool) -> None:
        from app.core import database
        from app.services import usage_crud
        # 写锁竞争重试（2026-09-14 加）：distill 是**串行单线程**——主线程在
        # distill_template 里持写事务（模板+向量入库），回调的独立 Session 会撞
        # SQLite 写锁（实测整轮 32 次记账全丢：database is locked）。主线程写完即释放，
        # 退避重试即可拿到窗口；非锁类错误不重试（真错重试也没用）。
        database.get_engine()          # 确保 SessionLocal 已初始化（惰性全局）
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                db = database.SessionLocal()
                try:
                    usage_crud.record_usage(
                        db, scene=scene,
                        vendor=_vendor,
                        model_name=_model,
                        usage=usage, project_id=project_id,
                        duration_ms=duration_ms, ok=ok,
                    )
                finally:
                    db.close()
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "locked" in str(e).lower() and attempt < 3:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                break
        logger.warning(f"[plot_import] 用量记账失败（不影响调用）scene={scene}: "
                       f"{type(last_err).__name__}: {last_err}")
    return _cb


def sf_chat(db: Session, user_content: str, *, scene: str = "sf_chat", **kw) -> str:
    """调用硅基流动 Qwen3-8B（关闭思考）。429/5xx 指数退避。失败抛 RuntimeError。

    带 Key 解析的封装（串行路径用）；并发路径请用 `_sf_post` + 主线程预取的 Key。

    `scene`（2026-09-13）：用量记账的场景名，便于按用途拆分成本。
    串行路径直接复用调用方的 session 记账（同线程，安全）。
    """
    key = sf_key(db)
    if not key:
        raise RuntimeError("未配置硅基流动 Key（app_configs.retrieval.siliconflow_key）")

    def _cb(usage, duration_ms: int, ok: bool) -> None:
        from app.services import usage_crud
        usage_crud.record_usage(db, scene=scene, vendor="siliconflow",
                                model_name=SF_MODEL, usage=usage,
                                duration_ms=duration_ms, ok=ok)

    kw.setdefault("on_usage", _cb)
    return _sf_post(key, user_content, **kw)


# ---------------------------------------------------------------------------
# 步骤 1：章节发现与读取
# ---------------------------------------------------------------------------
def discover_chapters(book_dir: str) -> list[dict]:
    """扫描目录，按文件名 `0001_章法宝坟墓.txt` 识别章节。返回按章号升序。"""
    out: list[dict] = []
    for p in sorted(Path(book_dir).glob("*.txt")):
        m = _CH_FILE_RE.match(p.name)
        if m:
            out.append({
                "no": int(m.group("no")),
                "title": m.group("title").strip(),
                "path": str(p),
            })
    return out


def read_text(path: str) -> str:
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="ignore")


# ---------------------------------------------------------------------------
# 步骤 2：逐章概括（幂等，断点续跑）
# ---------------------------------------------------------------------------
def _valid_summary(s: str) -> bool:
    """概括有效性粗校验：挡住"解析残渣/错误说明"被当成概括写进库。

    真实概括 80~120 字，15 字是很宽的下限；另挡 JSON 残片特征。
    （2026-09-11：单测暴露出补跑路径没校验，会把 "这不是 JSON" 原样入库）
    """
    if not s or len(s) < 15:
        return False
    if "chapters" in s or s.lstrip().startswith(("{", "[")):
        return False
    return True


# 相邻摘要相似度阈值：判「模型复制退化」（docs/08 B20）。
# 依据（2026-09-16 实测）：10 章批量下第 17~20 章摘要**逐字节相同**（1.000），16→17 是 0.853（半复制）；
# 正常相邻章摘要相似度通常在 0.2~0.6。阈值取 0.85：宁可多跑一次单章（代价 1 次调用），
# 也不让假摘要入库（代价是整条下游被污染 —— 实测把弧归并骗成了一条 19 章巨弧）。
_DUP_SUMMARY_RATIO = 0.85


def _dup_summary_chapters(got: dict[int, str], batch: list[dict],
                          threshold: float = _DUP_SUMMARY_RATIO) -> set[int]:
    """批内「摘要复制退化」检测：返回疑似被复制、需要重跑的章号。

    为什么必须拦：重复摘要是**合法文本**，能过 `_valid_summary`，会被当成真实内容写库 ——
    下游（弧归并 / 标签 / 模板凝练）会原样继承这个错误，且**没有任何告警**。
    """
    nos = [c["no"] for c in batch if got.get(c["no"])]
    dup: set[int] = set()
    for prev, cur in zip(nos, nos[1:]):
        a, b = got[prev], got[cur]
        if a == b:
            dup.add(cur)
            continue
        if abs(len(a) - len(b)) <= max(8, int(0.2 * max(len(a), len(b)))):
            if difflib.SequenceMatcher(None, a, b).ratio() >= threshold:
                dup.add(cur)
    if dup:
        logger.warning(f"[plot_import] 摘要疑似复制退化（相邻相似度 ≥{threshold}）"
                       f"→ 转补跑重算: 第 {sorted(dup)} 章")
    return dup


def _summarize_prompt(title: str, text: str) -> str:
    return (
        "请用 80~120 字概括这一章的主要情节。要求：\n"
        "1. 只记客观事件：谁、在哪、做了什么、结果如何；\n"
        "2. 记录新出场人物（带身份）与重要物品/地点；\n"
        "3. 不要评价文笔，不要猜测后续，不要用「本章讲述了」开头。\n"
        f"\n章节标题：{title}\n\n正文：\n{text[:MAX_CHAPTER_CHARS]}"
    )


def import_chapters(db: Session, book_dir: str, book_name: str, *,
                    start: int = 1, end: int | None = None,
                    rate: RateLimiter | None = None,
                    progress=None) -> dict:
    """逐章概括入库。幂等：已存在的 (book_name, chapter_no) 跳过。"""
    chs = discover_chapters(book_dir)
    if not chs:
        raise RuntimeError(f"目录中未发现章节文件（需形如 0001_标题.txt）: {book_dir}")
    rate = rate or RateLimiter()
    done = skipped = failed = 0
    for ch in chs:
        if ch["no"] < start or (end and ch["no"] > end):
            continue
        if db.query(ChapterSummaryORM).filter_by(
                book_name=book_name, chapter_no=ch["no"]).first():
            skipped += 1
            continue
        text = read_text(ch["path"])
        try:
            summ = sf_chat(db, _summarize_prompt(ch["title"], text),
                           max_tokens=512, temperature=0.2, rate=rate,
                           scene="sf_summarize")
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning(f"[plot_import] 第{ch['no']}章概括失败（跳过，重跑会补）: "
                           f"{type(e).__name__}: {e}")
            continue
        db.add(ChapterSummaryORM(
            id=uuid.uuid4().hex,
            book_name=book_name,
            chapter_no=ch["no"],
            title=ch["title"] or None,
            summary=summ,
            created_at=datetime.utcnow(),
        ))
        db.commit()
        done += 1
        if progress:
            progress(ch["no"], done, skipped, failed)
    return {"done": done, "skipped": skipped, "failed": failed, "total_files": len(chs)}


def _batch_prompt(items: list[dict]) -> str:
    """一次让模型概括连续 N 章（items: [{no, title, text}]）。"""
    blocks = [
        f"【第{it['no']}章 {it['title']}】\n{it['text'][:MAX_CHAPTER_CHARS]}"
        for it in items
    ]
    n = len(items)
    return (
        f"下面是连续 {n} 章的正文。请**为每一章分别**写 80~120 字概括。要求：\n"
        "1. 只记客观事件：谁、在哪、做了什么、结果如何；\n"
        "2. 记录新出场人物（带身份）与重要物品/地点；\n"
        "3. 不要评价文笔，不要猜测后续，不要用「本章讲述了」开头；\n"
        "4. 每章概括**独立完整**，不要把多章揉成一段。\n\n"
        "只输出 JSON（不要 markdown 代码块、不要任何额外说明）：\n"
        '{"chapters":[{"no":章号,"summary":"概括"},...]}\n\n'
        + "\n\n".join(blocks)
    )


def import_chapters_batch(db: Session, book_dir: str, book_name: str, *,
                          batch_size: int = 3, concurrency: int = 1,
                          start: int = 1, end: int | None = None,
                          rate: RateLimiter | None = None,
                          progress=None) -> dict:
    """批量概括入库（每 batch_size 章一次调用）+ 可选并发。**幂等语义同 import_chapters**。

    为什么这么做（2026-09-11 实测设计）：
    - 请求数降到 1/batch_size → 限流压力大降（1671 章：1671 次 → 557 次）；
    - 模型一次看到连续 N 章 → 跨章事件（一场战斗跨几章）概括更连贯；
    - 并发让"等生成"的时间重叠 —— **真正的瓶颈是等模型生成，不是限速间隔**。

    线程模型：**LLM 调用在工作线程（纯 IO），DB 写入全部回主线程串行** ——
    SQLAlchemy Session 非线程安全，这样既拿到并发又避开多线程写 SQLite。
    """
    chs = discover_chapters(book_dir)
    if not chs:
        raise RuntimeError(f"目录中未发现章节文件（需形如 0001_标题.txt）: {book_dir}")

    pending: list[dict] = []
    skipped = 0
    for ch in chs:
        if ch["no"] < start or (end and ch["no"] > end):
            continue
        if db.query(ChapterSummaryORM).filter_by(
                book_name=book_name, chapter_no=ch["no"]).first():
            skipped += 1
            continue
        pending.append(ch)

    if not pending:
        return {"done": 0, "skipped": skipped, "failed": 0, "batches": 0,
                "total_files": len(chs)}

    key = sf_key(db)      # 主线程取 Key（工作线程不碰 session）
    if not key:
        raise RuntimeError("未配置硅基流动 Key（app_configs.retrieval.siliconflow_key）")
    rate = rate or sf_limiter(db)   # 传入 db：网关模式下预算按网关 Key 数放大（2026-09-25）
    bs = max(1, int(batch_size))
    # 按 token 预算打包（章数是上限、预算是硬约束）—— 见 docs/08-B17
    _cap = per_request_token_cap(concurrency, sf_tpm_budget(db))
    batches = _pack_batches_by_tokens(pending, bs, _cap)

    # 记账回调（2026-09-13）：工作线程里现开现关独立 Session —— 天然的线程安全，
    # 且与主线程的写库事务完全隔离。
    on_usage = make_usage_cb("sf_summarize")

    def run_batch(batch: list[dict]):
        """工作线程：只读磁盘 + 网络，不碰 session。"""
        items = [{"no": c["no"], "title": c["title"], "text": read_text(c["path"])}
                 for c in batch]
        raw = _sf_post(key, _batch_prompt(items), max_tokens=400 * len(batch),
                       temperature=0.2, rate=rate, on_usage=on_usage)
        data = parse_json_loose(raw) or {}
        got: dict[int, str] = {}
        for row in data.get("chapters") or []:
            try:
                no = int(row.get("no"))
            except (TypeError, ValueError):
                continue
            s = str(row.get("summary") or "").strip()
            if s and _valid_summary(s):
                got[no] = s
        return batch, got

    done = failed = 0
    missed: list[dict] = []
    failed_batches = 0

    def _persist(batch: list[dict], got: dict) -> None:
        """主线程写库 + **每批即时提交**（2026-09-17）。

        以前把所有批次的写库**压到全部跑完之后**（`results` 先攒着）—— 中途崩
        （额度耗尽 / 进程被杀 / 网络断）就**整轮白跑**。实测：太荒 601~1200 跑了 1 小时，
        库内一动不动，正是因为写库在尾部。改成完成一批落一批：崩了也只丢最后一批，
        重跑同一命令靠幂等跳过已完成的章自然续上。
        """
        nonlocal done
        dup = _dup_summary_chapters(got, batch)   # B20：复制退化 → 不入库，走补跑重算
        for c in batch:
            s = got.get(c["no"])
            if not s or c["no"] in dup:
                missed.append(c)       # 批内漏章 / 疑似复制退化，稍后补跑
                continue
            db.add(ChapterSummaryORM(
                id=uuid.uuid4().hex,
                book_name=book_name,
                chapter_no=c["no"],
                title=c["title"] or None,
                summary=s,
                created_at=datetime.utcnow(),
            ))
            done += 1
        db.commit()
        if progress:
            progress(batch[-1]["no"], done, skipped, failed)

    if int(concurrency or 1) > 1:
        with ThreadPoolExecutor(max_workers=int(concurrency)) as ex:
            futs = [ex.submit(run_batch, b) for b in batches]
            for fut in as_completed(futs):
                try:
                    _batch, _got = fut.result()
                    _persist(_batch, _got)
                except Exception as e:  # noqa: BLE001
                    failed_batches += 1
                    logger.warning(f"[plot_import] 批次失败（重跑同一命令会自动补）: "
                                   f"{type(e).__name__}: {e}")
    else:
        for b in batches:
            try:
                _batch, _got = run_batch(b)
                _persist(_batch, _got)
            except Exception as e:  # noqa: BLE001
                failed_batches += 1
                logger.warning(f"[plot_import] 批次失败（重跑同一命令会自动补）: "
                               f"{type(e).__name__}: {e}")

    # 漏章补跑（2026-09-11 实测：批量模式下 8B 偶尔漏掉批内某章）。
    # 策略分两级：**优先把漏章重新凑批再跑一轮批量**（省得多：3 章批量 ~15s vs 单章 3×30s），
    # 仍漏的才用单章兜底（极少发生）。不依赖"下次重跑命令"来兜，让单轮尽量补齐。
    retried = 0
    if missed:
        retried = len(missed)
        still_missing: list[dict] = []
        for sb in _pack_batches_by_tokens(missed, bs, _cap):
            try:
                _, got = run_batch(sb)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[plot_import] 漏章批量补跑失败: {type(e).__name__}: {e}")
                got = {}
            for c in sb:
                s = got.get(c["no"])
                if not s:
                    still_missing.append(c)
                    continue
                db.add(ChapterSummaryORM(
                    id=uuid.uuid4().hex, book_name=book_name, chapter_no=c["no"],
                    title=c["title"] or None, summary=s, created_at=datetime.utcnow()))
                done += 1
        db.commit()

        # 兜底：批量仍漏的用单章 prompt（更简单、成功率更高）
        for c in still_missing:
            try:
                s = _sf_post(key, _summarize_prompt(c["title"], read_text(c["path"])),
                             max_tokens=512, temperature=0.2, rate=rate,
                             on_usage=make_usage_cb("sf_summarize")).strip()
                if not _valid_summary(s):
                    s = ""
            except Exception as e:  # noqa: BLE001
                s = ""
                logger.warning(f"[plot_import] 第{c['no']}章单章补跑失败（重跑命令可再补）: "
                               f"{type(e).__name__}: {e}")
            if not s:
                failed += 1
                continue
            db.add(ChapterSummaryORM(
                id=uuid.uuid4().hex, book_name=book_name, chapter_no=c["no"],
                title=c["title"] or None, summary=s, created_at=datetime.utcnow()))
            done += 1
        db.commit()
        logger.info(f"[plot_import] 漏章补跑：{retried} 章，批量+单章兜底后仍失败 {failed}")

    return {"done": done, "skipped": skipped, "failed": failed,
            "failed_batches": failed_batches, "batches": len(batches),
            "retried": retried, "total_files": len(chs)}


# ---------------------------------------------------------------------------
# 步骤 3：情节段切分
# ---------------------------------------------------------------------------
def _segment_prompt(numbered: str, prev_tail: str | None) -> str:
    ctx = f"\n（上一批最后一段的概括，供衔接参考：{prev_tail}）\n" if prev_tail else ""
    return (
        "下面是一部小说**连续若干章**的逐章概括。请把它们划分为若干「情节段」：\n"
        "- 每段 2~6 章，是一个相对完整的小故事（有起因、发展、结果）；\n"
        "- 每段输出 100~200 字概括，讲清起因→发展→结果；\n"
        "- 覆盖全部章节，段与段不重叠不遗漏；\n"
        "- 章节连续出现在同一段就按升序列出章号。\n"
        f"{ctx}\n输出 JSON：{{\"segments\": [{{\"chapters\": [1,2,3], \"summary\": \"…\"}}]}}\n"
        f"\n{numbered}"
    )


def segment_chapters(db: Session, book_name: str, *, batch: int = 10,
                     concurrency: int = 1, force: bool = False,
                     rate: RateLimiter | None = None,
                     progress=None) -> dict:
    """情节段切分：每批 batch 章交给 LLM 划段，写回 segment_no / segment_summary。

    **断点续跑（2026-09-11 加，重要）**：从前往后，**前缀内全部完成的批次直接跳过**，
    段号从已有最大段号续接。原因：长任务会被外部超时中断（实测 OpenCode 的 shell 有 25 分钟超时），
    而段切分是全书序列操作 —— 以前每次重跑都从零重算，存在"反复超时、永远跑不完"的风险。
    `force=True` 才清空重算（想整体重切时用）。

    **并发**：批次之间**没有顺序依赖**（不再用上批 tail 做衔接参考），可 `concurrency>1` 并行取结果；
    但**段号在主线程按章节顺序分配**（顺序不会乱）。
    """
    rows = (
        db.query(ChapterSummaryORM)
        .filter_by(book_name=book_name)
        .order_by(ChapterSummaryORM.chapter_no)
        .all()
    )
    rows = [r for r in rows if (r.summary or "").strip()]
    if len(rows) < 2:
        return {"segments": 0, "chapters": len(rows), "skipped_batches": 0,
                "batches_run": 0}

    if force:
        for r in rows:
            r.segment_no = None
            r.segment_summary = None
            # 同 merge_arcs：raw 必须一起清，否则后续 apply_replacement 会用旧 raw 覆盖新段文本
            r.segment_summary_raw = None
        db.commit()

    bs = max(1, int(batch))
    batch_specs = [rows[i:i + bs] for i in range(0, len(rows), bs)]

    # 前缀式跳过：从前往后，遇到第一个未完成批次就停（保证段号顺序一致）
    start_idx = 0
    for bi, b_rows in enumerate(batch_specs):
        if all(r.segment_no is not None for r in b_rows):
            start_idx = bi + 1
        else:
            break
    todo = list(enumerate(batch_specs))[start_idx:]
    skipped_batches = start_idx

    max_seg = (
        db.query(func.max(ChapterSummaryORM.segment_no))
        .filter_by(book_name=book_name)
        .scalar()
    ) or 0

    if not todo:
        return {"segments": max_seg, "chapters": len(rows),
                "skipped_batches": skipped_batches, "batches_run": 0,
                "note": "全部批次已完成"}

    key = sf_key(db)
    if not key:
        raise RuntimeError("未配置硅基流动 Key（app_configs.retrieval.siliconflow_key）")
    rate = rate or sf_limiter(db)   # 传入 db：网关模式下预算按网关 Key 数放大

    def run_batch(b_rows: list) -> dict:
        numbered = "\n".join(f"第{r.chapter_no}章：{r.summary}" for r in b_rows)
        raw = _sf_post(key, _segment_prompt(numbered, None), max_tokens=256 * len(b_rows),
                       temperature=0.2, rate=rate, on_usage=make_usage_cb("sf_segment"))
        return parse_json_loose(raw) or {}

    results: dict[int, dict] = {}
    if int(concurrency or 1) > 1 and len(todo) > 1:
        with ThreadPoolExecutor(max_workers=int(concurrency)) as ex:
            futs = {ex.submit(run_batch, b_rows): bi for bi, b_rows in todo}
            for fut in as_completed(futs):
                bi = futs[fut]
                try:
                    results[bi] = fut.result()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[plot_import] 段切分批次失败（重跑会补）: "
                                   f"{type(e).__name__}: {e}")
                    results[bi] = {}
    else:
        for bi, b_rows in todo:
            try:
                results[bi] = run_batch(b_rows)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[plot_import] 段切分批次失败（重跑会补）: "
                               f"{type(e).__name__}: {e}")
                results[bi] = {}

    # 主线程按章节顺序分配段号 + 逐批落库（进度可观测、中断不丢已完成批次）
    seg_no = max_seg + 1
    done_chapters = 0
    for i, (bi, b_rows) in enumerate(todo):
        segs = (results.get(bi) or {}).get("segments") or []
        if not segs:
            logger.warning(f"[plot_import] 段切分批次未解析出 JSON（第 {b_rows[0].chapter_no}"
                           f"~{b_rows[-1].chapter_no} 章），整批回退为单段")
            segs = [{"chapters": [r.chapter_no for r in b_rows],
                     "summary": "。".join(r.summary for r in b_rows)[:200]}]
        covered: set[int] = set()
        for seg in segs:
            summary = (seg.get("summary") or "").strip()
            hit_any = False
            for no in seg.get("chapters") or []:
                hit = next((r for r in b_rows if r.chapter_no == no), None)
                if hit is None or no in covered:
                    continue
                hit.segment_no = seg_no
                hit.segment_summary = summary
                covered.add(no)
                hit_any = True
            if hit_any:
                seg_no += 1
        for r in b_rows:          # 漏标兜底：归入上一段，保证全覆盖
            if r.segment_no is None:
                r.segment_no = max(1, seg_no - 1)
        db.commit()
        done_chapters += len(b_rows)
        logger.info(f"[plot_import] 段切分进度 {start_idx * bs + done_chapters}/{len(rows)} 章")
        if progress:
            progress(start_idx * bs + done_chapters, len(rows))
    return {"segments": seg_no - 1, "chapters": len(rows),
            "skipped_batches": skipped_batches, "batches_run": len(todo)}


# ---------------------------------------------------------------------------
# 步骤 4：段分类（情节类型标签）
# ---------------------------------------------------------------------------
def export_report(db: Session, book_name: str, out_path: str | None = None) -> str:
    """把处理结果导出为可读 Markdown 报告。返回写入路径。

    结构随数据层级自适应：
    - **有弧 → 三级**（弧 → 段 → 章）—— 人工审核"弧归并是否合理"的主要窗口；
    - 无弧 → 段级（旧行为）。
    """
    rows = (
        db.query(ChapterSummaryORM)
        .filter_by(book_name=book_name)
        .order_by(ChapterSummaryORM.chapter_no)
        .all()
    )
    # 聚合：arc → segment → chapters（无弧时统一落进 None 桶）
    arcs: dict = {}
    for r in rows:
        a = arcs.setdefault(r.arc_no, {"name": None, "summary": None, "segs": {}})
        if r.arc_name:
            a["name"] = r.arc_name
        if r.arc_summary:
            a["summary"] = r.arc_summary
        s = a["segs"].setdefault(r.segment_no, {"label": None, "summary": None, "chapters": []})
        s["chapters"].append(r)
        if r.segment_summary:
            s["summary"] = r.segment_summary
        if r.plot_label:
            s["label"] = r.plot_label

    n_segs = len({r.segment_no for r in rows})
    has_arc = any(k is not None for k in arcs)
    # 弧归并用的哪家模型：从记账表回查（2026-09-15）——provider 现在是运行时可选，
    # 不能写死 DS_MODEL，否则切魔搭后报告会撒谎。
    arc_model: str | None = None
    if has_arc:
        try:
            from app.models.orm import LlmUsageLogORM
            row = (db.query(LlmUsageLogORM)
                   .filter(LlmUsageLogORM.scene.in_(("ds_arc", "ms_arc")))
                   .order_by(LlmUsageLogORM.created_at.desc()).first())
            if row is not None:
                arc_model = row.model_name
        except Exception as e:  # noqa: BLE001 — 报告是旁路，回查失败不能拖垮导出
            logger.warning(f"[plot_import] 回查弧归并模型失败（报告降级为不详）: "
                           f"{type(e).__name__}: {e}")
    out: list[str] = [
        f"# 导入报告 · 《{book_name}》",
        "",
        f"- 章节：{len(rows)} 章 ｜ 情节段：{n_segs} 段"
        + (f" ｜ 故事弧：{len([k for k in arcs if k is not None])} 个" if has_arc else ""),
        f"- 模型：{SF_MODEL}（概括 / 段切分 / 分类）"
        + (f" + {arc_model or DS_MODEL}（弧归并）" if has_arc else ""),
        "",
        "---",
        "",
    ]

    def emit_seg(s_no, s, prefix: str) -> None:
        nos = "、".join(str(r.chapter_no) for r in s["chapters"])
        title = f"段 {s_no}" if s_no is not None else "未分段"
        out.append(f"{prefix}{title} · {s['label'] or '—'}（第 {nos} 章）")
        out.append("")
        out.append(f"**段概括**：{s['summary'] or '—'}")
        out.append("")
        for r in s["chapters"]:
            out.append(f"- **第 {r.chapter_no} 章 {r.title or ''}**：{r.summary}")
        out.append("")

    def _sorted(d: dict):
        return sorted(d.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))

    if has_arc:
        for a_no, a in _sorted(arcs):
            if a_no is None:
                if not a["segs"]:
                    continue
                out.append(f"## 未归弧（{len(a['segs'])} 段）")
                out.append("")
                for s_no, s in _sorted(a["segs"]):
                    emit_seg(s_no, s, "### ")
                continue
            chs = [c.chapter_no for s in a["segs"].values() for c in s["chapters"]]
            rng = f"第 {min(chs)}~{max(chs)} 章" if chs else "—"
            out.append(f"## 弧 {a_no} · {a['name'] or '—'}（{rng}）")
            out.append("")
            out.append(f"**弧概括**：{a['summary'] or '—'}")
            out.append("")
            for s_no, s in _sorted(a["segs"]):
                emit_seg(s_no, s, "### ")
    else:
        for s_no, s in _sorted(arcs.get(None, {"segs": {}})["segs"]):
            emit_seg(s_no, s, "## ")

    if out_path is None:
        # backend/app/services/plot_import.py → parents[3] = 项目根（[0]=services,[1]=app,[2]=backend）
        root = Path(__file__).resolve().parents[3]
        out_path = str(root / "outputs" / f"{book_name}-导入报告.md")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(out), encoding="utf-8")
    return out_path


# 段标签固定菜单（种子口径，08-B6）：覆盖网文常见情节类型；本书已用标签会动态并入
SEGMENT_LABEL_MENU = [
    "学院大比", "秘境寻宝", "势力冲突", "日常过渡", "升级突破",
    "结盟交涉", "追逃猎杀", "夺宝争夺", "复仇清算", "探查解谜",
    "危机救援", "身世揭秘", "拍卖交易", "阵地防守",
]


def label_segments(db: Session, book_name: str, *,
                   rate: RateLimiter | None = None) -> dict:
    """给每个情节段打情节类型标签（4~8 字），写回该段全部章的 plot_label。

    **标签口径治理（08-B6，2026-09-16）**：旧版让模型自由发挥 → 同一条故事线的连续段
    被标成不同标签（试点实测：赫连烈冲突系列 6 段全标「追逃猎杀」，实为冲突对峙）。
    现在三级复用抑制噪声：
      1. 【固定示例菜单】原有示例保留作种子；
      2. 【本书已用标签】DB 里该书出现过的标签全部进菜单（全书口径一致）；
      3. 【上一段标签】显式传入，提示"同一条故事线通常延续同标签，情节确实转型才换"。
    模型仍可自创新标签（菜单都不合适时），新标签即时进菜单 → 越跑越收敛。
    """
    rate = rate or RateLimiter()
    rows = (
        db.query(ChapterSummaryORM)
        .filter_by(book_name=book_name)
        .filter(ChapterSummaryORM.segment_no.isnot(None))
        .order_by(ChapterSummaryORM.segment_no, ChapterSummaryORM.chapter_no)
        .all()
    )
    segs: dict[int, list[ChapterSummaryORM]] = {}
    for r in rows:
        segs.setdefault(r.segment_no, []).append(r)

    # 固定示例菜单（种子）+ 本书已用标签词表（全书口径一致的锚）
    used_set: set[str] = set(SEGMENT_LABEL_MENU) | {
        r[0].strip() for r in db.query(ChapterSummaryORM.plot_label)
        .filter_by(book_name=book_name)
        .filter(ChapterSummaryORM.plot_label.isnot(None)).distinct()
        if r[0] and r[0].strip()
    }
    used_sorted = sorted(used_set)
    prev_label: str | None = None
    reused = 0
    labeled = 0
    for seg_no, seg_rows in sorted(segs.items()):
        summary = seg_rows[0].segment_summary or "。".join(r.summary for r in seg_rows)[:200]
        menu = "、".join(used_sorted) if used_sorted else "（本书还没有标签，可自创）"
        prev = (f"\n上一段标签：{prev_label}（同一条故事线通常延续同标签，"
                "仅当情节确实转型才换）。") if prev_label else ""
        try:
            label = sf_chat(
                db,
                "下面是小说的一个情节段概括。请从【候选标签】里选一个最贴切的"
                "（**优先复用已有标签**，保持全书口径一致；只有都不合适才自创新标签，4~8 字）。"
                "只输出标签本身，不要引号和句号。\n"
                f"【候选标签】{menu}{prev}\n"
                "【情节段概括】\n" + summary,
                max_tokens=32, temperature=0.2, rate=rate, scene="sf_label",
            )
            label = label.strip().strip("「」\"'。.")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[plot_import] 段 {seg_no} 分类失败（跳过）: {type(e).__name__}: {e}")
            continue
        if not label:
            continue
        if label in used_set:
            reused += 1
        else:
            used_set.add(label)
            used_sorted = sorted(used_set)   # 新标签即时进菜单
        for r in seg_rows:
            r.plot_label = label
        labeled += 1
        prev_label = label
    db.commit()
    return {"segments": len(segs), "labeled": labeled,
            "reused": reused, "new_labels": labeled - reused}


def merge_similar_segments(db: Session, book_name: str, *,
                           threshold: float = 0.86,
                           only_single: bool = True,
                           dry_run: bool = False) -> dict:
    """后处理：相邻且语义相似的段自动合并（08-B7，修「段切太碎」）。

    试点实测：修真四万年 19 段/30 章（平均 1.6 章/段，单章成段过半）。
    切分 prompt 已强化「优先 3~6 章/段」，本函数是**兜底后处理**，判据刻意保守：

    - 只看**相邻**段对的 embedding 余弦相似度（bge-m3，与聚类同源）；
    - `only_single=True`（默认）：仅当**至少一侧是单章段**（碎片）才允许合并——
      成型的 2~6 章段不碰；
    - 阈值 0.86 高门槛：只在「确实是同一条故事线被切碎」时合并；
    - 并查集处理链式相邻对（A~B、B~C 都相似才连成一组，避免漂移过并）。

    合并方向：组内**章数最多**的成员段为目标段（保留大单元段号），组内段概拼接；
    合并后**段号连续重排**（1..N，下游 arc 层依赖段序列）。
    ⚠️ 段变 = 既有弧标记全部失效 → **该书弧字段整体清空**（arc_summary/raw 一并清，
    防 04-B15 raw 复燃），需重跑 `--stage arc`（真实调 LLM，属预期成本）。
    向量不可用 → `skipped=True` 直接返回（与 cluster_arcs 同风格，不硬报错）。
    """
    rows = (
        db.query(ChapterSummaryORM)
        .filter_by(book_name=book_name)
        .filter(ChapterSummaryORM.segment_no.isnot(None))
        .order_by(ChapterSummaryORM.chapter_no)
        .all()
    )
    if len(rows) < 2:
        return {"skipped": True, "reason": "段数据不足", "merges": []}

    segs: dict[int, list[ChapterSummaryORM]] = {}
    order: list[int] = []
    for r in rows:
        if r.segment_no not in segs:
            segs[r.segment_no] = []
            order.append(r.segment_no)
        segs[r.segment_no].append(r)

    seg_summaries = {
        no: (srows[0].segment_summary
             or "。".join(x.summary for x in srows)[:300] or "")
        for no, srows in segs.items()
    }

    from app.services import vector_index
    if not vector_index.enabled(db):
        return {"skipped": True, "reason": "向量不可用（未配检索 Key）", "merges": []}
    try:
        from app.services import embedding_client
        vectors = embedding_client.embed_texts(
            [seg_summaries[no] for no in order], db=db)
    except Exception as e:  # noqa: BLE001
        return {"skipped": True,
                "reason": f"向量化失败: {type(e).__name__}: {str(e)[:80]}", "merges": []}

    def _cos(x, y):
        s = sum(i * j for i, j in zip(x, y))
        nx = sum(i * i for i in x) ** 0.5
        ny = sum(j * j for j in y) ** 0.5
        return s / (nx * ny) if nx and ny else 0.0

    # 相邻对判定 → 并查集（链式：A~B、B~C 都过门槛才成组）
    parent = {no: no for no in order}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    qualifying = []
    for i in range(len(order) - 1):
        a, b = order[i], order[i + 1]
        sim = _cos(vectors[i], vectors[i + 1])
        frag = (len(segs[a]) == 1 or len(segs[b]) == 1) if only_single else True
        if sim >= threshold and frag:
            parent[_find(a)] = _find(b)
            qualifying.append({"pair": [a, b], "sim": round(sim, 4),
                               "sizes": [len(segs[a]), len(segs[b])]})

    groups: dict[int, list[int]] = {}
    for no in order:
        groups.setdefault(_find(no), []).append(no)
    merge_groups = [g for g in groups.values() if len(g) > 1]

    if not merge_groups:
        return {"merged_groups": 0, "segments_before": len(order),
                "segments_after": len(order), "merges": [], "arcs_cleared": False}

    if dry_run:
        return {"dry_run": True,
                "merged_groups": len(merge_groups),
                "segments_before": len(order),
                "segments_after": len(order) - sum(len(g) - 1 for g in merge_groups),
                "merges": [{"members": g,
                            "chapter_sizes": [len(segs[x]) for x in g]}
                           for g in merge_groups]}

    # 真合并：组内章数最多者为目标段；段概拼接；plot_label 用目标段口径
    for g in merge_groups:
        target = max(g, key=lambda no: len(segs[no]))
        merged_summary = "\n".join(seg_summaries[no] for no in g)
        target_label = segs[target][0].plot_label
        for no in g:
            for r in segs[no]:
                r.segment_no = target
                r.segment_summary = merged_summary
                r.segment_summary_raw = None   # 04-B15：raw 必须同清，防复燃
                r.plot_label = target_label

    # 段号连续重排（1..N，按章节顺序）
    new_no = 0
    last = None
    for r in rows:
        if r.segment_no != last:
            new_no += 1
            last = r.segment_no
        r.segment_no = new_no

    # 段变 = 既有弧失效 → 整体清空（含 raw）
    for r in rows:
        r.arc_no = None
        r.arc_name = None
        r.arc_summary = None
        r.arc_summary_raw = None
    db.commit()

    return {"merged_groups": len(merge_groups),
            "segments_before": len(order),
            "segments_after": new_no,
            "merges": [{"members": g,
                        "chapter_sizes": [len(segs[x]) for x in g]}
                       for g in merge_groups],
            "arcs_cleared": True}


# ---------------------------------------------------------------------------
# 步骤 6：故事弧归并（arc 层 —— 模板真正的候选单元）
# ---------------------------------------------------------------------------
# ===========================================================================
# v3 弧归并：**切点优先 + 确定性缝合**（2026-09-16，见 docs/08-C8）
# ---------------------------------------------------------------------------
# 为什么推倒重来（两轮 200 章实测的结论）：
#   · v1「让模型在窗口内直接划弧、只采纳 core 区」→ **网格伪影**：14 条弧里 9 条边界
#     精确贴在窗口网格线上（弧边界被处理网格逼出来，与 C1 同源）；
#   · v2「按弧心归属、边界自由」→ 修好网格但 **约 10% 章节无弧覆盖**（丢内容）。
#   ⇒ 结论：**"定边界"与"写内容"必须分离**。
#      Pass1 每窗只提切点（输出一行章号）→ 纯代码缝合（覆盖由构造保证）
#      → Pass2 逐弧写内容（输入仅几千字，截断风险最低）。
# 实测（九星 1~200 章）：core 40+后视20（5 窗）与 core 100+后视20（2 窗）**两档都 100% 覆盖、
# 0 条超长弧**；core 100 更优（碎弧 3 条 vs 6 条、均值 13.3 vs 11.8）。
# ===========================================================================
ARC_CORE_DEFAULT = 100     # 每窗核心章数
ARC_TAIL_DEFAULT = 20      # 后视章数 —— **必须 ≥ 单弧最大长度**，否则跨界的弧看不全
ARC_MIN_LEN = 8            # 单弧最短：短于此与邻居合并（碎弧阈值）
ARC_MAX_LEN = 40           # **仅作提示**（2026-09-16 上调 25→40）：大事件/高光篇章本就可以很长
#                            （用户实测反馈：一场考核含多个阶段 → 33 章是**好**篇章，不该切）。
#                            `postcheck_arcs` 只把它列进 `over_max` 供人工看一眼，**不参与任何自动处理**。
CUT_TRUST_MARGIN = 8       # 只信"离可见区起点 ≥ 此值"的切点（窗口开头的刀不可信）
CUT_CLUSTER_GAP = 4        # ±此值内视为同一刀（不同窗口对同一边界的提议会有抖动）
ARC_AMIN, ARC_AMAX = 12, 18  # 提示词里的目标弧长区间（用户 2026-09-16 拍板）


def _cuts_prompt(lo: int, hi: int, numbered: str) -> str:
    """Pass 1 提示词：**只提切点**，输出极小（一行章号），也不提"核心区"（避免网格暗示）。"""
    return (
        f"下面是一部小说连续的逐章概括（第 {lo}~{hi} 章，共 {hi - lo + 1} 章）。\n"
        "请你找出其中的**篇章分界点**：一个篇章结束、下一个新篇章开始的那一章。\n\n"
        "判断标准：当这一段冲突/目标已经收束、另起一条新线索或进入新的场景/目标时，就是分界点。\n"
        "🔴 边界按**故事本身的转折**定，**不要迁就任何固定的章号间隔**。\n"
        "🔴 **篇幅不设硬上限**：普通篇章 8~20 章；**大事件篇章**（一场考核、一次拍卖会、一段夺宝/逃亡）\n"
        "   内部往往含多个阶段，**25~40 章也是正常的好篇章**（这正是我们要的「优质篇章」），不要为压短而硬切。\n"
        "🔴 但**不要把两件不相干的事捆成一个篇章**（例如「某决战 + 某建会」是两个篇章），\n"
        "   也不要把同一件事切成碎片 —— 该合就合、该分就分。\n\n"
        "只输出 JSON（章号 = **新篇章的第一章**，不要包含第 " + str(lo) + " 章）：\n"
        '{"cuts":[章号, …]}\n\n'
        f"===== 逐章概括（第 {lo}~{hi} 章）=====\n{numbered}"
    )


def _arc_content_prompt(arc: list[int], body: str, before: str, after: str) -> str:
    """Pass 2 提示词：给一条弧写名称/概括/节拍（输入只有该弧的章概括 + 少量上下文）。"""
    ctx_b = f"（前面几章，仅供理解背景，不要写进本弧：\n{before}）\n\n" if before else ""
    ctx_a = f"\n\n（后面几章，仅供理解走向，不要写进本弧：\n{after}）" if after else ""
    return (
        f"下面是一部小说**第 {arc[0]}~{arc[-1]} 章**的逐章概括 —— 这是**一个完整的篇章（故事弧）**。\n"
        "请为它输出：4~8 字名称、80~150 字概括（起因→升级→转折→结果）、以及 3~6 个「节拍」"
        "（节拍 = 弧内部的一个阶段，通常覆盖 3~6 章，**不是「一章一个」**）。\n"
        f"节拍的章号必须落在 {arc[0]}~{arc[-1]} 之间，连续、不重叠、不遗漏，合起来覆盖整条弧。\n\n"
        "只输出 JSON（不要 markdown 代码块）：\n"
        '{"name":"弧名","summary":"概括","beats":[{"chapters":[起,止],"label":"节拍名","summary":"20~40字说明"}]}\n\n'
        f"{ctx_b}===== 本弧逐章概括 =====\n{body}{ctx_a}"
    )


def stitch_cuts(raw_cuts: list[int], total: int, *,
                cluster_gap: int = CUT_CLUSTER_GAP,
                min_len: int = ARC_MIN_LEN) -> list[int]:
    """把各窗口提的切点**聚类成全局切点表**（纯函数，可单测）。

    1. 排序后按 `cluster_gap` 单链聚类（±4 章视为同一刀），取中位 —— 消掉窗口间的抖动；
    2. 去掉 ≤1 的切点（第 1 章永远是弧起点）；
    3. 保证最短弧长：反复删掉"造成最短弧"的那个切点，直到没有弧短于 `min_len`。
    """
    cuts = sorted({c for c in raw_cuts if isinstance(c, int) and c > 1})
    if not cuts:
        return []
    clusters: list[list[int]] = [[cuts[0]]]
    for c in cuts[1:]:
        if c - clusters[-1][-1] <= cluster_gap:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    merged = sorted(cl[len(cl) // 2] for cl in clusters)   # 偶数个取**上中位**：
    # 边界偏后 → 前一条弧偏长，更不容易触发"最短弧长"约束（偏前会把前弧压短）

    # 最短弧长约束：starts = [1] + cuts
    while merged:
        starts = [1] + merged
        lens = [(starts[i + 1] if i + 1 < len(starts) else total + 1) - starts[i]
                for i in range(len(starts))]
        if min(lens) >= min_len:
            break
        worst = lens.index(min(lens))
        # 删掉"让这一小段成为独立弧"的那个切点（即该段右边界）
        merged.pop(worst if worst < len(merged) else worst - 1)
    return merged


def arcs_from_cuts(cuts: list[int], total: int) -> list[tuple[int, int]]:
    """由切点表生成弧区间 —— **覆盖由构造保证**（不可能出现缺口或重叠）。"""
    starts = [1] + [c for c in cuts if 1 < c <= total]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] - 1 if i + 1 < len(starts) else total
        if e >= s:
            out.append((s, e))
    return out


def postcheck_arcs(arcs: list[tuple[int, int]], total: int, *,
                   min_len: int = ARC_MIN_LEN,
                   max_len: int = ARC_MAX_LEN) -> tuple[list[tuple[int, int]], dict]:
    """确定性校验：**短弧并入邻居**（迭代到稳定）、**超长弧标记**、覆盖自检。

    这一步是"提示词不可靠"的兜底 —— 实测模型不会 100% 服从长度约束
    （200 章里出现过 9 章弧、也出现过 44 章巨弧）。
    """
    work = list(arcs)
    merged = 0
    for _ in range(50):
        if len(work) <= 1:
            break
        lens = [e - s + 1 for s, e in work]
        short = [i for i, n in enumerate(lens) if n < min_len]
        if not short:
            break
        i = short[0]
        j = i - 1 if i > 0 else 1                  # 优先并入前一条
        a, b = min(i, j), max(i, j)
        work[a:b + 1] = [(work[a][0], work[b][1])]
        merged += 1
    lens = [e - s + 1 for s, e in work]
    report = {
        "merged": merged,
        "arcs": len(work),
        "lens": lens,
        "mean": round(sum(lens) / len(lens), 2) if lens else 0,
        "over_max": [f"{s}~{e}" for s, e in work if e - s + 1 > max_len],
        "under_min": [f"{s}~{e}" for s, e in work if e - s + 1 < min_len],
        "coverage_ok": bool(work) and work[0][0] == 1 and work[-1][1] == total
        and sum(lens) == total,
    }
    return work, report


def _arc_provider_post(db: Session, provider: str, prompt: str, *, max_tokens: int,
                       rate: RateLimiter | None = None, thinking: bool = False) -> str:
    """按 provider 分派到厂商（与 `merge_arcs` 同口径；Key 在主线程取）。

    `thinking` 只对 modelscope 有效（DeepSeek 那条本来就是关思考的）——
    篇章划分/内容提炼是"读懂叙事结构"的强语义任务，开思考质量更高（见 `_ms_post`）。
    """
    if provider == "modelscope":
        key = _read_key(db, MS_KEY_CONFIG)
        if not key:
            raise RuntimeError("未配置魔搭 Key（app_configs.llm.modelscope_key）")
        return _ms_post(key, prompt, max_tokens=max_tokens, rate=rate, thinking=thinking,
                        timeout=(MS_THINKING_TIMEOUT if thinking else 300),
                        on_usage=make_usage_cb("ms_arc"))
    key = ds_key(db)
    if not key:
        raise RuntimeError("未配置 DeepSeek Key（app_configs.llm.deepseek_key）")
    return _ds_post(key, prompt, max_tokens=max_tokens, rate=rate,
                    on_usage=make_usage_cb("ds_arc"))


def build_arcs_v3(db: Session, book_name: str, *, provider: str = "modelscope",
                  core: int = ARC_CORE_DEFAULT, tail: int = ARC_TAIL_DEFAULT,
                  force: bool = False, rate: RateLimiter | None = None,
                  thinking: bool = True, progress=None,
                  append_mode: bool = False) -> dict:
    """v3 弧归并（切点优先 + 确定性缝合 + 逐弧写内容）。

    与 `merge_arcs` 的关键差别：**不再需要"段"** —— 直接读逐章概括。
    写回 `chapter_summaries` 的同名字段，其中 `segment_*` **语义重定义为「弧内节拍」**
    （列名不改，避免 SQLite 迁移；`plot_distill.collect_arcs` 正好按
    `(arc_no, segment_no)` 聚合节拍，下游零改动）。

    幂等（同 `merge_arcs` 口径）：全书已有弧 → 直接跳过（`force=True` 强制重算）。
    """
    if provider not in ARC_PROVIDERS:
        raise ValueError(f"未知的 arc provider: {provider!r}（可选 {ARC_PROVIDERS}）")
    rows = (db.query(ChapterSummaryORM)
            .filter_by(book_name=book_name)
            .order_by(ChapterSummaryORM.chapter_no).all())
    rows = [r for r in rows if (r.summary or "").strip()]
    if len(rows) < 2:
        return {"arcs": 0, "chapters": len(rows), "error": "逐章概括不足（先跑 --stage summarize）"}

    by_no = {r.chapter_no: r for r in rows}
    # ---------- 区间弧模式（2026-09-17，用户拍板 B 方案）----------
    # append_mode=True：**保留已有弧**，只把「最后一条已有弧的起点 → 全书末尾」重新切弧
    # （最后一条弧回炉，修正旧边界可能的切错）。扩容新书不必全量重跑（省 ~70% 调用）。
    arc_no_start = 1
    range_start = None
    if append_mode:
        _arc_rows = [r for r in rows if r.arc_no is not None]
        if _arc_rows:
            arc_no_start = max(r.arc_no for r in _arc_rows)
            range_start = min(r.chapter_no for r in _arc_rows if r.arc_no == arc_no_start)
    if range_start is not None:
        rows = [r for r in rows if r.chapter_no >= range_start]   # 工作集 = 重跑区间
        logger.info(f"[plot_import] arc(v3) 区间模式：从第 {range_start} 章重切"
                    f"（保留此前 {arc_no_start - 1} 条弧），弧号从 {arc_no_start} 续编")

    if all(r.arc_no is not None for r in rows) and not force:
        n = len({r.arc_no for r in rows})
        logger.info(f"[plot_import] arc(v3) 幂等跳过：{book_name} 全部 {len(rows)} 章已有弧（{n} 条）")
        return {"skipped": True, "reason": f"全部 {len(rows)} 章已有弧（{n} 条）", "arcs": n}

    nos = [r.chapter_no for r in rows]
    total = nos[-1]
    rate = rate or RateLimiter()

    # ---------- Pass 1：切点 ----------
    cuts: list[int] = []
    s = nos[0]
    while s <= total:
        core_end = min(s + core - 1, total)
        hi = min(core_end + tail, total)
        visible = [n for n in range(s, hi + 1) if n in by_no]
        numbered = "\n".join(f"第{n}章：{by_no[n].summary}" for n in visible)
        prompt = _cuts_prompt(s, hi, numbered)
        try:
            raw = _arc_provider_post(db, provider, prompt,
                                     max_tokens=(6000 if thinking else 2000),
                                     rate=rate, thinking=thinking)
            got = [int(x) for x in ((parse_json_loose(raw) or {}).get("cuts") or [])
                   if str(x).lstrip("-").isdigit()]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[plot_import] v3 Pass1 窗口 {s}~{core_end} 失败："
                           f"{type(e).__name__}: {str(e)[:120]}")
            got = []
        trusted = [c for c in got if s + CUT_TRUST_MARGIN <= c <= core_end + CUT_TRUST_MARGIN
                   and s < c <= hi]
        cuts += trusted
        logger.info(f"[plot_import] v3 Pass1 窗口 {s}~{core_end}（可见到 {hi}）："
                    f"提出 {len(got)} 刀，可信 {len(trusted)} 刀")
        if progress:
            progress(core_end, len(cuts))
        s = core_end + 1

    # ---------- 缝合（区间模式：坐标先平移到 1 起，算完再平移回去）----------
    _off = (range_start - 1) if range_start else 0
    final_cuts = stitch_cuts([c - _off for c in cuts], total - _off)
    arcs_rel = arcs_from_cuts(final_cuts, total - _off)
    arcs_rel, rep = postcheck_arcs(arcs_rel, total - _off)
    arcs = [(a + _off, b + _off) for a, b in arcs_rel]
    logger.info(f"[plot_import] v3 缝合：{len(cuts)} 刀 → {len(final_cuts)} 刀 → "
                f"{len(arcs)} 条弧（合并 {rep['merged']} 次），均值 {rep['mean']} 章，"
                f"覆盖 {'OK' if rep['coverage_ok'] else '异常'}")

    # ---------- 清旧标记（重算语义同 merge_arcs）----------
    for r in rows:
        r.arc_no = r.arc_name = r.arc_summary = None
        r.arc_summary_raw = None
        r.segment_no = r.segment_summary = None
        r.segment_summary_raw = None
    db.commit()

    # ---------- Pass 2：逐弧写内容 ----------
    seg_no = 0
    for idx, (a0, a1) in enumerate(arcs, start=arc_no_start):
        arc_chs = [n for n in range(a0, a1 + 1) if n in by_no]
        body = "\n".join(f"第{n}章：{by_no[n].summary}" for n in arc_chs)
        before = "\n".join(f"第{n}章：{by_no[n].summary}"
                           for n in range(max(nos[0], a0 - 3), a0) if n in by_no)
        after = "\n".join(f"第{n}章：{by_no[n].summary}"
                          for n in range(a1 + 1, min(total, a1 + 3) + 1) if n in by_no)
        name, summary, beats = None, None, []
        try:
            raw = _arc_provider_post(db, provider,
                                     _arc_content_prompt(arc_chs, body, before, after),
                                     max_tokens=(12000 if thinking else 3000),
                                     rate=rate, thinking=thinking)
            d = parse_json_loose(raw) or {}
            name = (d.get("name") or "").strip() or None
            summary = (d.get("summary") or "").strip() or None
            beats = [b for b in (d.get("beats") or []) if isinstance(b, dict)]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[plot_import] v3 Pass2 弧 {a0}~{a1} 失败："
                           f"{type(e).__name__}: {str(e)[:120]}")
        # 兜底：Pass2 失败也不许让弧缺名/缺概括（下游要按名展示）
        name = name or f"第{a0}~{a1}章"
        summary = summary or "。".join((by_no[n].summary or "") for n in arc_chs)[:200]
        for r_no in arc_chs:
            r = by_no[r_no]
            r.arc_no, r.arc_name, r.arc_summary = idx, name, summary
        # 节拍 → segment_*（语义=弧内节拍）
        covered: set[int] = set()
        for b in beats:
            ch = [int(x) for x in (b.get("chapters") or []) if str(x).isdigit()]
            ch = [n for n in range(min(ch), max(ch) + 1) if n in arc_chs] if len(ch) >= 2 else []
            if not ch or covered.intersection(ch):
                continue
            seg_no += 1
            for n in ch:
                r = by_no[n]
                r.segment_no = seg_no
                r.segment_summary = (b.get("summary") or "").strip() or None
                r.plot_label = (b.get("label") or "").strip() or r.plot_label
            covered.update(ch)
        # 未被节拍覆盖的章 → 归入本弧最后一段（保证 arc→beat 无空洞）
        left = [n for n in arc_chs if n not in covered]
        if left:
            seg_no += 1
            for n in left:
                r = by_no[n]
                r.segment_no = seg_no
                r.segment_summary = r.segment_summary or (r.summary or "")[:120]
        db.commit()
        if progress:
            progress(a1, idx)

    return {"arcs": len(arcs), "chapters": len(rows), "cuts": len(final_cuts),
            "stitch": rep, "provider": provider, "core": core, "tail": tail,
            "append": append_mode, "range_start": range_start}


def window_specs(first: int, last: int, core: int, tail: int) -> list[dict]:
    """按 core/tail 切窗口 → `[{lo, core_end, need}]`（纯函数，可单测）。

    `need` = 跑该窗 Pass1 所必需的**已摘要到**的章号（core_end + 后视）。
    """
    out: list[dict] = []
    lo = first
    while lo <= last:
        core_end = min(lo + core - 1, last)
        out.append({"lo": lo, "core_end": core_end, "need": min(core_end + tail, last)})
        lo = core_end + 1
    return out


def settled_arcs(raw_cuts: list[int], core_end: int, *,
                 margin: int = CUT_TRUST_MARGIN,
                 min_len: int = ARC_MIN_LEN) -> list[tuple[int, int]]:
    """窗口 `core_end` 跑完 Pass1 后**已定型**的弧区间（可立刻跑 Pass2）。

    🔴 依据（这是"与摘要并行"的正确性基础）：窗口 w 的可信切点范围是
    `[lo+margin, core_end+margin]`，而下一个窗口 w+1 的可信切点**最早只能是
    `core_end+margin+1`** ⇒ 落在 `[.., core_end+margin]` 内的切点**再也不会被后续窗口改动**。
    因此：**结束章 < core_end+margin 的弧**（其右邻居切点也在已知区间内）即可定型；
    恰好停在 horizon 上的那条不算（它的右边界还要看后续切点）。
    """
    horizon = core_end + margin
    cuts = [c for c in raw_cuts if c <= horizon]
    if not cuts:
        return []
    merged = stitch_cuts(cuts, horizon, min_len=min_len)
    return [a for a in arcs_from_cuts(merged, horizon) if a[1] < horizon]


def _wait_for_summaries(db: Session, book_name: str, upto: int, *,
                        timeout: float = 3600.0, poll: float = 5.0) -> int:
    """等摘要进度追上 `upto`（章号），返回已达成的最大章号。

    ⚠️ 每次查询前先 `commit()` 结束当前事务 —— 本机实测「长事务会把 SQLite 读视图冻住」，
    不结束事务就永远读不到别的连接新提交的行（那正是"运行中读到旧快照"的成因）。
    """
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        db.commit()
        mx = (db.query(func.max(ChapterSummaryORM.chapter_no))
              .filter_by(book_name=book_name).scalar()) or 0
        if mx >= upto:
            return mx
        if time.time() > deadline:
            logger.warning(f"[plot_import] v3-stream 等摘要超时：已到第 {mx} 章，需要 {upto}")
            return mx
        time.sleep(poll)


def _fetch_arc_content(db: Session, by_no: dict, span: tuple[int, int], *,
                       provider: str, rate: RateLimiter | None, thinking: bool) -> dict:
    """调模型取**一条弧**的名称/概括/节拍（含兜底）。纯取内容、不写库 → 流式与串行两条路共用。"""
    a0, a1 = span
    arc_chs = [n for n in range(a0, a1 + 1) if n in by_no]
    body = "\n".join(f"第{n}章：{by_no[n].summary}" for n in arc_chs)
    first = min(by_no)
    last = max(by_no)
    before = "\n".join(f"第{n}章：{by_no[n].summary}"
                       for n in range(max(first, a0 - 3), a0) if n in by_no)
    after = "\n".join(f"第{n}章：{by_no[n].summary}"
                      for n in range(a1 + 1, min(last, a1 + 3) + 1) if n in by_no)
    name = summary = None
    beats: list = []
    try:
        raw = _arc_provider_post(db, provider, _arc_content_prompt(arc_chs, body, before, after),
                                 max_tokens=(12000 if thinking else 3000),
                                 rate=rate, thinking=thinking)
        d = parse_json_loose(raw) or {}
        name = (d.get("name") or "").strip() or None
        summary = (d.get("summary") or "").strip() or None
        beats = [b for b in (d.get("beats") or []) if isinstance(b, dict)]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plot_import] v3 Pass2 弧 {a0}~{a1} 失败："
                       f"{type(e).__name__}: {str(e)[:120]}")
    return {
        "span": (a0, a1),
        "name": name or f"第{a0}~{a1}章",
        "summary": summary or "。".join((by_no[n].summary or "") for n in arc_chs)[:200],
        "beats": beats,
    }


def _apply_arc_content(by_no: dict, content: dict, idx: int, seg_start: int) -> int:
    """把一条弧的内容写进 ORM 对象（调用方负责 commit）。返回下一个可用节拍号。"""
    a0, a1 = content["span"]
    arc_chs = [n for n in range(a0, a1 + 1) if n in by_no]
    for n in arc_chs:
        r = by_no[n]
        r.arc_no, r.arc_name, r.arc_summary = idx, content["name"], content["summary"]
    seg_no = seg_start
    covered: set[int] = set()
    for b in content.get("beats") or []:
        ch = [int(x) for x in (b.get("chapters") or []) if str(x).isdigit()]
        ch = [n for n in range(min(ch), max(ch) + 1) if n in arc_chs] if len(ch) >= 2 else []
        if not ch or covered.intersection(ch):
            continue
        seg_no += 1
        for n in ch:
            r = by_no[n]
            r.segment_no = seg_no
            r.segment_summary = (b.get("summary") or "").strip() or None
            r.plot_label = (b.get("label") or "").strip() or r.plot_label
        covered.update(ch)
    left = [n for n in arc_chs if n not in covered]
    if left:
        seg_no += 1
        for n in left:
            r = by_no[n]
            r.segment_no = seg_no
            r.segment_summary = r.segment_summary or (r.summary or "")[:120]
    return seg_no + 1


def build_arcs_v3_streaming(db: Session, book_name: str, *, provider: str = "modelscope",
                            core: int = ARC_CORE_DEFAULT, tail: int = ARC_TAIL_DEFAULT,
                            force: bool = False, rate: RateLimiter | None = None,
                            thinking: bool = True, expect_total: int | None = None,
                            wait_timeout: float = 3600.0, progress=None) -> dict:
    """v3 **流式**编排：**与"章摘要"阶段并行**跑（用户提案，2026-09-16）。

    为什么要流式：两条线用**不同厂商、不同额度**（章摘要 = 硅基 Qwen3-8B，受 5 万 TPM 限；
    弧 = 魔搭 Qwen3.8-Flash-Next，独立额度），且**节奏相近**（各约 90 秒 / 15 章）
    ⇒ 弧的活可以整体藏进摘要的等待时间里（600 章：≈130 分 → ≈70 分）。

    流程（每窗）：等摘要到 `core_end+tail` → Pass1 提切点 → 按 `settled_arcs` 判定**已定型弧**
    → 立刻跑 Pass2 写内容；全部窗口跑完后统一缝合 → **收尾对账**（只重算被缝合改动的弧）。

    `expect_total`：全书章号上界（来自源目录文件数）。**必须给**，否则会把"已摘要到的最后一章"
    当全书末尾（正是不能靠"手工开两个进程"代替的原因）。
    """
    if provider not in ARC_PROVIDERS:
        raise ValueError(f"未知的 arc provider: {provider!r}（可选 {ARC_PROVIDERS}）")
    rate = rate or RateLimiter()

    def _load_rows() -> list:
        db.commit()
        rows = (db.query(ChapterSummaryORM).filter_by(book_name=book_name)
                .order_by(ChapterSummaryORM.chapter_no).all())
        return [r for r in rows if (r.summary or "").strip()]

    rows = _load_rows()
    if not rows:
        return {"arcs": 0, "error": "没有逐章概括（先跑 --stage summarize）"}
    first, last_known = rows[0].chapter_no, rows[-1].chapter_no
    total = int(expect_total or last_known)
    if all(r.arc_no is not None for r in rows) and not force and last_known >= total:
        n = len({r.arc_no for r in rows})
        return {"skipped": True, "reason": f"全部 {len(rows)} 章已有弧（{n} 条）", "arcs": n}

    specs = window_specs(first, total, core, tail)
    cuts: list[int] = []
    cache: dict[tuple[int, int], dict] = {}      # 已写过的弧内容（span → content）
    seg_next = 1
    for sp in specs:
        mx = _wait_for_summaries(db, book_name, sp["need"], timeout=wait_timeout)
        if mx < sp["lo"]:                        # 这一段摘要压根没到 → 停
            logger.warning(f"[plot_import] v3-stream：摘要只到第 {mx} 章，"
                           f"窗口 {sp['lo']}~{sp['core_end']} 无法处理，提前收尾")
            break
        by_no = {r.chapter_no: r for r in _load_rows()}
        visible = [n for n in range(sp["lo"], min(sp["need"], mx) + 1) if n in by_no]
        numbered = "\n".join(f"第{n}章：{by_no[n].summary}" for n in visible)
        try:
            raw = _arc_provider_post(db, provider, _cuts_prompt(sp["lo"], min(sp["need"], mx), numbered),
                                     max_tokens=(6000 if thinking else 2000),
                                     rate=rate, thinking=thinking)
            got = [int(x) for x in ((parse_json_loose(raw) or {}).get("cuts") or [])
                   if str(x).lstrip("-").isdigit()]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[plot_import] v3-stream Pass1 窗口 {sp['lo']} 失败："
                           f"{type(e).__name__}: {str(e)[:120]}")
            got = []
        cuts += [c for c in got
                 if sp["lo"] + CUT_TRUST_MARGIN <= c <= sp["core_end"] + CUT_TRUST_MARGIN
                 and sp["lo"] < c <= sp["need"]]
        logger.info(f"[plot_import] v3-stream 窗口 {sp['lo']}~{sp['core_end']}"
                    f"（摘要到 {mx}）：提 {len(got)} 刀 → 累计 {len(cuts)} 刀")
        # —— 立刻为"已定型弧"写内容（这一步才是耗时大头）——
        for arc in settled_arcs(cuts, sp["core_end"]):
            if arc in cache:
                continue
            content = _fetch_arc_content(db, by_no, arc, provider=provider,
                                         rate=rate, thinking=thinking)
            cache[arc] = content
            seg_next = _apply_arc_content(by_no, content, len(cache), seg_next)
            db.commit()
            logger.info(f"[plot_import] v3-stream 弧已定型并写好：{arc[0]}~{arc[1]} "
                        f"《{content['name']}》")
            if progress:
                progress(arc[1], len(cache))

    # ---------- 收尾：全局缝合 + 对账 ----------
    final = postcheck_arcs(arcs_from_cuts(stitch_cuts(cuts, total), total), total)[0]
    by_no = {r.chapter_no: r for r in _load_rows()}
    changed = [a for a in final if a not in cache]
    if changed or set(final) != set(cache):
        logger.info(f"[plot_import] v3-stream 收尾对账：最终 {len(final)} 条弧，"
                    f"需补算 {len(changed)} 条（其余 {len(final) - len(changed)} 条复用已写内容）")
        for r in by_no.values():                  # 清干净再按最终弧表重写（幂等）
            r.arc_no = r.arc_name = r.arc_summary = None
            r.arc_summary_raw = None
            r.segment_no = r.segment_summary = None
            r.segment_summary_raw = None
        db.commit()
        seg_next = 1
        for idx, arc in enumerate(final, start=1):
            content = cache.get(arc) or _fetch_arc_content(
                db, by_no, arc, provider=provider, rate=rate, thinking=thinking)
            cache[arc] = content
            seg_next = _apply_arc_content(by_no, content, idx, seg_next)
            db.commit()
        _, rep = postcheck_arcs(final, total)
    else:
        rep = postcheck_arcs(final, total)[1]
        logger.info("[plot_import] v3-stream 收尾对账：最终弧表与增量结果一致，无需重写")

    return {"arcs": len(final), "chapters": len(by_no), "cuts": len(stitch_cuts(cuts, total)),
            "stitch": rep, "provider": provider, "core": core, "tail": tail,
            "thinking": thinking, "streaming": True, "reused": len(final) - len(changed)}


def run_all_v3(db: Session, book_name: str, book_dir: str, *, provider: str = "modelscope",
               core: int = ARC_CORE_DEFAULT, tail: int = ARC_TAIL_DEFAULT,
               batch_size: int = 3, concurrency: int = 3, start: int = 1,
               end: int | None = None, force: bool = False, thinking: bool = True,
               progress=None) -> dict:
    """一体化入口：**章摘要（独立线程）∥ 弧阶段（当前线程）**。

    为什么能并行（用户洞察 + 实测）：两条线走**不同厂商、不同额度** ——
    章摘要 = 硅基流动 Qwen3-8B（受 5 万 TPM 预算约束、本来最慢）；弧 = 魔搭 Qwen3.8-Flash-Next（独立额度）
    ⇒ 并行**不互相抢额度**，且两者节奏相近（各约 90 秒 / 15 章）→ 弧的开销藏进摘要的等待里。

    摘要线程**自建 Session**（SQLAlchemy Session 非线程安全，绝不复用主线程的 db）；
    弧阶段每次查询前 `commit()` 刷新快照（见 `_wait_for_summaries` 的说明）。
    """
    from app.core import database as _db      # 模块引用（同 06 §2.2.1 的流式铁律）
    stats: dict = {}

    def _summarize():
        s = _db.SessionLocal()     # 注意：必须在 init_db() 之后（CLI 启动时已调用），否则是 None
        try:
            stats["summarize"] = import_chapters_batch(
                s, book_dir, book_name, batch_size=batch_size, concurrency=concurrency,
                start=start, end=end)
        except Exception as e:  # noqa: BLE001
            stats["summarize_error"] = f"{type(e).__name__}: {e}"
            logger.warning(f"[plot_import] all-v3 摘要线程失败：{type(e).__name__}: {e}")
        finally:
            s.close()

    expect_total = None
    try:
        chs = discover_chapters(book_dir)
        if chs:
            top = max(c["no"] for c in chs)
            expect_total = min(end, top) if end else top
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plot_import] all-v3 读取源目录失败（无法确定总章数）：{e}")

    th = threading.Thread(target=_summarize, daemon=True)
    th.start()
    try:
        arc = build_arcs_v3_streaming(db, book_name, provider=provider, core=core, tail=tail,
                                      force=force, thinking=thinking,
                                      expect_total=expect_total, progress=progress)
    finally:
        th.join()
    return {"summarize": stats.get("summarize"), "summarize_error": stats.get("summarize_error"),
            "arc": arc, "expect_total": expect_total}


def _arc_prompt(seg_list: list[dict]) -> str:
    """seg_list: [{no, summary, chapters:[...]}]"""
    lines = [
        f"段{s['no']}（第 {s['chapters'][0]}~{s['chapters'][-1]} 章）：{s['summary']}"
        for s in seg_list
    ]
    return (
        f"下面是一部小说连续 {len(seg_list)} 个情节段的概括。请把它们归并为若干「故事弧」。\n"
        "「故事弧」的定义：一个有完整冲突升级链的故事单元（通常含 2~6 个情节段），\n"
        "也就是读者感知到的「一个完整套路 / 一个爽点周期」（例如：金手指觉醒、冲突升级与备战、家族危机）。\n\n"
        "要求：\n"
        "1. 每个弧输出 80~150 字概括，讲清这条弧的起因 → 升级 → 转折 → 结果；\n"
        "2. 覆盖全部情节段，不重叠不遗漏；同一个弧里的段号必须连续；\n"
        "3. 给每个弧起一个 4~8 字的名称。\n\n"
        "只输出 JSON（不要 markdown 代码块、不要任何额外说明）：\n"
        '{"arcs":[{"segments":[段号...],"name":"弧名","summary":"概括"}]}\n\n'
        + "\n".join(lines)
    )


def merge_arcs(db: Session, book_name: str, *, rate: RateLimiter | None = None,
               max_tokens: int = 16000, chunk: int = 120,
               provider: str = "deepseek", force: bool = False) -> dict:
    """把情节段归并为故事弧（Phase 7.1 arc 层，2026-09-11）。

    **为什么需要这一层**：段（beat）是"事件粒度"，而模板需要的单元是"故事弧"
    （一个完整套路 = 一个爽点周期，8~15 章）。只有段层时，切出来的段"单独看没毛病、
    但不像一个完整篇章"—— 缺的就是这层归并。

    **模型升档**：Qwen3-8B 做局部概括（机械活）够用，但"看懂全局叙事结构"是强语义任务 →
    用 DeepSeek V4.1 Flash（输入几千 token，一次几厘钱）。

    `provider`（2026-09-15 加）：
      - `deepseek`（默认）：DeepSeek V4.1 Flash，付费但稳。
      - `modelscope`：魔搭 Qwen3.8-Flash-Next（125B/6B 激活，262K 上下文），
        用免费额度（200 次/天）干这笔活最划算 —— **注意它默认开思考，必须显式关**
        （见 `_ms_post` 说明）。
    两条路的 prompt / 解析 / 幂等逻辑完全一致，只有 HTTP 端点与思考字段不同。

    **幂等跳过（08-B8③，2026-09-16 加）**：该书全部有段的章**都已带弧** → 直接返回
    `skipped=True`，**不调 LLM**（`--stage all` 重跑全链路时最容易被这里白烧钱）。
    部分覆盖（增量灌了新段）→ **不跳**，全量重算 —— 新段必须与旧段一起重归并，
    弧的边界语义才正确（只归并新段会切错边界）。`force=True` 无视跳过强制重算（CLI --force）。
    数据幂等不变：每次重算先清空该书的旧 arc 标记。未覆盖的段兜底归入最后一个弧。
    """
    if provider not in ARC_PROVIDERS:
        raise ValueError(f"未知的 arc provider: {provider!r}（可选 {ARC_PROVIDERS}）")
    rows = (
        db.query(ChapterSummaryORM)
        .filter_by(book_name=book_name)
        .order_by(ChapterSummaryORM.chapter_no)
        .all()
    )
    rows = [r for r in rows if r.segment_no is not None]
    if not rows:
        return {"arcs": 0, "segments": 0, "error": "没有段数据（请先跑 --stage segment）"}

    covered = sum(1 for r in rows if r.arc_no is not None)
    if covered == len(rows) and not force:
        n_arcs = len({r.arc_no for r in rows})
        logger.info(f"[plot_import] arc 幂等跳过：{book_name} 全部 {len(rows)} 段已有弧"
                    f"（{n_arcs} 条），未调 LLM。force=True 可强制重算")
        return {"skipped": True,
                "reason": f"全部 {len(rows)} 段已有弧（{n_arcs} 条），无需重算",
                "arcs": n_arcs, "segments": len(rows)}
    if covered and covered < len(rows):
        logger.info(f"[plot_import] arc 增量重算：{covered}/{len(rows)} 段已有弧，"
                    f"新段须与旧段一起重归并 → 全量重算")

    segs: dict[int, list] = {}
    for r in rows:
        segs.setdefault(r.segment_no, []).append(r)
    seg_list = [
        {
            "no": no,
            "summary": srows[0].segment_summary or "。".join(x.summary for x in srows)[:200],
            "chapters": [x.chapter_no for x in srows],
        }
        for no, srows in sorted(segs.items())
    ]

    for r in rows:          # 重算前清旧标记
        r.arc_no = None
        r.arc_name = None
        r.arc_summary = None
        # ⚠️ 必须同时清 raw（2026-09-14 修）：apply_replacement 优先用 *_raw 作源，
        # 若旧 raw 留着，弧后补扫会把**上一次的弧文本**写回来覆盖新生成的 arc_summary。
        r.arc_summary_raw = None
    db.commit()

    # ── provider 分派：只有端点 / 思考字段 / 记账 scene 不同，其余逻辑共用 ──
    if provider == "modelscope":
        key = ms_key(db)
        if not key:
            raise RuntimeError("未配置魔搭 Key（app_configs.llm.modelscope_key）")
        _post = _ms_post
        scene = "ms_arc"
    else:
        key = ds_key(db)
        if not key:
            raise RuntimeError("未配置 DeepSeek Key（app_configs.llm.deepseek_key）")
        _post = _ds_post
        scene = "ds_arc"
    rate = rate or RateLimiter()

    # 分批归并：段数多了单次输出会超过 max_tokens 被**截断** → JSON 解析失败
    # （2026-09-11 实测教训：斗破 84 段一次归并，max_tokens=3000 直接截断，arcs 全丢）
    batches = [seg_list[i:i + max(1, chunk)] for i in range(0, len(seg_list), max(1, chunk))]
    logger.info(f"[plot_import] arc 归并 provider={provider}｜{len(seg_list)} 段 → "
                f"{len(batches)} 批｜max_tokens={max_tokens}")
    arcs: list[dict] = []
    raws: list[str] = []
    for b in batches:
        raw = _post(key, _arc_prompt(b), max_tokens=max_tokens, rate=rate,
                    on_usage=make_usage_cb(scene))
        raws.append(raw)
        got = (parse_json_loose(raw) or {}).get("arcs") or []
        if not got:
            logger.warning(f"[plot_import] arc 归并批次（段 {b[0]['no']}~{b[-1]['no']}）"
                           f"未解析出 JSON: {raw[:150]!r}")
        arcs.extend(got)
    if not arcs:
        logger.warning(f"[plot_import] arc 归并整体未解析出 JSON（段数据保留不动）: "
                       f"{raws[0][:160]!r}" if raws else "")
        return {"arcs": 0, "segments": len(seg_list), "error": "JSON 解析失败",
                "raw_head": (raws[0] if raws else "")[:200]}

    covered: set[int] = set()
    n_arcs = 0
    for arc in arcs:
        seg_nos: list[int] = []
        for x in arc.get("segments") or []:
            try:
                seg_nos.append(int(x))
            except (TypeError, ValueError):
                continue
        name = (arc.get("name") or "").strip()
        summ = (arc.get("summary") or "").strip()
        targets = [sn for sn in seg_nos if sn in segs and sn not in covered]
        if not targets:
            continue
        n_arcs += 1
        for sn in targets:
            covered.add(sn)
            for r in segs[sn]:
                r.arc_no = n_arcs
                r.arc_name = name or None
                r.arc_summary = summ or None
    leftover = [sn for sn in segs if sn not in covered]
    if leftover:
        if n_arcs == 0:
            n_arcs = 1
        for sn in leftover:          # 兜底：保证全覆盖（否则报告里会丢段）
            for r in segs[sn]:
                r.arc_no = n_arcs
        logger.warning(f"[plot_import] {len(leftover)} 个段未被归并，已兜底归入最后一个弧")
    db.commit()
    return {"arcs": n_arcs, "segments": len(seg_list),
            "covered": len(covered), "leftover": len(leftover)}
