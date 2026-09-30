"""A4 rerank 重排单测。

rerank 全部 mock，不触网、不依赖 key。
关键契约：
- rerank 客户端：解析/排序/分批下标还原/空输入/top_n/key 缺失抛 RerankError；
- pick_relevant 集成：rerank 生效时按 rerank 序取 top_k；rerank 失败时**静默降级为 RRF 原序**；
- 候选数 ≤ top_k 时不调 rerank（省一次 API 调用）；
- rerank 分落在明细 rerank_score 字段。
"""
import pytest

from app.services import rerank_client
from app.services import vector_index
from app.services.vector_store import SearchHit
from app.schemas.reference import ReferenceDocCreate
from app.services.reference_crud import pick_relevant, create_reference


def _mk(db, pid, name, content):
    return create_reference(db, pid, ReferenceDocCreate(
        filename=name, content_text=content, content_type="text/plain"))


# ---------------- rerank_client 单元 ----------------

def test_rerank_parses_and_sorts(monkeypatch):
    """返回按 relevance_score 降序，index 正确。"""
    def fake_post(base, query, docs, key, top_n):   # 2026-09-25 网关适配：base 前置
        assert base in (rerank_client.BASE, rerank_client.GATEWAY_BASE)
        return [{"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.1}]
    monkeypatch.setattr(rerank_client, "_post_rerank", fake_post)
    out = rerank_client.rerank("q", ["a", "b", "c"], api_key="k")
    assert [x["index"] for x in out] == [2, 0]
    assert out[0]["relevance_score"] == 0.9


def test_rerank_empty_docs_returns_empty():
    assert rerank_client.rerank("q", [], api_key="k") == []


def test_rerank_missing_key_raises(monkeypatch):
    monkeypatch.delenv("NA_SILICONFLOW_KEY", raising=False)
    monkeypatch.setattr(rerank_client, "get_api_key", lambda db=None: "")
    with pytest.raises(rerank_client.RerankError):
        rerank_client.rerank("q", ["a"], api_key=None)


def test_rerank_batches_restore_global_index(monkeypatch):
    """超 64 文档分批时，第二批的 index 要加偏移还原成全局下标。"""
    monkeypatch.setattr(rerank_client, "_MAX_BATCH", 2)
    calls = []

    def fake_post(base, query, docs, key, top_n):   # 2026-09-25 网关适配：base 前置
        calls.append(len(docs))
        return [{"index": i, "relevance_score": 1.0 - i * 0.1}
                for i in range(len(docs))]
    monkeypatch.setattr(rerank_client, "_post_rerank", fake_post)
    out = rerank_client.rerank("q", ["a", "b", "c", "d", "e"], api_key="k")
    assert calls == [2, 2, 1]
    idxs = sorted(x["index"] for x in out)
    assert idxs == [0, 1, 2, 3, 4]  # 全局下标无重叠无丢失


def test_rerank_top_n_truncates(monkeypatch):
    monkeypatch.setattr(rerank_client, "_post_rerank",
                        lambda b, q, d, k, tn: [{"index": i, "relevance_score": 1.0 - i * 0.1}
                                                for i in range(len(d))])
    out = rerank_client.rerank("q", ["a", "b", "c"], api_key="k", top_n=2)
    assert len(out) == 2


# ---------------- pick_relevant 集成 ----------------

def _setup_candidates(db, pid):
    """4 份文档都关键词命中 → 候选数 4 > top_k 2 → 触发 rerank。"""
    return [
        _mk(db, pid, "甲设定.md", "陈默在破庙的设定。" + "补充" * 20),
        _mk(db, pid, "乙设定.md", "陈默在破庙的剧情。" + "补充" * 20),
        _mk(db, pid, "丙设定.md", "陈默在破庙的线索。" + "补充" * 20),
        _mk(db, pid, "丁设定.md", "陈默在破庙的人物。" + "补充" * 20),
    ]


def test_rerank_reorders_topk(test_db, monkeypatch):
    """rerank 把最末一份提到第 1 → 选中序按 rerank 走。"""
    docs = _setup_candidates(test_db, "p1")
    monkeypatch.setattr(vector_index, "search_similar", lambda *a, **k: [])

    def fake_rerank(query, docs_text, *, api_key=None, db=None, top_n=None):
        # 索引 3（丁）给最高分
        return [{"index": 3, "relevance_score": 0.99},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.3},
                {"index": 2, "relevance_score": 0.1}]
    monkeypatch.setattr(rerank_client, "rerank", fake_rerank)

    blocks, detail = pick_relevant(test_db, "p1", None, "陈默在破庙", top_k=2)
    picked = [d for d in detail if d["score"] is not None]
    assert picked[0]["filename"] == "丁设定.md"
    assert picked[0]["rerank_score"] == 0.99
    assert len(picked) == 2


def test_rerank_failure_falls_back_to_rrf(test_db, monkeypatch):
    """rerank 抛错 → 静默降级 RRF 原序，不抛异常、不断流。"""
    docs = _setup_candidates(test_db, "p1")
    monkeypatch.setattr(vector_index, "search_similar", lambda *a, **k: [])

    def boom(*a, **k):
        raise rerank_client.RerankError("模拟 API 失败")
    monkeypatch.setattr(rerank_client, "rerank", boom)

    blocks, detail = pick_relevant(test_db, "p1", None, "陈默在破庙", top_k=2)
    picked = [d for d in detail if d["score"] is not None]
    assert len(picked) == 2
    assert all(d["rerank_score"] is None for d in picked)  # 未参与 rerank


def test_rerank_not_called_when_candidates_le_topk(test_db, monkeypatch):
    """候选数 ≤ top_k 时不调 rerank（省 API）。"""
    _mk(test_db, "p1", "唯一设定.md", "陈默在破庙。" + "补充" * 20)
    called = []
    monkeypatch.setattr(vector_index, "search_similar", lambda *a, **k: [])
    monkeypatch.setattr(rerank_client, "rerank",
                        lambda *a, **k: called.append(1) or [])
    pick_relevant(test_db, "p1", None, "陈默在破庙", top_k=3)
    assert called == []
