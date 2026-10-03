# 无口述时模板检索兜底 + 显式选模板（F9·方案定稿待实施）

> 状态: planned | 建立: 2026-09-30 | 更新: 2026-10-03 | 上次验证: 2026-10-03（F95 骨架池恢复后 search 冒烟：命中全为 arc 骨架、beats=6；恢复脚本 scripts/restore_v3_skeletons_20261003.py）

## 为什么做（问题是什么）

1. 作者不写「剧情口述」（hint 空）时：q_list 为空 → 关键词兜底 → **0 命中** → 计划 origin='free'、每行 template_ref='无模板·自由设计'。模板库对这类作者完全不可见。
2. **2026-10-03 新发现**：10-02 f8_p3_swap 用 retire --status active 一刀切，把 100 条 v3 情节骨架误归档（用户意图只下角色模板）——篇规划模板参考池被清空，**有口述也检索不到骨架**（实测命中的全是 scale=character 角色模板，beats=0 注入不出节拍）。已由 F95 修复（100 条骨架 active + 三路向量 1738 块重建）。
3. 顺带查明 TemplateView 三个显示 bug：statusText 三元链兜底把 active 显示成"已归档"、来源读 source_stats.book_names 而 F8 数据键是 book、粒度 chip 无"角色模板"；且模板库页不按 scale 过滤导致 265 条角色模板混显（同表是设计，缺过滤是缺陷）。归 DEV-F9a 修复。

## 定稿方案（2026-10-03 用户拍板，四分支）

前端生成表单加「参考模板」可搜索下拉（默认"自动匹配"，列 active 骨架）：

| 场景 | 行为 |
|---|---|
| 有选+有口述 | 选中模板锁定注入 + 标注「作者指定」；可选点「AI 增强口述」（qwen3-8B 关思考）→ 增强口述**填进口述框作者可改** → 用增强口述再检索几个候选补充（选中模板保送候选集，不与候选打架） |
| 有选+无口述 | 选中模板锁定注入；可选点「AI 帮我写口述」（8B 依据模板+卷概要+前情生成）→ 填框可改 |
| 无选+有口述 | 现状不变：口述检索 top-4 骨架 |
| 无选+无口述 | 多路兜底检索（卷概要截 200 字 + 篇名 + 活跃角色前 8 名，search() 原生多路 RRF）→ 空/挂才随机抽 4~5 骨架并入规划 prompt 让 LLM 挑（**灵感触发器定位**，随机名单记 raw_ai） |

**关键取舍记录**：
- AI 增强/生成口述的产物**必须落在口述框可见可改**（不是隐藏管道）——同时治"口述简陋"痛点；
- 增强调用用 qwen3-8B 关思考（短输出任务，零成本快返回）；不预支大模型调用；
- 「作者指定」标注解决候选打架：作者选择的优先级最高；
- 随机 = 灵感触发器（用户定位），不追求可复现，但名单记账；
- 题材过滤暂不做（全池修仙系无区分度），**入库第一本异题材书时**给 genre_tags 打题材标签后一并上；
- 篇规划检索必须加 `scale='arc'` 过滤（DEV-F9a），角色模板留给建卡链（char_archetype 路）。

## 怎么验证

1. 不写口述生成计划 → origin='template'、template_ref 有真实骨架、命中的 beats 与卷概要相关；raw_ai 留兜底 query 账
2. 显式选模板生成 → 计划参考该骨架（模板名进 prompt/或 template_ref 指向）；「AI 增强口述」产物在口述框可见可改
3. force_free=True（自由规划开关）→ 仍完全跳过检索
4. 有口述 → 行为与现状一致（回归）
5. 检索故障（停网关）→ 随机降级触发且名单进 raw_ai，不阻断生成

## 代码位置

- backend/app/services/plan_crud.py（`_templates_for_plan` / `_plan_prompt` / `generate_plan`）
- backend/app/services/plot_template_crud.py（search 多路 query）
- frontend/src/views/PlanView.vue（生成表单 + 口述框）
- backend/app/routers/plans.py（generate 入参）

## 关联

- 前置 F95（骨架池修复，✅ 2026-10-03）；上游 F3（模板库）；下游 M7/P1（篇规划生成）
- 实施派单：DEV-F9a（scale 过滤 + TemplateView 显示修复，✅ 2026-10-03 实施完毕待 PM 验收）→ DEV-F9b（四分支主体）→ QA

## 实施记录

### DEV-F9a（2026-10-03，SWE 执行；半成品续用+验证补完）

> 开工时工作区已有上一执行者留下的未提交改动（3 文件，覆盖验收 1~5），经用户确认**续用**，本对话负责核对、跑测、浏览器实测与补完。

