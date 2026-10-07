# 交接文档（HANDOFF）——给下一个接手的 AI Agent（PM）

> 交接时间：2026-10-07｜上一会话：PM（ZCode）
> **当本会话完成**：SK06 状态核查 + 执行顺序纠偏（判类后移至重构后）→ **F6R 弧层重构全轮关账**（393 非好弧 → 496 新弧独立新库）→ **SK06B 判类轮全轮关账**（1033 条判类落定）→ **SK07 rebuild v4.2 全轮关账**（v2 增补 → dry-run 停闸裁决 978 张 → apply 前置三问含 **265 关键拦截** → apply 六步 → PM 终验复抽 3 张全过）→ **前端验证（978 张可见）**。最新 commit 至 `f6-resume` 前端验证补记，flow validate 全绿。
> **大主线已收官**：F6Q 普查 → 弧层重构（F6R）→ 判类（SK06B）→ rebuild（SK07），双层模板库**弧级层成型**（1033 弧 → 978 张 v4.2 模板）。**无紧急卡点**——后续全是「用户点单才启动」的主线后队列（§6），最近的拍板项是增类轮商讨。

---

## 1. 项目一句话

AI 网络小说创作智能体（FastAPI + Vue3 + SQLite/sqlite-vec）：作者建书 → AI 按篇规划逐章生成正文，配套记忆摄取/检索注入/情节骨架/角色模板/条目库全链路。单人 + 多 AI 接力开发。

## 2. 文档地图（各管什么）

| 文档 | 职责 |
|---|---|
| `docs/06-AI工作手册.md` | **每次开工必读**：索引 + 铁律 + 常见误判 |
| `docs/02-已实现清单.md` / `docs/03-规划与进行中.md` | 能力清单 / 唯一路线图 |
| `docs/04-踩坑档案.md` | 动手前先查；**E23 已升格家族**（窗口内重编号/atom_no 分卷/ALL_SUM/两套 gid 体系——统一教训「假设唯一键本身是铁律，依赖处 assert」）；**E24 新增**（查库要带 scale/status 维度标签 + JSON 列中文是 \uXXXX 转义、`LIKE '%中文%'` 恒 0——用 json_extract/json_each） |
| `.flow/docs/f6-resume.md` | **F6 线全记录**：「PM 接班动态」节逐条记本轮全部动态（含 SK07 交付记录+终验+前端验证） |
| `.flow/docs/skel-v4.md` / `p3-inject.md` / `data-clean-narrative.md` | v4 模板库线 / P3 检索线 / 素材清洗线 |
| `outputs/task-*.md` | 任务单（gitignore 不入库）；本轮新：task-INTERN-F6R.md / task-INTERN-SK06B.md / task-DEV-SK07.md（**v2**） |
| `outputs/f6r/` | **弧库_F6R.json（独立新库 496 弧，来源.原弧gid 回溯）** + 裁决×5 |
| `outputs/sk06b/` | **判类结果_重构后.md（1033 行主件）** + 判类_全量1033.json + 增类并集清单.md + 裁决×3 |
| `outputs/sk07/` | **SK07_重组报告.md（24 万字 §0–§13）** + SK07_backup_预览.json（回滚锚点）+ SK07_apply/verify/restore_check 日志 + 裁决×3 + SK07_库现状.txt |
| `.flow/flow.json` | 流程图，**PM 独家写**；本轮新增 F6R（completed）/SK08（planned），SK06 并入判类轮口径后 completed |
| `CHANGELOG.md` | 每次改动一行（本轮已记 F6R/SK06B/SK07 三条关账） |

## 3. 工作铁律（旧版全有效，本轮新增/强调）

