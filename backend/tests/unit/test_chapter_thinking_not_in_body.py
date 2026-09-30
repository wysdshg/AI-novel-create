"""防回归：思考内容（reasoning）绝不进正文、绝不落库（2026-09-20）。

背景（两个要同时满足的目标，历史上互相打架）：

1. **必须保活**：Qwen3.8-Flash-Next 开思考首字实测 407s。旧链路把 reasoning 只
   buffer 不 yield，思考阶段前端 0 字节 → vite 代理 300s 空闲掐断 → 浏览器只看到
   "network error"，真实原因被完全掩盖。故改为 thinking 走独立事件发给前端。
2. **必须纯净**：历史坑是「厂商把 reasoning 塞进 content」，或适配器兜底把
   reasoning_buffer 当正文输出 → 小说正文混入思考文字。中文思考的模型（Qwen3.8）
   尤其危险，因为兜底那道"英文占比 >15%"的防线对它完全放行。

本文件用假适配器驱动真实 SSE 生成器，锁死两件事：
- thinking 帧**必须**发出来（否则又会退化成 network error）；
- 无论模型怎么吐，正文/落库内容里**绝不出现** reasoning 文本。
"""
import json
import uuid

import pytest

from app.models.orm import (
    ModelConfigORM, ProjectORM, VolumeORM, ArticleORM, ChapterORM,
)
from app.routers import chapter as chapter_router
from app.schemas.chapter import GenerateRequest

# 故意用**中文**思考：英文占比那条兜底防线对中文无效，最能暴露污染
REASONING = "让我先梳理一下本章的节奏。主角此时应当处在药铺后院，刚拿到灵石。"
BODY = "石阶尽头，云雾翻涌。沈砚按住胸口，指缝间透出一点微光。"


class _DualAdapter:
    """双通道假适配器：可配置为「思考+正文」或「只有思考」。"""

    def __init__(self, with_body=True):
        self.with_body = with_body

    def stream_dual(self, *_a, **_kw):
        # 分多帧吐出，模拟真实流式（顺带验证节流合批不会丢帧顺序）
        for i in range(0, len(REASONING), 8):
            yield ("thinking", REASONING[i:i + 8])
        if self.with_body:
            for i in range(0, len(BODY), 8):
                yield ("content", BODY[i:i + 8])

    def stream(self, *_a, **_kw):
        # 兜底路径：若代码错误地退回单通道，正文里不会有任何内容
        for i in range(0, len(BODY), 8):
            yield BODY[i:i + 8]


def _seed_project(test_db):
    pid = uuid.uuid4().hex
    test_db.add(ProjectORM(id=pid, name="思考纯净回归", genre="测试"))
    vid = uuid.uuid4().hex
    test_db.add(VolumeORM(id=vid, project_id=pid, name="第一卷"))
    aid = uuid.uuid4().hex
    test_db.add(ArticleORM(id=aid, project_id=pid, name="第一篇", volume_id=vid))
    test_db.add(ModelConfigORM(
        id=uuid.uuid4().hex, name="假模型", vendor="openai_compat",
        api_base="http://127.0.0.1:1/v1", api_key="sk-test",
        model_name="fake", is_default=True, status="active",
    ))
    test_db.commit()
    return pid, aid


def _consume(resp):
    """把 StreamingResponse 的 SSE 流解析成 [(event, payload)]。"""
    import asyncio

    async def _drain():
        chunks = []
        async for raw in resp.body_iterator:
            chunks.append(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return "".join(chunks)

    text = asyncio.run(_drain())
    events = []
    for block in text.split("\n\n"):
        event, buf = None, ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                buf += line[len("data:"):].strip()
        if event:
            try:
                events.append((event, json.loads(buf) if buf else {}))
            except json.JSONDecodeError:
                events.append((event, {}))
    return events


@pytest.fixture()
def dual_adapter(monkeypatch):
    monkeypatch.setattr(
        chapter_router, "get_adapter", lambda *a, **k: _DualAdapter(with_body=True)
    )


@pytest.fixture()
def dual_adapter_no_body(monkeypatch):
    monkeypatch.setattr(
        chapter_router, "get_adapter", lambda *a, **k: _DualAdapter(with_body=False)
    )


def test_thinking_events_are_emitted(test_db, dual_adapter):
    """① 思考阶段必须持续发 thinking 事件 —— 否则又会退化成 300s network error。"""
    pid, aid = _seed_project(test_db)
    resp = chapter_router.generate_chapter(
        pid, GenerateRequest(chapter_no=1, article_id=aid, title="第1章"), test_db
    )
    events = _consume(resp)
    names = [e for e, _ in events]

    assert "thinking" in names, f"思考阶段必须推流保活，实得事件 {names}"
    thinking_text = "".join(d.get("text", "") for e, d in events if e == "thinking")
    assert "梳理" in thinking_text, "thinking 帧应带上思考内容（不得被吞掉）"


def test_reasoning_never_enters_body(test_db, dual_adapter):
    """② 正文与落库内容里绝不出现 reasoning —— 历史污染坑的正面防线。"""
    pid, aid = _seed_project(test_db)
    resp = chapter_router.generate_chapter(
        pid, GenerateRequest(chapter_no=1, article_id=aid, title="第1章"), test_db
    )
    events = _consume(resp)

    chunk_text = "".join(d.get("text", "") for e, d in events if e == "chunk")
    assert REASONING not in chunk_text, "思考内容混进了 chunk 正文流"
    assert "梳理" not in chunk_text, "思考内容混进了 chunk 正文流"

    saved = [d for e, d in events if e == "saved"]
    assert saved, f"有正文就应落库，实得事件 {[e for e, _ in events]}"
    row = test_db.query(ChapterORM).filter_by(project_id=pid).first()
    assert row is not None
    assert "梳理" not in (row.content or ""), "思考内容被写进了章节正文（污染落库）"
    assert "石阶尽头" in (row.content or ""), "真实正文应当落库"


def test_thinking_only_does_not_persist(test_db, dual_adapter_no_body):
    """③ 模型**只**吐思考、始终没出正文 → 拒绝落库，且绝不能拿思考冒充正文。

    这是 openai_compat.stream_with_thinking 的兜底会踩的坑：它会把 reasoning_buffer
    当正文输出（中文思考绕得过它的英文占比防线）。章节生成走的是 stream_dual，
    必须只留痕、不兜底。
    """
    pid, aid = _seed_project(test_db)
    resp = chapter_router.generate_chapter(
        pid, GenerateRequest(chapter_no=1, article_id=aid, title="第1章"), test_db
    )
    events = _consume(resp)
    names = [e for e, _ in events]

    assert "saved" not in names, f"正文为空不得落库，实得 {names}"
    assert "error" in names, f"应发 error 说明原因，实得 {names}"
    chunk_text = "".join(d.get("text", "") for e, d in events if e == "chunk")
    assert "梳理" not in chunk_text, "绝不能拿中文思考冒充正文"

    assert test_db.query(ChapterORM).filter_by(project_id=pid).count() == 0