**改动文件（3 个，均无库表/公共模块改动）**：

| 文件 | 改动 |
|---|---|
| `backend/app/services/plan_crud.py` | `_templates_for_plan` 的 `tpl_crud.search` 调用加 `scale=tpl_crud.SCALE_ARC`（角色模板归建卡链，不再混进篇规划参考池） |
| `backend/tests/unit/test_plan_crud.py` | 新增 `TestTemplateSearchScaleArc` 2 例：①契约钉死 search 必带 scale='arc'；②同名 arc 骨架与 character 角色模板共存时只有骨架进结果（走关键词兜底路径，不依赖 sqlite-vec） |
| `frontend/src/views/TemplateView.vue` | ①`statusText` 补 `active→现役` 分支；②新增 `srcBooks(t)` 兼容 `book_names` 数组与 F8 单数 `book` 键（卡片/来源书下拉/客户端过滤三处统一用）；③新增 `scaleText`（arc→故事弧、character→角色模板、segment→情节段、其余原样），卡片 chip 与详情抽屉共用；④`loadList` 默认 `scaleFilter='arc'`、`statusFilter='active'`（后端 list 端点原生支持 status 参数，无需改后端）；⑤粒度下拉补「角色模板」选项、状态下拉补「现役」选项，两下拉均保留 clearable 可回全量考古 |

**验收逐条实测（2026-10-03，PM 沙箱环境：后端 8000/前端 5173/网关 9377）**：

1. ✅ scale=arc 过滤接线 + 2 新单测（见上）
2. ✅ `statusText('active')`→「现役」；`CharTemplateView.vue` 排查无同款三元链 bug（其 68 行已正确含 active 分支），未改
3. ✅ 来源显示：骨架卡显示多书名（"九星霸体诀、凡人修仙传、太荒吞天诀（3 书）"），角色模板单 book 键回退正常（"玄鉴仙族"），默认视图 0 张显示"—"；详情抽屉本就不显示来源书列表（只有 arc_refs），无同款问题
4. ✅ 粒度 chip 三态正确（卡片+抽屉都走 `scaleText`）
5. ✅ 默认「故事弧+现役」；重进页面默认值复位正常
6. ✅ 浏览器实测（截图 `gui-test-screenshots/f9a/01~05`）：默认视图 100 条骨架（与 DB active arc=100 一致），无角色混入，徽章全「现役」无错标；粒度切"角色模板"→ 265 条徽章全「现役」；人物模板库页（template-char）共 265 个不受影响
7. ✅ 篇规划真机冒烟（后端重启新代码后）：王从天降→第一卷/第一篇，口述"主角进入秘境夺宝"生成 → UI 徽章「模板规划」；DB 只读核对 `article_plans`：`origin='template'`，`template_ids` 4 个全部 `scale=arc, status=active`（秘境夺宝·拍卖竞价·吞噬异宝·反杀复仇/围困被困/闭关突破/单挑决斗·秘境夺宝·闭关突破·立威震慑），逐行 `template_ref` 指向骨架码（G02 秘境夺宝、D03 围困被困…）。截图 `06-plan-generate-dialog-hint.png`、`07-plan-generated-origin-template.png`
8. ✅ 全量单测 554 passed 全绿（基线 552 + 新增 2），`test_plan_crud.py` 12 passed

**执行决策/坑**：
- 默认过滤方案取"前端默认选『现役』"而非后端 exclude 语义（验收 5 允许两者，最简且不破坏既有筛选）。
- 冒烟中第一次 UI 点「开始生成」返回 400（`plans.py:151` 把 generate 链路 RuntimeError 包成 400，疑网关冷启动瞬时失败）；同参数立即重试 200 落库成功。**与本单改动无关**（400 发生在 LLM 调用段，非检索段），记此备查。
- 只读查库全程 `mode=ro` URI；本单未做任何直接写库（验收 7 的计划落库走应用 API，属任务单指定动作）。

**新发现（超本单范围，留给 PM/F9b 定夺）**：
- TemplateView 详情抽屉的状态下拉（`TemplateView.vue:196-200`）选项只有 reviewed/draft/archived——active 模板打开抽屉时下拉裸显英文 "active"；且后端 `TemplateUpsert/TemplateReview` 的 status pattern 不含 active，前端直接加选项会在改回 active 时 422。要修需连后端契约一起动，不在 F9a 验收清单内，**未修**。
- `plan_crud._templates_for_plan` 现在只召回 arc 骨架，segment 粒度模板（若未来启用）也会一并被过滤掉——当前 DB 无 active segment，无实际影响；F9b 做四分支时若需要 segment 再放开。