1. Python 只用 `.venv\Scripts\python.exe`（A15；系统 python 缺 sqlite_vec 会假红 4 例）；网关 9377 流式（`E:\MyAPI\start-uniapi.bat`）；生产库只读用 `mode=ro`（库在 `~/.ai_novel/data/novel_agent.db`）；backend 起法=`backend` 目录下 `../.venv/Scripts/python.exe main.py`（不是 uvicorn app.main:app）。
2. **「假设唯一键」本身是铁律：凡依赖唯一性的地方 assert 一次**（E23 家族四连发：蛊真件 seq 歧义 / ALL_SUM dict 覆盖 / 两套 gid 编号体系 / atom_no 分卷重编号）。
3. **执行渠道矩阵**：OpenCode 免费子代理为主力（内容生成零 API——判类/重编/正文）；**embedding 是索引计算走网关，不算内容生成 API**。「零 API」表述要精确（SK07 回信有标准说法：脚本层零 HTTP/不调 LLM 网关/子代理模型推理除外）。回信落 `outputs/<目录>/reply-*.md` 由用户转交（E12）。
4. **Windows 文本模式写库会把 LF→CRLF**（SK06B 实测，回滚校验拦下）：一律二进制写 + 回滚真实往返。
5. **库换代一律走甲案模式**：origin 版本标识（skel_v4 → skel_v4_2 → 将来 SK08 打 skel_v4_3）+ 旧版 archived 不删 + verify 口径带维度标签（json_extract/json_each，**禁 LIKE 中文**——E24）。
6. 收尾四件套：flow validate+render → docs/02/03 → CHANGELOG → git 提交；执行方脚本记得入库。单测基线 **857**。
7. **PM 验收三板斧**：①亲跑机器闸（执行方给复算入口）②独立复算（**不复用执行方代码**，自写一次性脚本从源文件重推；不信子代理自报 dict——SK06B 利用率 795 vs 812 案例）③抽原文/抽库开膛（PM 亲自，如 R11/R60/R44、复抽 3 张多成员模板）。
8. **执行方停闸文化**：对表不过 / 硬闸不闭 / 安全策略拦截都会主动停——PM 快速裁决防停滞；**裁决一律落盘 `outputs/<dir>/pm-verdict-*.md`** 由用户回贴；写生产库的操作用户会转截图问 PM——**亲查库后再裁**（265 拦截案例：执行方「清非本轮向量」会断选角检索，查库 30 秒避免事故）。

## 4. 当前状态总览（2026-10-07 交接时）

| 线 | 状态 |
|---|---|
| P3/P3a、F6a/b/c、F6D2、SK04、SK05/05b/05c、F6Q、DATA01b、F6R、SK06B | 🟢 全部关账（详 f6-resume「PM 接班动态」） |
| **SK07 rebuild v4.2** | 🟢 **关账+前端验证**：978 张 v4.2 active（origin=skel_v4_2）+616 v4.1 归档可逐行还原+绍宋 25 退场；向量 1243 源零孤儿；verify 20 断言全 PASS；PM 复抽 3 张多成员全过。**列表 API 口径**：`status=active` 返回 978；不带 status 返回 1858 混杂（978+880 归档）——前端记得筛 active |
| SK08 语义聚合试算轮 | ⏸ planned（**开单前置：样本分档输入改读 1033**，现读 v4.1 md 实测影响 0 张）。判据候选：拍概要语义向量 cos（bge-m3，F4 验证过）/cores Jaccard/tags。动机=83 个成员≥2 的大键形状聚合零合并（978 张诚实结果），语义聚合是收拢杠杆。流程：试算→阈值标定→样张→用户拍板 |
| 增类轮 | ⏸ **证据包就位**（`outputs/sk06b/增类并集清单.md` 23 方向+61 单源长尾+待定组 33+低置信 54）——SK02c 机制**用户逐类拍板**，PM 勿代拍 |
| SK06B 尾巴 | ⏸ 「2 拍择重判中」抽 20 条复检 + gid103/107 偏轴备注补写 + 块2 290 条出口列统一（低优先） |
| 绍宋 25 弧 | ⏸ 形态普查+判类同轮（无 F6Q 普查数据）；本轮 25 张旧模板已 archived 可还原 |
| F6D1 修真四万年 | ⏸ 挂起（Qoder 限额；单+751 章概括已备 `outputs/f6d1/`），切完走判类补判 |
| DATA01c | ⏸ backlog 累积（§6） |

## 5. 模板库资产速查（后续任何模板库操作先读这）

