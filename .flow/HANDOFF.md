# 交接文档（HANDOFF）——给下一个接手的 AI Agent（PM）

> 交接时间：2026-10-06 晚｜上一会话：PM（ZCode）全天班
> **当本会话完成**：SK06 状态核查 + **执行顺序纠偏（判类整体后移至重构后）** → **F6R 弧层重构全轮关账**（393 非好弧 → 496 新弧独立新库落库）→ **SK06B 判类轮全轮关账**（1033 条判类落定）→ SK07 v2 增补 + dry-run 停闸裁决（978 张）+ apply 前置三问（含 **265 关键拦截**）+ 备份快照验证。最新 commit `43cc23a`，flow validate 全绿。
> **你的第一件事大概率是**：SK07 apply 跑到**向量重建中断的中间态**——模板行已全落、向量只索引了 393/978 源。让执行方续跑（指南见 §5，SQL 现成），verify → 重组报告 → 终验关账。

---

## 1. 项目一句话

AI 网络小说创作智能体（FastAPI + Vue3 + SQLite/sqlite-vec）：作者建书 → AI 按篇规划逐章生成正文，配套记忆摄取/检索注入/情节骨架/角色模板/条目库全链路。单人 + 多 AI 接力开发。

## 2. 文档地图（各管什么）

| 文档 | 职责 |
|---|---|
| `docs/06-AI工作手册.md` | **每次开工必读**：索引 + 铁律 + 常见误判 |
| `docs/02-已实现清单.md` / `docs/03-规划与进行中.md` | 能力清单 / 唯一路线图 |
| `docs/04-踩坑档案.md` | 动手前先查；**E23 已升格为家族**（窗口内重编号/atom_no 分卷/ALL_SUM 唯一键/两套 gid 体系——统一教训「假设唯一键本身是铁律，依赖处 assert」） |
| `.flow/docs/f6-resume.md` | **F6 修仙扩容线全记录**：本轮全部动态（PM 接班动态节逐条） |
| `.flow/docs/skel-v4.md` | v4 模板库线（SK01~SK05c 关账记录） |
| `.flow/docs/p3-inject.md` / `.flow/docs/data-clean-narrative.md` | P3 检索注入线 / 素材清洗线 |
| `outputs/task-*.md` | 任务单（gitignore 不入库）；本轮新：task-INTERN-F6R.md / task-INTERN-SK06B.md / task-DEV-SK07.md（**v2 增补版**） |
| `outputs/f6r/` | F6R 产物：**弧库_F6R.json（独立新库 496 弧，来源.原弧gid 回溯）** + 裁决×5 + 回信 |
| `outputs/sk06b/` | SK06B 产物：**判类结果_重构后.md（1033 行主件）** + 判类_全量1033.json + 增类并集清单.md + 裁决×3 |
| `outputs/sk07/` | SK07 产物：SK07_dryrun.txt / SK07_apply.txt / **SK07_backup_预览.json（回滚锚点 616 条）** / 裁决×2 + 回信 |
| `.flow/flow.json` | 流程图，**PM 独家写**；本轮新增 F6R（completed）/SK06B（并入 SK06 节点 completed）/SK08（planned） |
| `CHANGELOG.md` | 每次改动一行（本轮已记 F6R/SK06B 两条关账） |

## 3. 工作铁律（旧版全有效，本轮新增/强调）

1. Python 只用 `.venv\Scripts\python.exe`（A15）；网关 9377 必须流式（`E:\MyAPI\start-uniapi.bat`）；生产库只读探查用 `mode=ro`（库在 `~/.ai_novel/data/novel_agent.db`）。
2. 「弧→原子」解析多方法交叉一致才信（E23 终态）+ **「假设唯一键」本身是铁律：凡依赖唯一性的地方 assert 一次**（E23 补记 2；本轮 four 连发：蛊真件 seq 歧义 / ALL_SUM dict 覆盖 / 两套 gid 编号体系 / atom_no 分卷）。
3. **执行渠道矩阵**：OpenCode 免费子代理为主力（内容生成零 API——判类/重编/正文）；**embedding 是索引计算走网关，不算内容生成 API**（SK07 向量重建 2934 块与 SK03 2907 持平）。回信落 `outputs/<目录>/reply-*.md` 由用户转交（E12）。
4. **Windows 文本模式写库会把 LF→CRLF**（SK06B 实测，回滚校验拦下）：一律二进制写 + 回滚真实往返。
5. 收尾四件套：flow validate+render → docs/02/03 → CHANGELOG → git 提交；执行方脚本记得入库。
6. 单测基线 **857**（SK07 判据改造 +2）。
7. **PM 验收三板斧（本轮定型，沿用）**：①亲跑机器闸（t37/t08/t05 等，执行方给复算入口）②独立复算（**不复用执行方代码**，自写一次性脚本从源文件重推）③抽原文开膛（PM 亲自读，如 R11/R60/R44）。**不信子代理自报 dict——数字必须现场重算**（SK06B 利用率 795 vs 812 案例）。
8. **执行方停闸文化**：对表不过 / 硬闸不闭 / 安全策略拦截都会主动停——PM 要快速裁决防停滞；**所有裁决一律落盘 `outputs/<dir>/pm-verdict-*.md`** 由用户回贴。

