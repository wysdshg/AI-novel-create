"""全局键值配置服务（app_configs 表）。

上下文预算档位、写库模式、去 AI 味开关这类"会一直往上加"的选项统一放这里，
避免每加一个开关就改一次 ORM + 迁移一次表。

读取全部走 get()，永远有兜底默认值——配置表为空时系统必须照常能跑。
"""
from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.orm import AppConfigORM

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置键约定（改默认值只改这里）
# ---------------------------------------------------------------------------

# 上下文预算档位：tight / standard / loose
KEY_CONTEXT_BUDGET = "context.budget_level"
# 写库模式：review = 后置抽取 + 预览确认；auto = 直接落库；tool = function calling 由模型自己调
KEY_WRITE_MODE = "db.write_mode"
# 章节生成时是否前置注入去 AI 味规则
KEY_HUMANIZE_INJECT = "humanize.inject_on_generate"
# 生成完成后是否自动跑去 AI 味检测（只出报告，不改文）
KEY_HUMANIZE_SCAN = "humanize.scan_after_generate"
# 是否在每章生成后自动跑写后摄取（抽记忆 + 出走向）
KEY_INGEST_ENABLED = "memory.ingest_after_generate"
# 摄取内的记忆抽取：关掉后不调 LLM，改用规则兜底摘要（省 1 次调用，数据链不断）
KEY_EXTRACT_ENABLED = "memory.extract_enabled"
# 摄取内的概览向上聚合：关掉后篇级也走拼接，不再调 LLM 精炼（纯展示层，省 1 次调用）
KEY_AGGREGATE_OVERVIEW = "memory.aggregate_overview"
# 章后走向建议是否推送到对话区
KEY_PUSH_DIRECTIONS = "memory.push_directions_to_chat"
# 每隔多少章滚动压缩一次阶段摘要
KEY_STAGE_EVERY = "memory.stage_compress_every"
# 注入下一章的最近章级记忆条数
KEY_RECENT_MEMORY_N = "memory.recent_chapters"

DEFAULTS: dict[str, Any] = {
    KEY_CONTEXT_BUDGET: "standard",
    KEY_WRITE_MODE: "review",
    KEY_HUMANIZE_INJECT: True,
    KEY_HUMANIZE_SCAN: True,
    KEY_INGEST_ENABLED: True,
    KEY_EXTRACT_ENABLED: True,
    KEY_AGGREGATE_OVERVIEW: True,
    # ⬇ 2026-09-13 用户拍板下线：走向卡片不再每章自动推到对话区。
    #   代码全保留（抽取 prompt 的 next_directions 字段、_render_directions、推送块均未动），
    #   恢复方式：把此处改回 True，或在设置页 / DB app_configs 落一行
    #   {"key": "memory.push_directions_to_chat", "value": true} 即刻生效。
    KEY_PUSH_DIRECTIONS: False,
    KEY_STAGE_EVERY: 10,
    KEY_RECENT_MEMORY_N: 3,
}

DESCRIPTIONS: dict[str, str] = {
    KEY_CONTEXT_BUDGET: "上下文预算档位：tight(紧,8K字符) / standard(标准,32K) / loose(宽松,120K)",
    KEY_WRITE_MODE: "AI 写数据库的方式：review(抽取后预览确认) / auto(直接落库) / tool(模型自主调用工具)",
    KEY_HUMANIZE_INJECT: "章节生成时把去 AI 味规则前置注入提示词",
    KEY_HUMANIZE_SCAN: "章节生成后自动扫描 AI 味问题（只出报告不改文）",
    KEY_INGEST_ENABLED: "每章生成后自动抽取记忆并推演后续走向",
    KEY_EXTRACT_ENABLED: "摄取时用 LLM 抽取章级记忆（关掉改用规则兜底摘要，省一次调用）",
    KEY_AGGREGATE_OVERVIEW: "摄取时用 LLM 精炼篇级概览（关掉改纯拼接，概览页仍有内容）",
    KEY_PUSH_DIRECTIONS: "把章后走向建议以卡片形式推送到对话区（2026-09-13 起默认下线，需手动开启）",
    KEY_STAGE_EVERY: "每累积多少章滚动压缩一次阶段摘要",
    KEY_RECENT_MEMORY_N: "注入下一章的最近章级记忆条数",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get(db: Session, key: str, default: Any = None) -> Any:
    """读配置。表不存在 / 无记录 / 查询异常一律回落默认值，绝不抛给调用方。"""
    fallback = DEFAULTS.get(key, default)
    try:
        row = db.query(AppConfigORM).filter_by(key=key).first()
    except Exception as e:  # noqa: BLE001 — 表还没建好时不能拖垮主流程
        # 回落默认值不抛异常，但要留痕：否则「我明明改了配置怎么没生效」会先去怀疑配置值本身（Phase 3.5）
        logger.warning(f"[app_config] 读取 {key} 失败，回落默认值 {fallback!r}: {type(e).__name__}: {e}")
        return fallback
    if row is None or row.value is None:
        return fallback
    return row.value


def set_value(db: Session, key: str, value: Any) -> Any:
    row = db.query(AppConfigORM).filter_by(key=key).first()
    if row is None:
        row = AppConfigORM(
            key=key,
            value=value,
            description=DESCRIPTIONS.get(key),
            updated_at=_now(),
        )
        db.add(row)
    else:
        row.value = value
        row.updated_at = _now()
        if not row.description:
            row.description = DESCRIPTIONS.get(key)
    db.commit()
    return value


def get_all(db: Session) -> dict[str, Any]:
    """返回完整配置视图：默认值打底，已落库的覆盖上去。"""
    merged = dict(DEFAULTS)
    try:
        for row in db.query(AppConfigORM).all():
            if row.value is not None:
                merged[row.key] = row.value
    except Exception as e:  # noqa: BLE001
        # 全量读取失败 → 只返回默认值（管理页会显示"全是默认"，要能解释为什么）Phase 3.5
        logger.warning(f"[app_config] 全量读取失败，仅返回默认值: {type(e).__name__}: {e}")
    return merged


def set_many(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """批量写。只接受 DEFAULTS 里声明过的 key，挡掉前端传脏键。"""
    for k, v in (payload or {}).items():
        if k not in DEFAULTS:
            continue
        row = db.query(AppConfigORM).filter_by(key=k).first()
        if row is None:
            db.add(AppConfigORM(key=k, value=v, description=DESCRIPTIONS.get(k), updated_at=_now()))
        else:
            row.value = v
            row.updated_at = _now()
    db.commit()
    return get_all(db)


def describe() -> list[dict]:
    """配置项元信息，供前端渲染设置面板。"""
    return [
        {"key": k, "default": v, "description": DESCRIPTIONS.get(k, "")}
        for k, v in DEFAULTS.items()
    ]
