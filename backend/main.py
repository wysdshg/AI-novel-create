"""网页小说智能体 — FastAPI 入口（脚手架阶段）。

所有业务端点当前以「接口桩」形式存在：返回统一信封 + 占位/mock 数据，
或返回 50101（未实现）。后续功能开发按 API接口规范.md 落地，无需改动本文件结构。
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import logging

from app.core.logging_config import setup_logging
from app.core.response import ok
from app.core.database import init_db
from app.routers import (
    projects,
    database,
    chapter,
    discussion,
    model,
    memory,
    foreshadow,
    template,
    references,
    # ——本轮（用户大任务 1）新增的三大全局模块：——
    setting,         # /settings      全局设定库（境界/货币/体系/规则）
    custom_skill,    # /global-skills 自定义写作 SKILL 模板（与资料库 Skill 区分）
    workflow,        # /workflows     全局工作流（节点+边的 DAG 模板）
    # ——本轮（用户大任务 2 第 2 批）：4 级结构 小说→卷→篇→章 ——
    volume,          # /volumes       卷（volume）
    article,         # /articles      篇（article）+ /articles/{aid}/chapters
    # ——本轮（用户大任务 2 第 3 批）：参考三概念 ——
    reference_global,  # /references/global 全局共享参考资料池 + import-global
    # ——AI 能力层：去AI味检测 / SKILL 调度查询 / 全局配置 / 实体确认入库 ——
    assist,
    # ——Phase 4.1 最小 eval：生成版本留档 / 打分 / 对比 ——
    # 别名导入：模块名 eval 会遮蔽 Python 内置 eval()，显式改名避免隐患
    eval as eval_router,
    # ——Phase 4.2/4.3 观测消费端：用量计量 + 反馈回流 ——
    usage,
    feedback,
    # ——Phase 7.1 情节模板库 ——
    graph_rag,    plot_templates,
    # ——Phase 7.2 篇规划 ——
    plans,
    # ——Phase 7.3 角色选角 ——
    casting,
    # ——E3 全局物品/技能库管理页 ——
    global_ref,
    setting_template,
)# 统一日志：必须在任何业务模块打日志之前初始化，否则 INFO 级日志会被
# logging 的「last resort」处理器（仅 WARNING+）静默丢掉。级别可用 NA_LOG_LEVEL 覆盖。
setup_logging()

logger = logging.getLogger(__name__)

app = FastAPI(
    title="网页小说智能体 API",
    description="B/S 小说创作专属智能体后端（脚手架阶段，仅接口契约）",
    version="0.1.0",
)

# 开发期放开 CORS，生产按域名收紧
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 统一挂载各模块路由；prefix 与 API接口规范.md 的 /api/v1 一致
for r in (
    projects, database, chapter, discussion, model, memory,
    foreshadow, template, references,
    setting, custom_skill, workflow,                 # 本轮新增的三大全局模块
    volume, article,                                 # 4 级结构：卷 / 篇
    reference_global,                                 # 全局参考资料池
    assist,                                           # AI 辅助能力总入口
    eval_router,                                      # Phase 4.1 最小 eval
    usage, feedback,                                  # Phase 4.2/4.3 观测消费端
    plot_templates,                                   # Phase 7.1 情节模板库
    graph_rag,                                        # docs/09 §3：GraphRAG 注入装配（阶段 A3）
    plans,                                            # Phase 7.2 篇规划
    casting,                                          # Phase 7.3 角色选角
    global_ref,                                       # E3 物品技能库管理
    setting_template,                                 # 设定模板（按题材一套一套）
):
    app.include_router(r.router, prefix="/api/v1")

# 启动时建表并写入示例作品，保证角色库等模块开箱可用
@app.on_event("startup")
def _startup():
    # uvicorn 在 Config.load() 阶段会执行一次 dictConfig，可能把我们原先挂的
    # 文件 handler 冲掉 —— 这里幂等补挂，确保 uvicorn 自己的日志（含 access）也落盘。
    from app.core.logging_config import attach_uvicorn_file_logging

    attach_uvicorn_file_logging()
    logger.info("===== 应用启动完成，开始接收请求 =====")

    init_db()
    # 预置 6 条写作 SKILL（幂等：已存在同名则跳过）。
    # 开箱即用比让作者对着空列表发呆强，不满意可以在「写作技能」里改或禁用。
    try:
        from app.core import database as _db_mod
        from app.services import seed_skills

        # SessionLocal 是 get_engine() 里才赋值的模块级变量，
        # 必须走属性访问，不能在 import 时就绑定（那时还是 None）
        db = _db_mod.SessionLocal()
        try:
            r = seed_skills.install(db)
            if r.get("created"):
                logger.info(f"[startup] 已安装预置写作 SKILL: {r['created']}")
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        # 原为 print：print 不进日志文件，挂掉后查不到（Phase 3.5 起统一走 logging）
        logger.warning(f"[startup] 预置 SKILL 安装跳过: {type(e).__name__}: {e}")


@app.get("/api/v1/health", tags=["系统"])
def health_check():
    """健康检查，供前端/部署探针使用。"""
    return ok({"status": "up", "version": "0.1.0"})


@app.get("/", include_in_schema=False)
def root():
    return ok({"msg": "网页小说智能体 API（脚手架）。详见 /docs 与 API接口规范.md"})


if __name__ == "__main__":
    import uvicorn

    # reload 默认关闭：在 E:\ 共享盘/沙箱下不生效，且会多拉 worker 孙进程造成关不掉的孤儿端口进程（见项目记忆 ⑤）。
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