## 4. 当前状态总览（2026-10-06 晚交接时）

| 线 | 状态 |
|---|---|
| P3/P3a、F6a/b/c、F6D2、SK04、SK05/05b/05c、F6Q、DATA01b | 🟢 均关账（沿旧，详见 f6-resume） |
| **F6R 弧层重构** | 🟢 **关账**：393 非好弧（88 块/115 孤立+5 书缘）→ 锚定三轮+走①原文级开膛（R66 背书修订=线随章段走）→ 铺开 94/94（好弧 359/多线 41/散件 96+过渡拍 9，利用率 91.9%）→ apply 落独立新库 `outputs/f6r/弧库_F6R.json`。PM 亲膛 R11/R60/R44 三项全过。E23 家族两记 |
| **SK06B 判类轮（后移版 SK06）** | 🟢 **关账**：1033 条判类落定（LLM 713+确定性 320）；激活母题 139/149、大类 57/57；置信 高207/中452/低54+待定组 33；C1~C15 全过+C14 降档 29；弧库两处授权改动落库（主轴回写+台账补齐，sha c5b1553a→0ebab57c） |
| **SK07 rebuild v4.2** | 🔴 **apply 中间态（详 §5）**：模板行全落（978 v4.2 active+616 archived+265 角色完好），**向量重建中断（393/978 源）**；verify/restore-check/重组报告未跑 |
| SK08 语义聚合试算轮 | ⏸ planned（flow 已立项：bge-m3 cos，F4 验证过技术；新判据需试算→标定→样张→用户拍板） |
| 增类轮 | ⏸ 证据包就位（`outputs/sk06b/增类并集清单.md` 23 方向+61 长尾+待定 33+低置信 54）——**SK02c 机制，用户逐类拍板**，PM 勿代拍 |
| SK06B 尾巴 | ⏸ 「2 拍择重判中」抽 20 条复检（验 P4 判则）+ gid103/107 偏轴备注补写 |
| 绍宋 25 弧 | ⏸ 挂 backlog：形态普查+判类同轮做（无 F6Q 普查数据，本轮判类跳过） |
| F6D1 修真四万年 | ⏸ 挂起（Qoder 限额；单+751 章概括已备 `outputs/f6d1/`），切完走判类补判 |
| DATA01c | ⏸ backlog 累积（详 §6） |

## 5. SK07 卡点精确续作指南（新 PM 最先读）

**已完成**：v2 增补任务单 → dry-run 978 张超预估停闸 → **裁路①照实落库**（三条归因实证：降阈值/换核心拍全无效；83 大键零合并=重构后每条弧是独立因果链，吸收 5.3% 已是 SK03 零合并的量级提升；硬凑重蹈覆辙）→ apply 前置三问（问 2 裁甲 origin=skel_v4_2+归档 616；**问 3 关键拦截**：PM 查库实锤 265=character active 角色模板非归档，「清非本轮向量」照跑会断选角检索——正确口径=只清被换代 616 块+retire 补调 remove_source+verify 零孤儿按 **1243 源（978+265）** 断言）→ 备份快照验证（`outputs/sk07/SK07_backup_预览.json` 616 条，与 character 零相交，回滚=一句 UPDATE）→ 执行方 7 处修订+857 绿+dry-run 复现。

**库中间态（接手先跑这条 SQL 确认）**：
```sql
-- ~/.ai_novel/data/novel_agent.db (mode=ro)
SELECT pt.scale, pt.status,
  SUM(json_extract(pt.source_stats,'$.origin')='skel_v4') v41,
  SUM(json_extract(pt.source_stats,'$.origin')='skel_v4_2') v42
FROM plot_templates pt GROUP BY pt.scale, pt.status;
-- 交接时实况：arc active 0/978(v4.2)、arc archived 616/0、character active 265
SELECT vc.source_type, pt.scale, pt.status, COUNT(*), COUNT(DISTINCT vc.source_id)
FROM vector_chunks vc LEFT JOIN plot_templates pt ON vc.source_id=pt.id
WHERE vc.source_type IN ('plot_template','plot_cast','char_archetype')
GROUP BY 1,2,3;
-- 交接时实况：plot_template arc active 仅 393 源/1895 块（缺 585 源）
```

