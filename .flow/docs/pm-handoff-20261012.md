# PM 交接文档（2026-10-12 深夜）

> 交接人：本窗口 PM｜接手人：下个对话 PM｜权威状态以本文件 + 06 手册 + CHANGELOG + 库实测为准
> 开工第一件事：`agent-flow status` → 读 `docs/06-AI工作手册.md`（唯一入口，已合并全部纪律）→ 再读本文件

## 一、当前全景（全部已提交推送至 `5fbf745`）

- **库终态（P-CHARSPLIT 拆表完成）**：`plot_templates` **1106 行纯现役剧情弧**（arc active 1105 + reviewed 1）+ `character_templates` **265 行角色模板**（独立表/独立门牌/唯一读写入口）。全库职责单一，物品/技能六表本就独立零交集。
- **模板线**：四来源 arc active 1106（skel_v4_2 978 + zx 42 + hist 37 + cr 49）；词表 91 环（atomic_events 唯一权威）；角色模板 265（origin=f8_p0_anonymized，F8 P0~P4 关账）。
- **归档区**：`E:\归档文件\AI小说创作\` 三个批次（文档大清理 43524 文件 / 运行日志 81 项 / 底片），各有 `_归档清单.md` 可逆向恢复。
- **flow 图**：152 节点，P-DOCGOV/P-F7A completed、P-CHARSPLIT in_progress（主体完成留尾款）、P-KEYROUTE/P-HISTSRC 等挂账。
- 最近提交链：`9d93ee2(SK05N) → c584b38(F7A) → 7faa704(SK05K口径) → 4b60eb0+abce01f(文档治理) → 5fbf745(P-CHARSPLIT)`。

## 二、本次会话关键裁定（新 PM 必读，防重复踩坑）

1. **记忆体系终案（用户拍板）**：**flow 图为主** + 分层文件。开工=status，收工=四件套（flow validate+render → 02/03 对账 → CHANGELOG → 提交）。L1 白名单 5 份=docs/01/02/03/05+CHANGELOG；L2=.flow/docs（文档头四件套≤100 行）；坑库=docs/04-踩坑档案.md **v2**；归档=E:\归档文件\。
2. **权威文件纪律**：重写优先于追加；完成即瘦身（历史只留一行进 CHANGELOG）；替换走**安全流程**（先写新文件→原件移 E:\归档文件\→新件更名上岗）；小教训只加一行。切原子任务单唯一格式权威=`docs/任务单模板-切原子.md`。
3. **04-踩坑档案已 v2 重组**：原编号是引用锚点禁重编；新坑从 **F 区**起（F1 网关死亡/F2 JSON deepcopy/F3 agent-flow 不落盘+串台/F4 PowerShell5/F5 备份 default=str/F6 batch_import/F7 单测姿势/F8 非相邻重复盲区/F9 双重编码）。06 手册合并了 agent-flow 流程，**双入口已终结**（AIREADME 只是指向页）。
4. **P-CHARSPLIT 拆表要点**：新表展开列+structure 原文保底列；沿用原 id→三路向量零重嵌；plans.py 建卡反查**双查过渡**（PlotTemplateORM→CharacterTemplateORM）；写角色模板只走 `char_template_crud`（update 自动同步原文列）；`desc` 列名改 `slot_desc`（SQL 关键字）。
5. **F7A 已验收关账**（我是掌门停闸修复，跑批 2854/2854=100%）；**遗留**：库内 15 组/34 章非相邻逐字重复（B20 相邻比对盲区，04-F8）——**处置待用户拍板，勿擅动**。
6. **归档纪律**：移出前先 grep 单测/脚本对目标目录的引用（本次 skel_v4 测试夹具被误移，单测 6 红当场抓回）。

## 三、挂账（按优先级）

| 事项 | 状态 | 下一步 |
|---|---|---|
| **P-KEYROUTE 过堂** | 到期（冷静期 10-10 过），最高优先 | 分段取父设计定稿会（15~30 分钟），用户拍板后立项出单；设计全文在 flow 节点 P-KEYROUTE description |
| **15 组/34 章非相邻重复处置** | 待用户拍板（04-F8） | 删行重跑 or 留档观察 |
| **增类轮商讨** | 证据包就位（outputs/sk06b/增类并集清单.md） | 用户逐类拍板（SK02c 机制） |
| P-CHARSPLIT 尾款 | vector_chunks 分域评估 | 只评估不动手（5 种 source_type 共用 KNN 表） |
| 04 v2 瘦身尾巴 | 待做 | 10 号文档对账 91 环、03 巨型行、7 份超限功能文档、09-数据模型设计归档（先核实 05 覆盖度） |
| 第二批死表清理 | 用户指定下批做 | outlines 弃/relations 收口/eval_records 去留/lv_standards 查数据下落 |
| 史书第五批放量 | 点单启动 | outputs/史书模板任务包/ 现役契约（91 环版），交回走三板斧+SK05M 同款回填 |
| 橙红 c846+ / DATA01c / F6D1 修真四万年 | 点单启动 | 详 HANDOFF 旧账与 outputs/f6d1/ |

## 四、铁律速查

完整版在 `docs/06-AI工作手册.md`（§2 铁律）+ `docs/04-踩坑档案.md` v2（F 区）。最常踩的五条：

- 单测：`.venv\Scripts\python.exe -m pytest backend/tests/unit`，设 `NA_LOG_DIR` 临时目录，**不加 -q**（04-E21/F7）
- 动生产库：先 DB 快照+行级 JSON 双备份（禁 default=str，04-F5）；入库前探活 9377 网关、跑完 verify 向量块>0（04-F1）
- JSON 列：deepcopy 再改（04-F2）；查 JSON 列禁 LIKE 中文（04-E24）
- agent-flow：update_node 不落盘要 status 复核，不行直接编辑 flow.json（04-F3）；MCP 带 project_root
- 派单：PM 出单用户人工中转；执行方交付必带官方校验 PASS；PM 验收三板斧（亲跑机器闸/独立复算/开膛抽检）

## 五、关键文件索引

- 06 手册（唯一入口）：`docs/06-AI工作手册.md`｜坑库：`docs/04-踩坑档案.md` v2
- 判类键体系：`outputs/sk05h/判类提案_62弧.md`（57 大类）｜回填规格：`outputs/sk05h/SK05K_回填规格.md`
- 三线现役契约：`outputs/cr300/`（任务单已升 docs/）、`outputs/zx300/`、`outputs/史书模板任务包/`
- 词表同步点（91 环）：库 atomic_events + `outputs/原子事件词表速览.md` + cr300/史书校验脚本副本
- 拆表底片：`outputs/_backup/plot_templates_全表_拆表前_20261012.json` + `归档880_20261012.json` + DB 快照 `novel_agent.backup-20261012-拆表.db`
- F7A 裁决与台账：`outputs/f7a/`（停闸裁决/进度台账/退化未解决.json——13 条已自然补齐）
