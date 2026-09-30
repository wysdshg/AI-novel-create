"""Embedding 客户端（A1）：硅基流动 BAAI/bge-m3，纯 stdlib 零新依赖。

设计要点：
- API key 查找顺序：环境变量 NA_SILICONFLOW_KEY > app_configs 表
  `retrieval.siliconflow_key` > 模型配置表 vendor=siliconflow 的可用记录。
  三处都没有 -> EmbeddingError，调用方（vector_index）自动降级为不索引。
- 批量上限：单请求 ≤16 条、单条文本截断 2000 字（bge-m3 支持长文，
  但切块后单块约 500 字，这里只做防御性截断）。
- 向量统一 L2 归一化（bge 系一般已归一，这里兜底），归一化后 L2 距离
  与余弦相似度单调等价——vector_store 的 vec0(L2) 与暴力余弦可混用。
"""
import json
import logging
import math
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

BASE = "https://api.siliconflow.cn/v1"
GATEWAY_BASE = "http://127.0.0.1:9377/v1"
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_DIM = 1024
_MAX_BATCH = 16
_MAX_TEXT_CHARS = 2000


class EmbeddingError(RuntimeError):
    """embedding 不可用（key 缺失 / 网络失败 / 响应异常）。调用方应降级而非中断。"""


def _resolve_endpoint(db=None) -> tuple[str, str]:
    """返回 (base, key)。

    网关模式（app_configs `llm.use_gateway`=true 且已配 `llm.gateway_key`，2026-09-25）
    走本地统一网关（`/v1/embeddings` 已实测可用，dim=1024 一致）；网关 Key 缺失回落
    硅基直连并告警。直连保持原三级查找链（env > app_configs > 模型配置表）不变。
    注意：走网关后 embedding 用量计入网关硅基渠道的 token 窗口（与概括共享
    Key数×5万/min），网关按 Key 自动轮换。
    """
    if db is not None:
        try:
            from app.services import app_config
            if app_config.get(db, "llm.use_gateway", False):
                gk = str(app_config.get(db, "llm.gateway_key", "") or "").strip()
                if gk:
                    return GATEWAY_BASE, gk
                logger.warning("[embedding_client] use_gateway=true 但未配 llm.gateway_key，回落硅基直连")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[embedding_client] 读取网关开关失败，按直连处理: {type(e).__name__}: {e}")
    return BASE, get_api_key(db)


def get_api_key(db=None) -> str:
    """按 env > app_configs > 模型配置表 的顺序找硅基流动 key。找不到返回空串。"""
    key = (os.environ.get("NA_SILICONFLOW_KEY") or "").strip()
    if key:
        return key
    if db is not None:
        try:
            from app.services import app_config
            key = str(app_config.get(db, "retrieval.siliconflow_key", "") or "").strip()
            if key:
                return key
            from app.models.orm import ModelConfigORM
            row = (
                db.query(ModelConfigORM)
                .filter(ModelConfigORM.vendor == "siliconflow")
                .filter(ModelConfigORM.status == "active")
                .first()
            )
            if row and (row.api_key or "").strip():
                return row.api_key.strip()
        except Exception as e:  # noqa: BLE001
            # 第三级查找（模型配置表）失败 → 当作没找到，返回空串由上层降级为不索引。
            # 必须留痕：三级 key 查找全空时用户看到的是"检索不工作"，日志要能区分
            # 「真没配 key」与「查表就报错了」（Phase 3.5）
            logger.warning(f"[embedding_client] 从模型配置表查 siliconflow key 失败: {type(e).__name__}: {e}")
    return ""


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed_model_name() -> str:
    return (os.environ.get("NA_EMBED_MODEL") or DEFAULT_MODEL).strip()


def _post_embeddings(base: str, texts: list[str], api_key: str) -> list[list[float]]:
    """单次请求（调用方保证 batch ≤ _MAX_BATCH）。返回与 texts 等长的向量列表。"""
    payload = {
        "model": embed_model_name(),
        "input": [t[:_MAX_TEXT_CHARS] for t in texts],
        "encoding_format": "float",
    }
    req = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise EmbeddingError(f"embeddings HTTP {e.code}: {detail}") from e
    except Exception as e:  # noqa: BLE001
        raise EmbeddingError(f"embeddings 请求失败: {type(e).__name__}: {e}") from e

    items = data.get("data") or []
    if len(items) != len(texts):
        raise EmbeddingError(f"embeddings 返回条数不符: 期望 {len(texts)} 实得 {len(items)}")
    # 按 index 排序（部分实现不保证顺序）
    items.sort(key=lambda it: it.get("index", 0))
    vecs = []
    for it in items:
        v = it.get("embedding") or []
        if len(v) != DEFAULT_DIM:
            raise EmbeddingError(f"embedding 维度不符: 期望 {DEFAULT_DIM} 实得 {len(v)}")
        vecs.append(_l2_normalize([float(x) for x in v]))
    return vecs


def embed_texts(texts: list[str], *, api_key: str | None = None, db=None) -> list[list[float]]:
    """批量向量化。自动分批（≤16 条/请求），顺序与输入一致。全部 L2 归一化。

    端点选择：显式传 `api_key` → 硅基直连（语义不变）；否则按 `_resolve_endpoint`
    自动适配（网关模式走统一网关，一把 Key）。
    """
    if not texts:
        return []
    if api_key:
        base, key = BASE, api_key.strip()
    else:
        base, key = _resolve_endpoint(db)
    if not key:
        raise EmbeddingError(
            "未配置硅基流动 API Key（env NA_SILICONFLOW_KEY 或 app_configs retrieval.siliconflow_key；"
            "网关模式需 app_configs llm.gateway_key）")
    out: list[list[float]] = []
    for i in range(0, len(texts), _MAX_BATCH):
        out.extend(_post_embeddings(base, [t for t in texts[i:i + _MAX_BATCH]], key))
    return out
