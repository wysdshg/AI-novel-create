"""OpenAI 兼容适配器。

覆盖厂商：openai / deepseek / qwen / kimi / ollama / custom / ernie / spark /
siliconflow（硅基流动）/ nvidia（英伟达 NIM）/ zhipu（智谱 GLM）。
这些厂商均提供与 OpenAI 一致的 /chat/completions 端点（部分需在 api_base 指向其兼容地址）。

实现原则：
- 仅用标准库 urllib，零第三方依赖，避免污染隔离 venv。
- chat() 同步返回完整文本；stream() 同步生成器逐段 yield（供章节生成 SSE 使用）；
  astream() 异步包装 stream() 以满足抽象基类接口。
"""
import logging
import json
import re
import socket
import urllib.request
import urllib.error

from app.core.gateway.base import BaseModelAdapter


logger = logging.getLogger(__name__)


def _build_request(url: str, payload: dict, api_key: str) -> urllib.request.Request:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    return req


def _http_post(api_base: str, api_key: str, payload: dict):
    """返回 (status:int, body:str)。"""
    base = (api_base or "").rstrip("/")
    url = f"{base}/chat/completions"
    req = _build_request(url, payload, api_key)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.getcode(), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        # 网络层异常 → 返回 status=-1，调用方据此抛「模型调用失败(status=-1): <msg>」给用户。
        # 用户能看到原因，但看不到发生在哪一步（DNS？TLS？超时？）——补日志留痕（Phase 3.5）
        logger.warning(f"[openai_compat] HTTP 请求异常 url={url}: {type(e).__name__}: {e}")
        return -1, str(e)


