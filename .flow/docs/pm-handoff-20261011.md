# PM 交接文档（2026-10-11 晚）

> 交接人：本窗口 PM｜接手人：下个对话 PM｜权威状态以本文件 + CHANGELOG + 库实测为准

## 一、当前全景（全部已提交推送）

**模板库 arc active = 1106 张，四条来源线全部同构**（v4.2 契约：四段走法+标签+SK03 原式切段+display_top=5+十项溯源+孤例标）：
- 978 小说（skel_v4）+ 42 赘婿（zx_v1）+ 37 史书（hist_v1）+ 49 橙红年代（cr_v1，SK05N）
- 词表 91 环（atomic_events 表是唯一权威；86→88 SK05F-2 → 91 SK05M：+A14 人质劫持·对峙营救/+A15 循线围捕·缉凶擒拿/+C22 审讯逼供·问案对峙，H13 定义放宽）
- 最近提交链：41fd991（切段重排）→ a0c4fc7（zx 走法回填）→ 55d2cd6（hist 走法回填）→ c5e3cea（橙红试水批词表）→ 9d93ee2（SK05N 橙红入库）

## 二、今天的关键裁定（新 PM 必读）

1. **模板标准形态权威 = 库内现役数据实测**（_全字段实测.py），不是脚本文件——v4.2 有 rebuild 后增补字段，脚本里没有
2. **切原子默认概括切**（F6T A/B 实验裁决：差异小、成本 1/60）；盲区=能力/设定交代句易压掉，任务单必须写「设定交代句保留」；分批 ≤35 章、批边界 ±3 章背景、主会话跨批锁定衔接
3. **走法四段**「起：…。｜承：…。｜转：…。｜合：…。」80~200 字软上限 250；标签 2~4 个（2~6 字）；**网文线匿名**（人名/势力/地名→通用词，品牌器物名可保留），**史书线留真名**
4. **新环作键必须挂既有大类的子事件位**（如 大战征伐--循线围捕·缉凶擒拿），环名直接当大类会污染 key_vocab
5. **边界防腰斩机制 = 末弧回炉 + 背景章**，不是产出重叠窗口——章号严格不重叠是铁律（cut_atoms_from_chapters.py 第 155 行）
6. 重构"假大类"教训：判类键只能从既有 57 大类选，弧名不进键（弧名存 source_stats/member_arcs）

## 三、挂账（按优先级）

| 事项 | 状态 | 下一步 |
|---|---|---|
| **P-KEYROUTE 过堂** | 今天（10-11 应过，拖到 10-12） | 分段取父设计定稿，见 .flow/docs/ 相关节 |
| F7A（我是掌门概括停闸） | 已裁 A+C 修复，授权 OpenCode 执行中 | 等回信验收：B20 补跑漏闸+流式 rate 泄漏；裁决书 outputs/f7a/pm-verdict-停闸裁决.md |
| 史书第五批放量 | 随时 | 同史书契约（91 环版），交回后走三板斧+SK05M 同款回填 |
| 橙红年代放量批 c846+ | 未派 | 845 章已覆盖全部（c2~c845），视用户是否扩库 |
| c243 诱骗入局 / c539 毁证护产 | 挂待定观察 | 凑够多例再议开环 |
| 庞勋弧 | 撤下 | 执行方联网复核合格可补 |
| zx/hist/cr 三线的待定拍族 | 已改判入库 | 无尾款 |

## 四、铁律速查（完整版在项目记忆）

- 单测：`.venv\Scripts\python.exe -m pytest backend/tests/unit`，设 NA_LOG_DIR 临时目录 + --junitxml
- E24：JSON 列禁 LIKE 中文（json_each 或 Python 侧）；SQLAlchemy JSON 原地改不触发变更（deepcopy）
- uni-api 网关 9377 会静默死亡：入库前探活；**入库后必须 verify 向量块>0**（index_template 静默吞错返回 0）；死了启动 E:\MyAPI\start-uniapi.bat 再 reindex
- 动生产库前备份（Copy-Item novel_agent.db novel_agent.backup-<日期>-<主题>.db）；行级备份勿用 default=str
- PowerShell 5：多行 commit 用 `git commit -F <文件>`；agent-flow update_node 不落盘，改图后必须 status 复核，不行直接编辑 .flow/flow.json
- 派单：PM 出单，用户人工中转给 Qoder（通讯系统疑似故障）；Qoder 交付必带官方校验 PASS 输出
- 新想法记 to_plan 节点，三天冷静期；WIP 上限 1 主线+1 支线

## 五、关键文件索引

- 判类键体系快照：outputs/sk05h/判类提案_62弧.md（57 大类）；_键体系.py 可复跑
- 回填规格（v4.2 十项契约）：outputs/sk05h/SK05K_回填规格.md
- 三线生产产物：outputs/zx300/、outputs/史书模板任务包/、outputs/cr300/（试水批在 留档_试水批_c2-300/）
- outputs 清理台账（防旧口径误导）：outputs/清理台账_20261008.md
- ingest 脚本范式：backend/scripts/sk05n_cr_ingest.py（判类 MAP+RING_PATCH+十项 source_stats）
- 词表同步点（91 环）：库 atomic_events + outputs/原子事件词表速览.md + cr300/校验脚本.py + cr300/输出格式契约.md + 史书校验脚本副本
