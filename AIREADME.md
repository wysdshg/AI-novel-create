# AIREADME · AI 助手任务入口

> **本文件写给 AI 助手**（Claude Code / WorkBuddy / OpenCode / 其他 agent）：接手本仓库任务时，从这里开始。
> 人类访客请看 [`README.md`](README.md)（项目介绍与快速开始）。

---

## 0. 三句话认识这个项目

- 本机**单人**长篇小说写作辅助工具：后端 FastAPI + SQLAlchemy 2.0 + SQLite，前端 Vue 3 + Element Plus + Pinia。
- **定位红线**：不产品化、不上云、不做多用户、不做移动端。任何"顺手加个登录 / 部署 / 多租户"的想法都超出范围，先去看 [`docs/01-项目目标.md`](docs/01-项目目标.md) 的不做清单。
- 所有信息以 `docs/` 下 **8 份编号文档**为准（见下表），其他文档均已过时并归档在 `docs/_archive/` —— **常规任务不要读**。

## 1. ⚠️ 开工第一件事

**无条件先读 [`docs/06-AI工作手册.md`](docs/06-AI工作手册.md)** —— 它是唯一一份 AI 必读文档：

> 索引（这次任务该读哪份）+ 🔴 铁律（违反即回退）+ 改动前强制检查 + 常见误判 + 提交前自检清单。

## 2. 权威文档（唯一可信来源）

| 文件 | 内容 |
|---|---|
| [`docs/06-AI工作手册.md`](docs/06-AI工作手册.md) | **开工必读**：索引 / 铁律 / 常见误判 |
| [`docs/01-项目目标.md`](docs/01-项目目标.md) | 目标 · 边界 · 成功标准 · 不做清单 |
| [`docs/02-已实现清单.md`](docs/02-已实现清单.md) | **实测版**功能状态（哪些真能用，逐条带验证方式） |
| [`docs/03-规划与进行中.md`](docs/03-规划与进行中.md) | 路线图 Phase 0~6 · 模块去留决策 |
| [`docs/04-踩坑档案.md`](docs/04-踩坑档案.md) | 踩坑记录（**改提示词 / 解码参数 / 适配器之前必读**） |
| [`docs/05-架构与接口.md`](docs/05-架构与接口.md) | 架构 · 路由 · SSE 事件 · 数据模型 · 环境变量 |
| [`docs/07-任务分发手册.md`](docs/07-任务分发手册.md) | 多 agent 任务投递单规范 |
| [`docs/08-遗留问题总表.md`](docs/08-遗留问题总表.md) | **唯一遗留清单**（未解决/半解决/文档失真项，清一项勾一项） |

改动记录见 [`CHANGELOG.md`](CHANGELOG.md)。

## 3. AI 工作环境事实（跨会话必知）

| 事项 | 说明 |
|---|---|
| Python 环境 | **必须**用仓库内 `.venv\Scripts\python.exe`（Py 3.12），不要用全局 Python，也不要升级 venv 里的 pip |
| 启动方式 | 用户**手动开两个 PowerShell 窗口**分别启动前后端（见下方命令）；`start_project.ps1` 存在但用户不用 |
| 改后端后 | **必须提醒用户手动重启** —— `dev.py` 默认 `reload=False`，且该环境下 reload 监听不可靠 |
| 前端验证 | `cd frontend && npm run build -- --outDir dist-test`（默认 `npm run build` 清 `dist` 时沙箱安全钩子可能拦截） |
| 单元测试 | `cd backend && ..\.venv\Scripts\python.exe -m pytest tests/unit/`（秒级，**改任何后端代码后必跑**） |
| curl 本机 | 一律加 `--noproxy '*'`（系统代理会劫持 localhost） |
| 数据库 | `C:\Users\<用户>\.ai_novel\data\novel_agent.db`（**在仓库之外**，已 gitignore 兜底）；SQLite 无迁移工具，`init_db` 只能自动加列 |

启动命令（**用户手动执行**，AI 不要代起长驻服务；AI 自己临时验证用 8010+ 端口 + `DEV_RELOAD=0`）：

```powershell
# 窗口 1 —— 后端（端口 8000）
cd backend; ..\.venv\Scripts\python.exe dev.py

# 窗口 2 —— 前端（端口 5173）
cd frontend; npm run dev
```

## 4. 测试

```bash
cd backend

# 单元测试（秒级，改任何后端代码后必跑）
..\.venv\Scripts\python.exe -m pytest tests/unit/

# 端到端（分钟级，改生成 / 检索 / 按需加载后跑）
..\.venv\Scripts\python.exe tests/e2e/test_full_chain.py        # 生成质量基线
..\.venv\Scripts\python.exe tests/e2e/test_retrieval_chain.py   # 检索注入验收
..\.venv\Scripts\python.exe tests/e2e/test_on_demand_load.py    # 按需加载两阶段协议
```

测试规范见 [`backend/tests/README.md`](backend/tests/README.md)。e2e 脚本自带清理，创建的测试作品跑完即删。

## 5. 🔑 密钥红线（⚠️ 切勿提交到仓库）

所有密钥只存**本机数据库**，不写进任何代码文件：

| 用途 | 存放位置 |
|---|---|
| 对话 / 章节生成模型 | `model_configs` 表（界面「模型配置」维护） |
| 向量检索（硅基流动 bge-m3 / reranker） | 环境变量 `NA_SILICONFLOW_KEY`，或界面写入 `app_configs.retrieval.siliconflow_key` |

`.gitignore` 已排除 `.workbuddy/`、`backend/data/`、`frontend/dist*/` 等；提交前建议自查：

```bash
git grep -n "sk-" -- .        # 应为空（已提交内容）
git status --short            # 确认没有 .env / *.db / 密钥文件
```