class OpenAICompatibleAdapter(BaseModelAdapter):
    # 仅这些厂商的 OpenAI 兼容端点认 chat_template_kwargs 这一非标准扩展字段
    # 注意：NVIDIA NIM 上的 GLM-5.2 实际走顶层 thinking.type（与 zhipu 相同），
    # 用 chat_template_kwargs 会导致流式下 content 为空、正文全进 reasoning_content。
    _CHAT_TEMPLATE_KWARGS_VENDORS = {"qwen"}
    # 用顶层 thinking.type 控制思考的厂商：deepseek / zhipu / nvidia（NVIDIA NIM 的 GLM 系同 zhipu）。
    # 注意：绝不能用 chat_template_kwargs（会导致流式 content 为空、正文全进 reasoning_content）。
    # OpenAI 推理模型（o1/o3/o4 系列）用 max_completion_tokens，且不支持 temperature/top_p
    _OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4")

    def _payload(self, messages, **params):
        # 思考开关：本次请求参数 > 模型配置 > True
        enable_thinking = params.get("enable_thinking")
        if enable_thinking is None:
            enable_thinking = self.config.get("enable_thinking", True)

        vendor = (self.config.get("vendor") or "").lower()
        model = (self.config.get("model_name") or "").lower()
        api_base = (self.config.get("api_base") or "").lower()
        # 安全网：ModelScope 的 Qwen3.x 开 thinking 后，流式下正文 content 为空，
        # 或把英文 reasoning（harmless/helpful/safe 之类 RLHF 推理词）塞进 content 污染正文、
        # 导致正文变成「无标点长段 + 英文推理词 + 同义反复循环」。
        # 关键：默认模型在库里标的是 vendor="custom"（不是 "qwen"），但端点就是 ModelScope 的
        # Qwen3.x，因此按 model+api_base 命中，而非仅按 vendor=="qwen"（否则该组合被完全绕过）。
        # 2026-09-09 放宽：qwen3.5 字面匹配 → qwen3.x 正则（Qwen3.8-Flash-Next 实测同病：
        # 无标点长段 1870 字、首字 407s）。
        # 2026-09-09 二改：强关改为「仅默认值」——调用方显式传 enable_thinking 时尊重之
        # （路由层 _content_only_stream 已保证 reasoning 不进正文，此处不再二次覆盖）。
        _broken_ms_qwen35 = (
            bool(re.search(r"qwen3\.\d", model)) and "modelscope" in api_base
        )
        if _broken_ms_qwen35 and enable_thinking is None:
            enable_thinking = False

        max_tokens = params.get("max_tokens", self.config.get("max_tokens", 6000))

        payload = {
            "model": self.config.get("model_name", ""),
            "messages": messages,
            "temperature": params.get("temperature", self.config.get("temperature", 0.4)),
            "top_p": params.get("top_p", self.config.get("top_p", 0.9)),
            "max_tokens": max_tokens,
            "stream": False,
        }

        # 重复惩罚（OpenAI 兼容标准字段）：专治长文本复读循环。
        # frequency_penalty 惩罚「出现过的 token」的再次出现频率（建议 0.3~0.6）；
        # presence_penalty 惩罚「出现过的新 token」进入候选（建议 0~0.4）。
        # 章节生成长文本默认 0.4/0.4（chapter.py 按 vendor 注入 config）。
        frequency_penalty = params.get("frequency_penalty", self.config.get("frequency_penalty"))
        if frequency_penalty is not None:
            payload["frequency_penalty"] = float(frequency_penalty)
        presence_penalty = params.get("presence_penalty", self.config.get("presence_penalty"))
        if presence_penalty is not None:
            payload["presence_penalty"] = float(presence_penalty)

        # ── 思考模式字段：各厂商差异巨大，混用非标准字段会直接 400 ──
        if vendor in self._CHAT_TEMPLATE_KWARGS_VENDORS or _broken_ms_qwen35:
            # Qwen / 以及自定义厂商打到 ModelScope Qwen3.5 端点：认 chat_template_kwargs.enable_thinking。
            # 关键：custom 厂商默认不发任何思考字段，会让 ModelScope Qwen3.5 按默认 thinking=ON 跑，
            # reasoning 泄漏进 content（正文出现无标点长段 + harmless/helpful 等英文推理词）。
            # 显式发 chat_template_kwargs.enable_thinking=false 才能真正关掉思考、拿到干净中文 content。
            payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
        elif vendor in ("zhipu", "nvidia"):
            # 智谱 GLM-4.5+ / 英伟达 NIM GLM-5.2：顶层 thinking.type（enabled/disabled 均支持）。
            # 修复项：此前 nvidia 漏配、走 fall-through 未发任何思考字段，导致 chapter.py 的
            # 「强制开思考防复读」对 GLM 完全失效 —— 模型不思考硬写 3000+ 字长文，
            # 易中英混写、整段不分段。现在显式发 thinking.type，与 zhipu 同款。
            payload["thinking"] = {"type": "enabled" if enable_thinking else "disabled"}
        elif vendor == "deepseek":
            # DeepSeek：顶层 thinking.type；disabled 仅在 reasoning_effort=low 时合法
            if enable_thinking:
                payload["thinking"] = {"type": "enabled"}
            else:
                payload["thinking"] = {"type": "disabled"}
                payload["reasoning_effort"] = "low"
        # 其余厂商（openai/kimi/ernie/spark/siliconflow/custom/ollama）：不发送任何思考控制字段，
        # 使用厂商默认行为，避免 “additional properties not allowed” 400。

        # ── OpenAI 推理模型特例 ──
        if vendor == "openai" and model.startswith(self._OPENAI_REASONING_PREFIXES):
            # o1/o3/o4 用 max_completion_tokens（max_tokens 会 TypeError），且不接受 temperature/top_p
            payload.pop("max_tokens", None)
            payload["max_completion_tokens"] = max_tokens
            payload.pop("temperature", None)
            payload.pop("top_p", None)

        # ===== 调试日志：真正发给模型的请求体（发送前打印）=====
        try:
            def _cut(s, n=300):
                if not isinstance(s, str):
                    s = str(s)
                return s if len(s) <= n else s[:n] + f"…(共{len(s)}字)"
            logger.info("\n===== [DEBUG] LLM 请求体 =====")
            logger.info(f"  vendor={vendor!r} model={self.config.get('model_name')!r} stream={payload.get('stream')}")
            logger.info(f"  url={(self.config.get('api_base','') or '').rstrip('/')}/chat/completions")
            logger.info(f"  messages 条数={len(messages)}")
            for i, m in enumerate(messages):
                role = m.get("role")
                c = m.get("content", "")
                if isinstance(c, list):
                    c = str(c)
                # system 提示词（可能上万字）不截断，便于排查；其余消息截断到 300 字
                limit = 20000 if role == "system" else 300
                logger.info(f"  [{i}] {role}: {_cut(c, limit)}")
            extra = {k: v for k, v in payload.items() if k not in ("model", "messages", "stream")}
            logger.info(f"  其他参数={extra}")
            logger.info("===== [DEBUG] END =====\n")
        except Exception as e:  # noqa: BLE001
            # 仅调试日志本身出问题（如极端大的日志/编码异常）——绝不能因此影响真实请求，
            # 但要留痕：否则"打开了 DEBUG 却什么都没打印"将无从解释（Phase 3.5）
            logger.warning(f"[openai_compat] 调试日志打印失败（不影响请求）: {type(e).__name__}: {e}")

        return payload

    def chat(self, messages, **params) -> str:
        payload = self._payload(messages, **params)
        status, body = _http_post(self.config.get("api_base", ""), self.config.get("api_key", ""), payload)
        if status != 200:
            if status == 429:
                raise RuntimeError("请求过于频繁，请稍后再试")
            raise RuntimeError(f"模型调用失败(status={status}): {body[:300]}")
        try:
            data = json.loads(body)
            # Phase 4.2：非流式响应通常带 usage，直接取（流式多数厂商不带）
            self.last_usage = self.normalize_usage(data.get("usage"))
            msg = data["choices"][0]["message"]
            # content 即正文；reasoning/thinking 是思考过程，不混入正文。
            # 兜底：部分厂商（NVIDIA NIM GLM-5.2）非流式也把正文塞进 reasoning_content/reasoning，
            # 此时 content 为空；必须把 reasoning 作为正文输出，否则前端正文区域空白。
            raw_content = msg.get("content")
            if isinstance(raw_content, list):
                # OpenAI 新内容格式：[{"type":"text","text":"..."}, ...]
                content = "".join(p.get("text", "") for p in raw_content if isinstance(p, dict))
            else:
                content = (raw_content or "")
            if content:
                return content
            # 兜底：部分厂商把正文塞进 reasoning/thinking 字段（NVIDIA NIM GLM-5.2 等）
            for key in ("reasoning_content", "reasoning", "thinking", "thought"):
                val = msg.get(key)
                if val:
                    return val
            # 都没有：打印响应体片段便于排查，然后返回空
            logger.info(f"[openai_compat.chat] 返回为空: model={self.config.get('model_name')!r} body={body[:1000]!r}")
            return ""
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"解析模型响应失败: {body[:300]}") from e

    def stream(self, messages, **params):
        payload = self._payload(messages, **params)
        payload["stream"] = True
        base = (self.config.get("api_base", "") or "").rstrip("/")
        url = f"{base}/chat/completions"
        req = _build_request(url, payload, self.config.get("api_key", ""))
        # 2026-09-10 兜底：魔搭(ModelScope)端点的 GLM-5.3-Flash 实测把全部输出
        # （含正文）塞进 reasoning_content，content 恒为空（773 帧 content_len=0，
        # 与 NVIDIA NIM GLM-5.2 同款行为；thinking.type 字段被魔搭静默忽略，关不掉）。
        # chat() 与 stream_with_thinking() 都有 reasoning 兜底，唯独本方法漏了，
        # 导致章节生成 0 chunk（流"正常"走完、正文空白）。Qwen3.x 不受影响：
        # 它 content 正常出正文，_content_seen=True 永远进不了兜底分支。
        _reasoning_buffer = []
        _content_seen = False
        try:
            # 超时 90s（原 240s）：URL 级 timeout 是「整条流」的读超时，模型卡住时
            # 会让用户干等 4 分钟才见到报错。90s 足够云端首字+长文分帧，卡住也能较快失败。
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        # Phase 4.2：部分厂商在**流式末帧**带 usage（此时 choices 为空数组）。
                        # 因此必须先取 usage，再安全地取 choices —— 否则空数组会 IndexError，
                        # 被下面的容错分支当成"畸形帧"跳过并记 warning（噪音）。
                        if obj.get("usage"):
                            self.last_usage = self.normalize_usage(obj["usage"])
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0]["delta"]
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        piece = delta.get("content") or ""
                        if piece:
                            _content_seen = True
                            yield piece
                        elif reasoning:
                            _reasoning_buffer.append(reasoning)
                    except Exception as e:  # noqa: BLE001
                        # 单帧畸形容错跳过（SSE 流不能因一帧坏掉而整条中断）。
                        # 但要留痕：若某厂商改了响应结构，这里会连续刷同一告警，是唯一线索（Phase 3.5）
                        logger.warning(
                            f"[openai_compat.stream] 跳过无法解析的 SSE 帧: "
                            f"{type(e).__name__}: {e}; data={data[:200]!r}"
                        )
                        continue
            if not _content_seen and _reasoning_buffer:
                fallback = "".join(_reasoning_buffer)
                # Qwen3.x 式英文分析泄漏（harmless/helpful 等 RLHF 推理词）不能当正文，
                # 英文占比 >15% 时拒绝兜底（与 stream_with_thinking 同款防线）
                english_tokens = re.findall(r"[A-Za-z]{3,}", fallback)
                if len("".join(english_tokens)) > len(fallback) * 0.15:
                    logger.info(
                        "[openai_compat.stream] 正文为空且 reasoning 主要为英文分析，"
                        f"疑似 thinking 泄漏，拒绝兜底输出。model={self.config.get('model_name')!r}",
                    )
                    yield "\n[生成异常：模型仅返回思考分析，未输出正文。请尝试关闭思考模式或更换模型。]"
                else:
                    yield fallback
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            yield f"\n[模型调用失败 status={e.code}: {detail[:200]}]"
        except Exception as e:  # noqa: BLE001
            # 超时（socket.timeout / URLError.timeout）单独给一句人话，别让用户看堆栈
            # 但服务端必须留堆栈：流式失败时前端只拿到一句短文案（Phase 3.5）
            logger.exception(f"[openai_compat.stream] 流式调用异常 model={self.config.get('model_name')!r}")
            if isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in str(e).lower():
                yield "\n[模型响应超时（90 秒无数据）。多为模型端卡住或网络不稳，建议重试；也可先关闭思考模式。]"
            else:
                yield f"\n[模型调用异常: {str(e)[:200]}]"

    def stream_with_thinking(self, messages, **params):
        """流式返回「思考过程 + 正文」两种片段（供思考可见化展示）。

        yield ("thinking", 思考片段) 或 ("content", 正文片段)。
        思考来自推理模型的 reasoning_content / reasoning 字段，正文来自 content。
        默认 stream() 保持纯正文（兼容旧调用方）；需要思考透传时用本方法。
        """
        payload = self._payload(messages, **params)
        payload["stream"] = True
        base = (self.config.get("api_base", "") or "").rstrip("/")
        url = f"{base}/chat/completions"
        req = _build_request(url, payload, self.config.get("api_key", ""))
        # 兜底：某些模型（如 NVIDIA NIM 的 GLM-5.2）在流式下会把正文也塞进 reasoning_content，
        # content 始终为空。此时必须把 reasoning 也作为正文输出，否则前端正文区域空白。
        _reasoning_buffer = []
        _content_seen = False
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        # 同 stream()：先取 usage，再安全取 choices（末帧 choices 为空）
                        if obj.get("usage"):
                            self.last_usage = self.normalize_usage(obj["usage"])
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0]["delta"]
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        piece = delta.get("content") or ""
                        if reasoning:
                            _reasoning_buffer.append(reasoning)
                            yield ("thinking", reasoning)
                        if piece:
                            _content_seen = True
                            yield ("content", piece)
                    except Exception as e:  # noqa: BLE001
                        # 同 stream()：容错跳过单帧，但留痕（Phase 3.5）
                        logger.warning(
                            f"[openai_compat.stream_with_thinking] 跳过无法解析的 SSE 帧: "
                            f"{type(e).__name__}: {e}; data={data[:200]!r}"
                        )
                        continue
            # 流正常结束：若正文始终未出现，把全部 reasoning 作为正文一次性兜底输出。
            # 但若是英文分析（如 ModelScope Qwen3.5 thinking 泄漏），直接输出会污染正文，改为拒绝。
            if not _content_seen and _reasoning_buffer:
                fallback_content = "".join(_reasoning_buffer)
                english_tokens = re.findall(r"[A-Za-z]{3,}", fallback_content)
                if len("".join(english_tokens)) > len(fallback_content) * 0.15:
                    logger.info(
                        "[openai_compat.stream_with_thinking] 正文为空且 reasoning 主要为英文分析，"
                        f"疑似 thinking 泄漏，拒绝兜底输出。model={self.config.get('model_name')!r}"
                    )
                    yield ("content", "\n[生成异常：模型仅返回思考分析，未输出正文。请尝试关闭思考模式或更换模型。]")
                else:
                    yield ("content", fallback_content)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            yield ("content", f"\n[模型调用失败 status={e.code}: {detail[:200]}]")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[openai_compat.stream_with_thinking] 流式调用异常 model={self.config.get('model_name')!r}")
            yield ("content", f"\n[模型调用异常: {str(e)[:200]}]")

    def stream_dual(self, messages, **params):
        """流式返回「思考 / 正文」双通道，且**绝不做 reasoning→正文兜底**。

        与 `stream_with_thinking` 的区别只有一处，但这一处是致命的：
        后者在「流结束且 content 从未出现」时会把 reasoning_buffer 当正文输出
        （仅用 15% 英文占比拦一道）。这条兜底对**中文思考**的模型完全放行——
        Qwen3.8-Flash-Next 的思考是中文的，一旦命中就会把整段思考当成小说正文落库，
        正是 2026-09-09 那次「输出里一直带着 reasoning」的老坑。

        章节生成必须走本方法：thinking 帧仅用于前端保活/进度展示，
        正文**只**认 content 帧；若模型最终没吐出任何 content，就什么都不 yield，
        交给上层走「正文为空 → 拒绝落库」分支，而不是拿思考内容鱼目混珠。

        yield ("thinking", 思考片段) 或 ("content", 正文片段)。
        """
        payload = self._payload(messages, **params)
        payload["stream"] = True
        base = (self.config.get("api_base", "") or "").rstrip("/")
        url = f"{base}/chat/completions"
        req = _build_request(url, payload, self.config.get("api_key", ""))
        _content_seen = False
        _reasoning_chars = 0
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        if obj.get("usage"):
                            self.last_usage = self.normalize_usage(obj["usage"])
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0]["delta"]
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        piece = delta.get("content") or ""
                        if reasoning:
                            _reasoning_chars += len(reasoning)
                            yield ("thinking", reasoning)
                        if piece:
                            _content_seen = True
                            yield ("content", piece)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            f"[openai_compat.stream_dual] 跳过无法解析的 SSE 帧: "
                            f"{type(e).__name__}: {e}; data={data[:200]!r}"
                        )
                        continue
            # 🔴 关键差异：这里**不**做 reasoning 兜底。
            # 只留痕，让上层按「正文为空」处理（拒绝落库），绝不拿思考内容冒充正文。
            if not _content_seen and _reasoning_chars:
                logger.warning(
                    "[openai_compat.stream_dual] 模型全程未输出 content，仅产出 "
                    f"{_reasoning_chars} 字思考内容 → 按正文为空处理（拒绝落库，不做兜底）。"
                    f"model={self.config.get('model_name')!r}"
                )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            yield ("content", f"\n[模型调用失败 status={e.code}: {detail[:200]}]")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[openai_compat.stream_dual] 流式调用异常 model={self.config.get('model_name')!r}")
            if isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in str(e).lower():
                yield ("content", "\n[模型响应超时（240 秒无数据）。多为模型端卡住或网络不稳，建议重试；也可先关闭思考模式。]")
            else:
                yield ("content", f"\n[模型调用异常: {str(e)[:200]}]")

    async def astream(self, messages, **params):
        for chunk in self.stream(messages, **params):
            yield chunk

    def test_connection(self) -> bool:
        payload = self._payload([{"role": "user", "content": "hi"}], max_tokens=1)
        status, _ = _http_post(self.config.get("api_base", ""), self.config.get("api_key", ""), payload)
        return status == 200
