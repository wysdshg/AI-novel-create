"""08-B5 用量计量：摄取链路 + 指令解析此前不记账 → 观测页统计偏低。

本组用例用 **fake adapter**（带 last_usage 的假适配器）驱动真实代码路径，
断言 `llm_usage_logs` 表出现正确 scene / vendor / token / ok / estimated：

- ingest_extract  章级记忆抽取（ingestion.ingest_chapter 第 1 步）
- ingest_aggregate 概览聚合（_llm_compress_summary）
- ingest_stage     阶段压缩（compress_stage）
- command          指令解析入库（config_command.run）

商讨（discussion）是 SSE 生成器 + 真实模型，单测不覆盖 → 真机验证（见 02 §1.5）。

记账语义（usage_crud.record_usage）：有 last_usage → 真实值 estimated=False；
无 → 按 prompt/completion 文本估算 estimated=True；失败也记 ok=False。
"""
import pytest

from app.models.orm import ChapterMemoryORM, LlmUsageLogORM, ModelConfigORM


# ===========================================================================
# fixtures
# ===========================================================================

class _FakeAdapter:
    """假适配器：可注入 last_usage（模拟厂商回流）或强制失败。"""

    vendor = "deepseek"
    model_name = "fake-model"

    def __init__(self, fail=False, usage=None, reply=None):
        self._fail = fail
        self.last_usage = usage
        self._reply = reply

    def chat(self, messages, **kw):
        if self._fail:
            raise RuntimeError("fake 模型调用失败")
        return self._reply if self._reply is not None else "模型输出文本"

    def stream(self, messages, **kw):
        yield "fake"


@pytest.fixture()
def memory_model(test_db):
    """ingestion._pick_model 优先取 role='memory'。"""
    m = ModelConfigORM(id="mc-mem", name="假·记忆模型", vendor="deepseek",
                       model_name="fake-memory", api_base="http://fake", api_key="k",
                       role="memory", status="active")
    test_db.add(m)
    test_db.commit()
    return m


@pytest.fixture()
def default_model(test_db):
    """config_command.resolve_model / aggregate 取默认模型。"""
    m = ModelConfigORM(id="mc-def", name="假·默认模型", vendor="modelscope",
                       model_name="fake-default", api_base="http://fake", api_key="k",
                       role="primary", status="active", is_default=True)
    test_db.add(m)
    test_db.commit()
    return m


@pytest.fixture()
def chain(test_db):
    """project → volume → article → chapter（照 test_ingest_switches 的形态）。"""
    from app.schemas.article import ArticleCreate
    from app.schemas.chapter import ChapterCreate
    from app.schemas.volume import VolumeCreate
    from app.services import article_crud, chapter_crud, project_crud, volume_crud

    proj = project_crud.create_project(
        test_db, {"name": "用量计量测试", "genre": "测试", "summary": "s"})
    pid = proj["id"] if isinstance(proj, dict) else proj.id
    vol = volume_crud.create_volume(test_db, pid, VolumeCreate(name="卷一", summary=""))
    art = article_crud.create_article(
        test_db, pid, ArticleCreate(volume_id=vol.id, name="篇一", summary=""))
    ch = chapter_crud.create_chapter(
        test_db, pid,
        ChapterCreate(chapter_no=1, title="第一章",
                      content="雨落在青石板上，沈砚握紧了那枚铜牌。" * 30,
                      word_count=570, article_id=art.id),
    )
    return pid, art.id, ch


def _usage_rows(db, scene):
    return db.query(LlmUsageLogORM).filter_by(scene=scene).all()


# ===========================================================================
# ingest_extract：章级记忆抽取
# ===========================================================================

def test_ingest_extract_records_real_usage(test_db, chain, memory_model, monkeypatch):
    """厂商回流 last_usage → 记真实值（estimated=False），vendor/model 正确。"""
    import app.services.ingestion as ing

    fake = _FakeAdapter(usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                        reply='{"summary": "主角进山遇袭，结识同伴，夜宿破庙。"}')
    monkeypatch.setattr(ing, "get_adapter", lambda vendor, cfg: fake)

    pid, _aid, ch = chain
    res = ing.ingest_chapter(test_db, pid, ch, extract=True)

    rows = _usage_rows(test_db, "ingest_extract")
    assert len(rows) == 1, f"应记 1 条，实际 {len(rows)}"
    r = rows[0]
    assert r.total_tokens == 150
    assert r.vendor == "deepseek"
    assert r.model_name == "fake-memory"
    assert r.project_id == pid
    assert r.ok is True
    assert r.estimated is False, "厂商回流了 usage 就不该标估算"


def test_ingest_extract_records_failure(test_db, chain, memory_model, monkeypatch):
    """调用失败也记（ok=False，prompt 已发出同样烧 token）→ 按文本估算。"""
    import app.services.ingestion as ing

    fake = _FakeAdapter(fail=True)
    monkeypatch.setattr(ing, "get_adapter", lambda vendor, cfg: fake)

    pid, _aid, ch = chain
    res = ing.ingest_chapter(test_db, pid, ch, extract=True)

    assert res.get("fallback") is True, "调用失败应走规则兜底"
    rows = _usage_rows(test_db, "ingest_extract")
    assert len(rows) == 1
    r = rows[0]
    assert r.ok is False
    assert r.estimated is True, "无 last_usage → 按字符估算"
    assert r.prompt_tokens > 0


