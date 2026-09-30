"""Rerank 客户端（A4）：硅基流动 BAAI/bge-reranker-v2-m3，纯 stdlib 零新依赖。

定位：RK（retrieve-then-rerank）的第二阶段。向量/关键词召回只保证「大致相关」，
rerank 用交叉编码器（query 与 doc 拼一起进模型）逐对打分，区分度远高于
双塔 embedding——实测同一 query 下，正确项 0.128 / 次相关 0.035 / 无关 0.010~0.0，
而 bge-m3 余弦在同场景下密集在 0.51~0.53（差 0.007 无法排序）。

设计：
- 与 embedding_client 共用 key（同一硅基流动账号）：env NA_SILICONFLOW_KEY >
  app_configs retrieval.siliconflow_key > 模型配置表 vendor=siliconflow；
- 单请求 ≤64 文档（服务端限制），超量自动分批；
- 失败一律抛 RerankError，调用方（vector_index.rerank）降级为保留 RRF 原序。
"""
import json
import logging
import os
import urllib.error
import urllib.request

from app.services.embedding_client import get_api_key as _get_embed_key

logger = logging.getLogger(__name__)

BASE = "https://api.siliconflow.cn/v1"
GATEWAY_BASE = "http://127.0.0.1:9377/v1"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
_MAX_BATCH = 64
# 单文档截断：reranker 输入越长越慢，块级文本 500 字已够；防御性留 2000
_MAX_DOC_CHARS = 2000


class RerankError(RuntimeError):
    """rerank 不可用（key 缺失 / 网络失败 / 响应异常）。调用方应降级保留原序。"""


def get_api_key(db=None) -> str:
    """复用 embedding 的 key 查找链（同一硅基流动账号）。"""
    return _get_embed_key(db)


def _resolve_endpoint(db=None) -> tuple[str, str]:
    """返回 (base, key)：网关模式走统一网关（/v1/rerank 已实测可用），否则硅基直连。

    判定逻辑与 embedding_client._resolve_endpoint 同款（llm.use_gateway + llm.gateway_key，
    缺 Key 回落直连并告警）。直连保持 env > app_configs > 模型配置表 三级查找不变。
    """
    if db is not None:
        try:
            from app.services import app_config
            if app_config.get(db, "llm.use_gateway", False):
                gk = str(app_config.get(db, "llm.gateway_key", "") or "").strip()
                if gk:
                    return GATEWAY_BASE, gk
                logger.warning("[rerank_client] use_gateway=true 但未配 llm.gateway_key，回落硅基直连")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[rerank_client] 读取网关开关失败，按直连处理: {type(e).__name__}: {e}")
    return BASE, get_api_key(db)


def rerank_model_name() -> str:
    return (os.environ.get("NA_RERANK_MODEL") or DEFAULT_MODEL).strip()


def _post_rerank(base: str, query: str, docs: list[str], api_key: str, top_n: int | None) -> list[dict]:
    """单次请求（调用方保证 len(docs) ≤ _MAX_BATCH）。

    返回 [{"index": 原下标, "relevance_score": float}, ...]，已按分数降序。
    """
    payload = {
        "model": rerank_model_name(),
        "query": query[:_MAX_DOC_CHARS],
        "documents": [d[:_MAX_DOC_CHARS] for d in docs],
    }
    if top_n is not None:
        payload["top_n"] = min(top_n, len(docs))
    req = urllib.request.Request(
        f"{base}/rerank",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RerankError(f"rerank HTTP {e.code}: {detail}") from e
    except Exception as e:  # noqa: BLE001
        raise RerankError(f"rerank 请求失败: {type(e).__name__}: {e}") from e

    items = data.get("results") or []
    out = []
    for it in items:
        idx = it.get("index")
        if idx is None or not (0 <= idx < len(docs)):
            continue
        out.append({"index": int(idx), "relevance_score": float(it.get("relevance_score") or 0.0)})
    out.sort(key=lambda x: -x["relevance_score"])
    return out


def rerank(query: str, docs: list[str], *, api_key: str | None = None, db=None,
           top_n: int | None = None) -> list[dict]:
    """对候选文档按与 query 的相关性重排。

    返回 [{"index": 原下标, "relevance_score": float}, ...] 降序。
    传入空 docs 返回 []；key 缺失抛 RerankError。
    """
    if not docs:
        return []
    if api_key:
        base, key = BASE, api_key.strip()
    else:
        base, key = _resolve_endpoint(db)
    if not key:
        raise RerankError("未配置硅基流动 API Key（rerank 与 embedding 共用；网关模式需 llm.gateway_key）")
    merged: list[dict] = []
    for i in range(0, len(docs), _MAX_BATCH):
        batch = docs[i:i + _MAX_BATCH]
        # 分批时 top_n 按批内外分别截断会漏项，故仅单批时下传
        part = _post_rerank(base, query, batch, key,
                            top_n if len(docs) <= _MAX_BATCH else None)
        for it in part:
            it["index"] += i  # 还原全局下标
        merged.extend(part)
    merged.sort(key=lambda x: -x["relevance_score"])
    if top_n is not None:
        merged = merged[:top_n]
    return merged
