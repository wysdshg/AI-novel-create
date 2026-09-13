"""写后摄取的分阶段开关（Phase 2 测试优化，2026-09-10）。

背景：跑一次 e2e 全链路要 3 次 LLM 调用（正文 1 + 记忆抽取 1 + 概览聚合·篇级 1）。
验证「文笔稳定性」时后两次纯属白烧——多跑 5 轮就是 15 次调用，其中 10 次对
「文笔稳不稳」这个目标毫无贡献。本组用例固化新增的分阶段开关：

- `ingest_chapter(extract=False)`：跳过 LLM 抽取，**改走 fallback_extract 规则兜底**
  （不是整段跳过）——章级记忆仍落库、走向/伏笔/篇章摘要链路不断，只是摘要不如 AI 精炼。
- `ingest_chapter(aggregate=False)`：概览聚合的篇级不再调 LLM，退回拼接（展示层，不影响生成）。
- config 键 `memory.extract_enabled` / `memory.aggregate_overview` 作为默认值来源。

测试库无模型配置，天然走规则兜底，正好用来验证「不调 LLM 也能保留数据链」。
"""
import pytest


@pytest.fixture()
def chain(test_db):
    """建 project → volume → article → chapter（有正文），返回 (pid, aid, chapter)。"""
    from app.schemas.article import ArticleCreate
    from app.schemas.chapter import ChapterCreate
    from app.schemas.volume import VolumeCreate
    from app.services import article_crud, chapter_crud, project_crud, volume_crud

    proj = project_crud.create_project(
        test_db, {"name": "摄取开关测试", "genre": "测试", "summary": "s"})
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


def _mem(db, pid):
    from app.models.orm import ChapterMemoryORM
    return db.query(ChapterMemoryORM).filter_by(project_id=pid).all()


# ===========================================================================
# extract 开关：关掉不调 LLM，但仍要落库（数据链不断）
# ===========================================================================

def test_extract_disabled_still_persists_memory(test_db, chain):
    """extract=False：跳过 LLM，但规则兜底仍要落一条章级记忆——否则下一章没上下文。"""
    from app.services import ingestion

    pid, _aid, ch = chain
    res = ingestion.ingest_chapter(test_db, pid, ch, extract=False)
    test_db.commit()

    assert res.get("extract_llm") is False
    assert res.get("fallback") is True
    # 关键断言：省了调用，但记忆还在（这是与「整段跳过」的本质区别）
    assert res.get("fallback_reason") == "extract_disabled"
    assert len(_mem(test_db, pid)) == 1, "关掉 LLM 抽取后章级记忆丢失（数据链断了）"


def test_extract_disabled_result_flags(test_db, chain):
    """开关状态要能在 result 里看出来，否则日志把「主动省调用」误读成「抽取失败」。"""
    from app.services import ingestion

    pid, _aid, ch = chain
    res = ingestion.ingest_chapter(test_db, pid, ch, extract=False)
    test_db.commit()
    assert res["extract_llm"] is False
    assert res["fallback_reason"] == "extract_disabled"

    # 对照：extract=True（但测试库无模型）→ fallback 原因是 llm 不可用，而非 disabled
    res2 = ingestion.ingest_chapter(test_db, pid, ch, extract=True)
    test_db.commit()
    assert res2["extract_llm"] is True
    assert res2["fallback_reason"] == "llm_failed"


# ===========================================================================
# aggregate 开关：透传给调用方（聚合不在 ingest_chapter 内）
# ===========================================================================

def test_aggregate_flag_passthrough(test_db, chain):
    """aggregate 开关解析结果要透出来，供 chapter.py 的后台线程决定是否跑概览聚合。"""
    from app.services import ingestion

    pid, _aid, ch = chain
    res_on = ingestion.ingest_chapter(test_db, pid, ch, extract=False, aggregate=True)
    test_db.commit()
    assert res_on["_aggregate"] is True

    res_off = ingestion.ingest_chapter(test_db, pid, ch, extract=False, aggregate=False)
    test_db.commit()
    assert res_off["_aggregate"] is False


# ===========================================================================
# config 键：默认值即旧行为（True），保证不传参时零回归
# ===========================================================================