def test_ingest_extract_parse_failure_still_ok(test_db, chain, memory_model, monkeypatch):
    """解析失败 ≠ 调用失败：模型调成功就记 ok=True（走兜底是另一回事）。"""
    import app.services.ingestion as ing

    fake = _FakeAdapter(usage={"prompt_tokens": 10, "completion_tokens": 5},
                        reply="完全不是 JSON 的输出")
    monkeypatch.setattr(ing, "get_adapter", lambda vendor, cfg: fake)

    pid, _aid, ch = chain
    ing.ingest_chapter(test_db, pid, ch, extract=True)

    rows = _usage_rows(test_db, "ingest_extract")
    assert len(rows) == 1
    assert rows[0].ok is True


# ===========================================================================
# ingest_aggregate：概览聚合
# ===========================================================================

def test_ingest_aggregate_records_usage(test_db, chain, memory_model, monkeypatch):
    import app.services.ingestion as ing

    fake = _FakeAdapter(usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60})
    monkeypatch.setattr(ing, "get_adapter", lambda vendor, cfg: fake)

    pid, _aid, _ch = chain
    out = ing._llm_compress_summary(test_db, "篇《测试》", ["摘要一", "摘要二"], 200,
                                    memory_model, project_id=pid)
    assert out  # fake 返回文本，_strip_text 后非空

    rows = _usage_rows(test_db, "ingest_aggregate")
    assert len(rows) == 1
    assert rows[0].total_tokens == 60
    assert rows[0].project_id == pid


# ===========================================================================
# ingest_stage：阶段压缩
# ===========================================================================

def test_ingest_stage_records_usage(test_db, chain, memory_model, monkeypatch):
    import app.services.ingestion as ing

    fake = _FakeAdapter(
        usage={"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
        reply='{"summary": "阶段摘要", "key_events": [], "open_threads": []}')
    monkeypatch.setattr(ing, "get_adapter", lambda vendor, cfg: fake)

    pid, _aid, _ch = chain
    # compress_stage 需要[区间内有章级记忆]，否则在调模型前就返回 None
    test_db.add(ChapterMemoryORM(id="cm1", project_id=pid, chapter_id="c1",
                                 chapter_no=1, summary="第一章摘要"))
    test_db.commit()

    out = ing.compress_stage(test_db, pid, 1, 1)
    assert out is not None

    rows = _usage_rows(test_db, "ingest_stage")
    assert len(rows) == 1
    assert rows[0].total_tokens == 45


# ===========================================================================
# command：指令解析入库
# ===========================================================================

def test_command_records_usage(test_db, chain, default_model, monkeypatch):
    import app.services.config_command as cc

    fake = _FakeAdapter(usage={"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
                        reply='{"characters": [{"name": "张三", "personality": "冷静"}], "reply": "已整理"}')
    monkeypatch.setattr(cc, "get_adapter", lambda vendor, cfg: fake)

    pid, _aid, _ch = chain
    cc.run(test_db, pid, "添加角色张三，性格冷静")

    rows = _usage_rows(test_db, "command")
    assert len(rows) == 1
    r = rows[0]
    assert r.total_tokens == 120
    assert r.vendor == "modelscope"
    assert r.model_name == "fake-default"
    assert r.ok is True


def test_command_records_failure(test_db, chain, default_model, monkeypatch):
    import app.services.config_command as cc

    fake = _FakeAdapter(fail=True)
    monkeypatch.setattr(cc, "get_adapter", lambda vendor, cfg: fake)

    pid, _aid, _ch = chain
    res = cc.run(test_db, pid, "添加角色张三")
    # ok() 返回信封 {code, message, data, trace_id} → 业务字段在 data 里
    assert res["data"]["model_ok"] is False

    rows = _usage_rows(test_db, "command")
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].estimated is True


# ===========================================================================
# 护栏：计量失败绝不影响主链路（usage_crud 自身已静默，这里验摄取整体不炸）
# ===========================================================================

def test_metering_never_breaks_ingest(test_db, chain, memory_model, monkeypatch):
    """旁路纪律（纵深防御）：即使 record_usage 被换成必抛实现，摄取主链路照常完成。"""
    import app.services.ingestion as ing

    class _Boom:
        vendor = "deepseek"
        model_name = "fake"
        last_usage = {"prompt_tokens": 1, "completion_tokens": 1}

        def chat(self, messages, **kw):
            return '{"summary": "主角进山。"}'

    def _boom_record(*a, **kw):
        raise RuntimeError("记账炸了")

    monkeypatch.setattr(ing.usage_crud, "record_usage", _boom_record)
    monkeypatch.setattr(ing, "get_adapter", lambda vendor, cfg: _Boom())

    pid, _aid, ch = chain
    res = ing.ingest_chapter(test_db, pid, ch, extract=True)
    assert res.get("memory_id"), "主链路必须完成（记忆已落库）"