**续作指令（贴给执行方，SK07 原对话）**：
```
apply 在向量重建阶段中断（库实况：模板行 978+616 归档+265 角色已就位，
plot_template 弧级向量只索引 393/978 源）。续跑：①对缺的 585 源续跑
index_template（幂等先删后建，重跑安全；265 角色模板三路块禁碰）→
②--verify（零孤儿按 1243 源=978+265 断言）→ ③--restore-check →
④重组报告（多成员清单+变体后缀+孤例清单+形态标分布+绍宋 25 退场记录+
SK03_dryrun 恢复件对账节）。报告落 outputs/sk07/，回信追加节。
```

**PM 终验要点**（任务单 §四五条）：单测绿 / 模板数 978 有解释 / 抽 3 张多成员模板核溯源（含弧库 来源.原弧gid 链）+形态标与分线键备注落位 / 幂等+回滚+零孤儿 1243 / 绍宋退场记录。

**git 未提交**（apply 关账时一起）：`skel_v4_rebuild.py`（7 处修订）+ `test_skel_v4.py` + `x2_db_status.py` / `x3_preview_backup.py`（新）。

**裁决文件**：`outputs/sk07/pm-verdict-DEV-SK07-dryrun.md`（978 裁路①+SK08 立项）+ `pm-verdict-DEV-SK07-apply前置.md`（甲方案+向量口径拦截）。

## 6. backlog（有主人，全列防丢）

| 项 | 去向 |
|---|---|
| 增类轮商讨（23 方向+61 长尾+待定 33+低置信 54） | 用户逐类拍板（SK02c 机制） |
| SK06B 尾巴：2 拍择重判中抽 20 复检 + gid103/107 备注补写 | 随增类包 |
| SK08 语义聚合试算（bge-m3 cos） | 立项 planned，主线后 |
| 绍宋 25：形态普查+判类同轮 | backlog |
| DATA01c 累积：太荒解析缺陷 / R41 缺章 / **原子 summary 专名泛化（按专名检索失效→切原子提示词加专名保真）** / 蛊真缺 c121/c124 / F2 粗拍链（低利用 13 块根因）/ gid400 元数据噪声 / 北派注入 / 凡人章号偏移 / 重述拍 / 寒门污染拍 | 素材整备轮 |
| 旧 backlog：低置信 49 模板级去留 / merge_arcs_crossbook.py:140 A17 残留 / 绍宋 singles 改实义名 / 测试书用后删 / 斗破 14 行概括 / P5 判则书面化 / QA S1/S2/S6 / F6D1 挂起 / 遮天 c1700~1702 D08 误标 / 军制 3 条回填 A10（一行）/ 凡人 p2 34+寒门 26 流程话术 / F07 措辞盲区（农技） / t20 工具期望值过期 | 各归其位 |
| 本地 embedding 通道（脱网关依赖，可选优化） | 未立项，用户提再做 |

## 7. 用户协作偏好（历届验证 + 本轮新增）

- 全程中文大白话；多表格、一句话结论、✅/🟡/⏸；分歧列选项附后果+建议票。
- **用户一句话纠偏大方向（本轮两次，皆对）**：「SK06就停在试判」（纠正执行顺序：判类后移至重构后）、「SK07并未开始」（纠正进度认知）——**用户陈述与文档冲突时，先核查现场证据再信文档**；用户对产品方向的一等直觉被驳斥前先设计实验（走①模式）。
- **用户会把执行方截图/回信直接转来问 PM**——PM 亲查库/亲跑命令核实后再裁，不照转述裁（265 拦截：执行方「清非本轮向量」若跑会断选角检索，PM 查库 30 秒避免事故）。
- 用户在乎架构性「要不要调 API」——讲清 embedding（索引计算走网关）vs 内容生成（子代理零 API）的区分。
- 「不要自己猜测，找实际样本看」：推断必须开膛验证。
- 讨论结论当场落盘；单测全绿 → 提交；回执与实物核对（抽查命令亲跑）。

## 8. 给用户的可粘贴开场白（交接即用）

```
读取 E:\AI小说创作\.flow\HANDOFF.md 和 AGENTS.md，你是编排台 PM，用
get_project_status 了解 flow 图现状，然后继续工作。当前主线：SK07 apply
跑到向量重建中断的中间态（模板行已全落 978+616 归档+265 角色完好，向量
只索引 393/978 源）——按 HANDOFF §5 的续作指南让执行方续跑向量重建+
verify+重组报告，然后 PM 终验关账。之后主线后队列：增类轮商讨（证据包
就位待我逐类拍板）、SK08 语义聚合。先读 .flow/docs/f6-resume.md 的
「PM 接班动态」节再动手。
```