def test_config_defaults_are_on(test_db):
    """两个新键默认必须为 True——不传参时行为与改造前完全一致（零回归）。"""
    from app.services import app_config

    assert app_config.get(test_db, app_config.KEY_EXTRACT_ENABLED) is True
    assert app_config.get(test_db, app_config.KEY_AGGREGATE_OVERVIEW) is True


def test_config_off_propagates_to_ingest(test_db, chain):
    """配置改成 False 后，不传参的 ingest_chapter 也应按配置走（省调用）。"""
    from app.services import app_config, ingestion

    pid, _aid, ch = chain
    app_config.set_value(test_db, app_config.KEY_EXTRACT_ENABLED, False)

    res = ingestion.ingest_chapter(test_db, pid, ch)   # 不传 extract，读配置
    test_db.commit()
    assert res["extract_llm"] is False
    assert len(_mem(test_db, pid)) == 1, "按配置关掉抽取后记忆仍应规则兜底落库"


def test_explicit_param_overrides_config(test_db, chain):
    """显式入参优先于配置——测试脚本要能临时覆盖全局设置。"""
    from app.services import app_config, ingestion

    pid, _aid, ch = chain
    app_config.set_value(test_db, app_config.KEY_EXTRACT_ENABLED, False)
    res = ingestion.ingest_chapter(test_db, pid, ch, extract=True)  # 显式开
    test_db.commit()
    assert res["extract_llm"] is True


def test_new_keys_registered_in_defaults(test_db):
    """新键必须进 DEFAULTS，否则 set_many 会当作脏键挡掉（前端改不了）。"""
    from app.services import app_config

    assert app_config.KEY_EXTRACT_ENABLED in app_config.DEFAULTS
    assert app_config.KEY_AGGREGATE_OVERVIEW in app_config.DEFAULTS
    keys = {d["key"] for d in app_config.describe()}
    assert app_config.KEY_EXTRACT_ENABLED in keys
    assert app_config.KEY_AGGREGATE_OVERVIEW in keys


def test_empty_content_skips_everything(test_db, chain):
    """空正文必须原样短路（不落记忆、不报错）——这条回归旧行为。"""
    from app.services import ingestion

    pid, _aid, ch = chain
    ch.content = ""
    res = ingestion.ingest_chapter(test_db, pid, ch, extract=True)
    assert res.get("skipped")
    assert len(_mem(test_db, pid)) == 0


# ===========================================================================
# 走向卡片下线（2026-09-13 用户拍板）：默认不推，代码保留，恢复路径可用
# ===========================================================================

def test_push_directions_default_off(test_db):
    """走向卡片已下线：DEFAULTS 默认 False，DB 无行时 get() 必须读到 False。

    用户感知：每章生成后对话区不再自动出现「第N章写完了。接下来我看到三条路…」卡片。
    抽取链路不变（next_directions 字段照常抽、照常落 result），只是不往对话区推。
    """
    from app.services import app_config

    assert app_config.DEFAULTS[app_config.KEY_PUSH_DIRECTIONS] is False
    assert app_config.get(test_db, app_config.KEY_PUSH_DIRECTIONS) is False
    # describe() 是设置面板数据源，默认值要同步呈现为关
    meta = {d["key"]: d for d in app_config.describe()}
    assert meta[app_config.KEY_PUSH_DIRECTIONS]["default"] is False


def test_push_directions_reenable_path(test_db, chain):
    """恢复路径验证：配置落库 True（或显式传参）即可重新启用，无需改代码。"""
    from app.services import app_config, ingestion

    pid, _aid, ch = chain
    # ① 配置恢复：落库 True 后 get() 读到 True（DEFAULTS 的 False 被覆盖）
    app_config.set_value(test_db, app_config.KEY_PUSH_DIRECTIONS, True)
    assert app_config.get(test_db, app_config.KEY_PUSH_DIRECTIONS) is True

    # ② 链路恢复：显式 push_directions=True 时推送块可达（测试库无模型 → 抽取走
    #    规则兜底，next_directions 恒空 → 不会真推卡片，但 result 不报错、链路不炸）
    res = ingestion.ingest_chapter(test_db, pid, ch, extract=False, push_directions=True)
    test_db.commit()
    assert "directions_pushed" not in res or res.get("directions_pushed", 0) == 0