- **plot_templates**：arc active **978**（origin=skel_v4_2，变体后缀 957 张带 `-N`）+ arc archived **880**（616 v4.1 + 264 更早，含绍宋 25）+ character active **265**（三路向量在用，**任何向量清理禁碰**）。
- **vector_chunks**：plot_template 4875 块/1243 源（978+265）零孤儿；plot_cast 265；char_archetype 265。归档模板向量=0（retire 路径已补 remove_source）。
- **回滚锚点**：`outputs/_backup/plot_templates_arc_v3_20261006T154959Z.json`（616 条×12 列全字段）+ `outputs/sk07/SK07_backup_预览.json`；restore-check 三项全过。
- **溯源链**：模板 → source_stats.member_arcs（ident/book/arc/置信/form_tag/line_keys/origin_gids）→ 弧库_F6R.json（来源.原弧gid）→ win 文件原子（取数口径 `(书名,part,原子号)→atoms[原子号−1].summary`，**原子键叉乘字段禁直用**）。
- **判据现场值**：分段档 ≤3 拍 0.50 / >3 拍 0.75 + cores 单向；env 防呆 assert 已装（`--apply/--verify` 下非拍板值拒跑）。
- **库现状一键**：`./.venv/Scripts/python.exe backend/scripts/x2_db_status.py`（只读）。

## 6. 主线后队列（全部「用户点单才启动」，按成熟度排序）

| 队列 | 内容 | 启动条件 |
|---|---|---|
| **增类轮商讨** | 证据包就位（23 方向+61 长尾+待定 33+低置信 54）；「新政改革」拆键列首位 | **用户逐类拍板**（SK02c 机制）；顺带跑 SK06B 尾巴 20 条复检 |
| SK08 语义聚合 | 收拢 83 个零合并大键的杠杆；bge-m3 cos 技术现成 | 用户点单后 PM 出单（前置：样本分档改读 1033） |
| SK06B 尾巴 | 20 条复检 / gid103/107 备注 / 块2 出口统一 | 随增类轮或用户点单 |
| 绍宋小轮 | 形态普查+判类同轮（25 弧） | 用户想用绍宋素材时 |
| DATA01c | 十项累积（太荒解析缺陷/summary 专名泛化→切原子提示词加专名保真/蛊真缺章/F2 粗拍链/gid400 噪声/北派注入/章号偏移/重述拍/寒门污染拍） | 扩容灌书前清 |
| F6D1 修真四万年 | 单+概括已备，Qoder 限额挂起 | 限额恢复 |
| 旧 backlog | 低置信 49 模板级 / merge 脚本 A17 残留 / 测试书删除 / 斗破 14 行概括 / P5 判则书面化 / QA S1/S2/S6 / 遮天 D08 误标 / 军制 3 条（一行）/ 流程话术清理 / F07 措辞盲区 / t20 期望值过期 / 本地 embedding 通道（可选） | 各归其位 |

## 7. 用户协作偏好（历届验证 + 本轮新增）

- 全程中文大白话；多表格、一句话结论、✅/🟡/⏸；分歧列选项附后果+建议票。
- **用户一句话纠偏大方向（本轮三次皆对）**：「SK06就停在试判」（判类后移）、「SK07并未开始」（进度认知）、「现在是干嘛的」（要大白话现状）——**用户陈述与文档冲突时，先核查现场证据再信文档**；用户对产品方向的一等直觉被驳斥前先设计实验（走①模式）。
- **用户会把执行方截图/回信直接转来问 PM**——亲查库/亲跑命令后再裁，不照转述裁（265 拦截）。
- 用户在乎架构性「要不要调 API」——embedding（索引计算走网关）vs 内容生成（子代理零 API）要讲清。
- 「不要自己猜测，找实际样本看」：推断必须开膛验证。
- 讨论结论当场落盘；单测全绿 → 提交；回执与实物核对（抽查命令亲跑）。

## 8. 给用户的可粘贴开场白（交接即用）

```
读取 E:\AI小说创作\.flow\HANDOFF.md 和 AGENTS.md，你是编排台 PM，用
get_project_status 了解 flow 图现状，然后继续工作。大主线已收官（F6Q→
弧层重构→判类→rebuild，978 张 v4.2 模板已落库且前端可见），无紧急卡点。
工作模式=我点单你执行：当前队列见 HANDOFF §6（增类轮商讨需要我逐类
拍板，SK08 语义聚合已立项待出单）。先读 .flow/docs/f6-resume.md 的
「PM 接班动态」节和 §5 模板库资产速查再动手。
```
