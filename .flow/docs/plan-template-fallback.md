# 无口述时模板检索兜底 + 显式选模板（F9·全线关账 2026-10-03）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-10-03 | 上次验证: 2026-10-03（PM 验收：四分支服务层直调 + HTTP 探针落库双验——分支④ fallback 账 {queries:[沈砚青],random_ids:[]} 落 raw_ai、命中全 arc 骨架；refine-hint draft 直测出稿；单测 570 passed；执行方真机证据 `gui-test-screenshots/f9b/`。排查插曲见 docs/04 A15/A16）
>
> **DEV-F9c（尾巴三小修）2026-10-03 已实施待 PM 验收**：scale 契约补 character / 生成弹窗 force_free 复位 / 兜底主查询 卷概览>篇概览>篇名 + 双空概览行内提示；单测 580 passed，证据 `gui-test-screenshots/f9c/`，详见文末「DEV-F9c 实施记录」。

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
- 实施派单：DEV-F9a（scale 过滤 + TemplateView 显示修复，✅ 2026-10-03 实施完毕待 PM 验收）→ DEV-F9b（四分支主体，✅ 2026-10-03 实施完毕待 PM 验收）→ QA

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
- TemplateView 详情抽屉的状态下拉（`TemplateView.vue:196-200`）选项只有 reviewed/draft/archived——active 模板打开抽屉时下拉裸显英文 "active"；且后端 `TemplateUpsert/TemplateReview` 的 status pattern 不含 active，前端直接加选项会在改回 active 时 422。要修需连后端契约一起动，不在 F9a 验收清单内，**未修**。（→ DEV-F9b 已修，见下）
- `plan_crud._templates_for_plan` 现在只召回 arc 骨架，segment 粒度模板（若未来启用）也会一并被过滤掉——当前 DB 无 active segment，无实际影响；F9b 做四分支时若需要 segment 再放开。

### DEV-F9b（2026-10-03，SWE 执行；四分支主体 + refine-hint + 抽屉 status 修复）

**改动文件（8 个，无库表结构改动、无新增写表路径）**：

| 文件 | 改动 |
|---|---|
| `backend/app/services/plan_crud.py` | ①新增四分支主入口 `_pick_templates`（force_free > 作者指定 > 口述检索 > 无口述兜底 > 随机灵感），`_templates_for_plan` **签名与行为原样保留**（回归保护 + F9a 两条单测不动）；②配套 `_locked_template`（只认 scale=arc 且 status=active，否则 logger.warning 后按无选处理）、`_fallback_queries`（主查询=卷概要截 200 字，缺概要回退篇名；副查询=篇名 + 活跃角色前 8 名顿号拼接）、`_random_skeletons`（现役 arc 池 `random.sample` 抽 4~5 条）、`_picked_skeleton`（模型 template_ref 里出现骨架名即归因为它借鉴的那条，确定性零 LLM）；③`generate_plan` 加 `template_id` 参数并把兜底账写进 `raw_ai["fallback"] = {queries, random_ids, picked}`；④`_plan_prompt` 加 `mode` 关键字参数，**只改模板区说法**（author→标「作者指定」+ 段落标题改「作者指定的参考模板」；random→「随机候选骨架…请结合上下文判断最贴合的 1 个作为主参考」），注入格式与其余 prompt 一字未动；⑤新增 `refine_hint` + `_skeleton_brief` + `_refine_hint_prompt`（8B 口述增强/代写） |
| `backend/app/routers/plans.py` | `GenerateBody` 加 `template_id`（默认 None）；新端点 `POST …/plan/refine-hint`（`RefineHintBody.mode` pattern `^(enhance|draft)$`），RuntimeError → 400 与既有 generate 同款 |
| `backend/app/routers/plot_templates.py` | `TemplateUpsert.status` 与 `TemplateReview.status` 两处 pattern 改 `^(active\|draft\|reviewed\|archived)$`（验收 D9）；review 文档串补 active 语义（原文写的 `deprecated` 库里根本不存在） |
| `backend/tests/unit/test_plan_crud.py` | +16 例：`TestFallbackQueries` 3（卷概要截断/篇名回退不重复占路/空上下文）、`TestFourBranchPick` 7（作者指定跳过检索、三种不可用 id 回落检索、兜底 search 参数契约、兜底空→随机、检索抛异常→随机、池空→free、force_free 全跳过含指定模板）、`TestFourBranchGenerateAccounting` 3（作者指定 prompt 标注 + template_ids 落库、随机降级 raw_ai 记账与 picked 归因、有口述分支不写兜底账=回归钉）、`TestRefineHint` 3（只回文本不写库 + max_tokens/temperature 契约 + B6 无负面措辞、draft 无模板、空返回报错） |
| `frontend/src/views/PlanView.vue` | 生成弹窗加「参考模板」filterable 下拉（选项=active arc 骨架，副文本 logline，打开弹窗才拉取）；口述框下加 AI 按钮（见下「执行决策」）；`doGenerate` 传 `template_id`；**换篇清掉 template_id**（跨篇锁定残留） |
| `frontend/src/api/plan.js` | 加 `refineHint`；generate 注释补 template_id 语义 |
| `frontend/src/views/TemplateView.vue` | 详情抽屉状态下拉补「现役」选项（原本裸显英文 active） |
| `frontend/src/api/plotTemplate.js` | 注释里的 status 取值补 active（文档漂移修正，无代码改动） |

**验收逐条实测（2026-10-03，本机：后端 8000（venv 重启后新代码）/前端 5173/网关 9377 在线）**：

1. ✅ 四分支后端契约：见上单测；`generate_plan` 新参数经 router 透传，`template_id` 空=旧行为。
2. ✅ **分支①（选模板+口述）**：UI 选「立威震慑·越阶硬撼·反杀复仇·单挑决斗」→ 点「AI 增强口述」（简陋口述"主角出关后一路碾压仇家"）→ 8B 扩写并**填回口述框**（含所选骨架的节拍词）→ 生成。DB：`template_ids` 仅该 1 条（arc/active）、`origin=template`、`template_ref` = `G06 主动布下阵局`/`C05 立威震慑`。截图 `f9b/02`、`03`（chip 首位即所选模板，验收 C8）
3. ✅ **分支②（选模板无口述）**：选「跨书骨架组#3」+ 口述留空 → 按钮「AI 帮我写口述」出稿（截图 `04`）→ 清空口述再生成 → DB `template_ids=['跨书骨架组#3']`、`template_ref` 走该骨架节拍，**无 fallback 账**（作者分支不该记兜底）。
4. ✅ **分支③（不选+口述，回归）**：口述"主角被夺舍后苏醒…棋子…反手布局复仇" → 检索 top-4 全 arc/active（夺舍反噬、反杀复仇·夺舍反噬、秘境夺宝·群殴混战、秘境夺宝·拍卖竞价·吞噬异宝·反杀复仇），`raw_ai` 无 fallback 键 → **与现状一致**。
5. ✅ **分支④（不选+无口述，多路兜底）**：`raw_ai["fallback"] = {"queries": ["第一篇", "沈砚青"], "random_ids": []}`，兜底命中 4 条 arc 骨架（两次跑命中集稳定一致）。本书第一卷 summary 为空 → 主查询按设计回退篇名，角色路只有 1 个活跃角色，故 queries 只有两项。
6. ✅ **随机灵感降级**：真机未触发（兜底有命中），按定稿只在"兜底空/检索抛异常"时启用 → 由单测两例覆盖（空命中→随机、search 抛 RuntimeError→随机，且断言 4≤名单≤5、只抽 active arc、raw_ai 记 `random_ids`+`picked`）。
7. ✅ **force_free**：UI 开关开一次 → `origin=free`、`template_ids=[]`、无 fallback 键、`template_ref` = "无模板·自建开场"；单测另钉"force_free 连作者指定也跳过"。截图 `07`、`08`
8. ✅ **8B 口述增强/帮写（B4/B5）**：端点直测两模式（`outputs` 临时脚本，已删）→ draft 无模板也能凭卷概要+前情出稿；enhance 把所选骨架节拍织进口述；**计划表 before==after**（不落库、不触发生成，前端拿到只填框）。走 `sf_chat` → 网关 `qwen3-8b`，`enable_thinking:False` 在 `_sf_post` 里已钉死；`max_tokens=300, temperature=0.4`，scene=`sf_refine_hint` 进用量记账。
9. ✅ **D9 抽屉 status**：active 骨架开抽屉 → 下拉显示「现役」（不再裸显英文）→ 切实役→已审阅→现役，两次 PUT **均 200**（网络面板核对，改前会 422），事后模板状态已复核回 `active`。截图 `05`
10. ✅ 全量单测 **570 passed / exit 0**（基线 554 + 新增 16）；`npm run build` exit 0。
11. ✅ 提示词纪律（docs/04 B5/B6）：新增 prompt 全用中文引号「」、无「不要 XX」句式（单测里直接断言 `"不要" not in prompt`）；未叠加新约束，只替换模板区标题一行。

**执行决策/坑**：
- **按钮文案与 mode 都由「口述框有没有底稿」决定**，与任务单字面「选中模板后文案变 AI 增强口述」有偏差：空口述无从"增强"，写「AI 增强口述」而实际发 `mode=draft` 会骗作者。现规则=有底稿→「AI 增强口述」/enhance，无底稿→「AI 帮我写口述」/draft（选了模板也一样，8B 会拿模板骨架当依据）。任务单场景①（有选+有口述）文案仍是「AI 增强口述」。
- 分支判定顺序写成一条直线（force_free→author→hint→fallback→random），`_pick_templates` 返回 `(templates, ids, mode, fallback)`；不改 `_templates_for_plan` 签名，避免动到 F9a 刚钉的两条契约单测。
- `picked` 归因用「骨架名出现在任一行 template_ref」的确定性判断（template_ref 实测形如 `G06 主动布下阵局`、`退婚逆袭·开局`，骨架名基本都在其中），不额外花 LLM。
- **🔴 环境坑（重要）**：开工时 8000 端口上的后端进程是**系统 Python 3.12**（`C:\Users\w3013\...\Python312\python.exe dev.py`）拉起的，而该解释器**没装 sqlite-vec** → `vector_index.enabled` 为假 → 模板检索**静默退化成关键词匹配**（`mode=fallback_tags`，不报错、照样出命中，只是质量差且与线上语义不符）。按任务单命令用 `.venv/Scripts/python.exe dev.py` 重启后才是真向量。**排查线索**：检索"能命中但语义离谱"时先看进程解释器与 `sqlite_vec` 是否可导入，别只盯 prompt。
- 一次 UI 生成返回 400（`plans.py` 把 generate 链路 RuntimeError 包成 400，同参数立即重试 200）——与 F9a 记录同款 LLM 段瞬时失败，与四分支无关；400 时弹窗保持打开、作者可原地重试（拦截器已 ElMessage 报错，不阻塞手动生成）。
- 只读核对全程 `mode=ro` URI；本单唯一写库=计划生成链路与验收 9 指定的模板状态 PUT（后者已复位）。

**新发现（超本单范围，留给 PM 定夺）**：
- **兜底质量取决于卷概要**：验收 5 实测里，第一卷 `summary` 为空 → 兜底主查询退化成篇名「第一篇」，4 条命中里混进语义无关的「鄢陵定鼎」。建议：卷概要为空时生成弹窗给一句"补写卷概要可让自动匹配更准"的提示，或把 `article.summary` 也纳入兜底查询串（`_book_context` 目前不收集篇概要）。
- **TemplateUpsert.scale 只允许 `arc|segment`**：模板库页 F9a 已能筛出 `scale=character` 的角色模板并开抽屉，此时改状态保存会因 scale 校验 422（与本单修的 status 是同一处 PUT 的另一个契约缺口）。人物模板库页（CharTemplateView）另有一条链，未受影响。
- **PlanView 的「篇」选择是组件本地状态**：刷新/HMR 后丢失需重选；且侧栏树点中某篇不会同步到本页（`onMounted` 只读一次 `store.currentArticle`）。属既有 UX 缺口，本单未动。
- **force_free 开关在 genForm 里持久**：关掉弹窗再开仍是上次的值，作者可能"带着上次的自由规划"点了生成。建议每次开弹窗复位（或把已开启状态在按钮文案上强调）。

### DEV-F9c（2026-10-03，SWE 执行；F9 尾巴三小修，**已实施待 PM 验收**）

**改动文件（6 个，无库表结构改动、无新增写表路径）**：

| 文件 | 改动 |
|---|---|
| `backend/app/routers/plot_templates.py` | `TemplateUpsert.scale` 与 `TemplateSearch.scale` 两处 pattern 改 `^(arc\|segment\|character)$`。**两处都要改**：只补 Upsert 会漏第二个 422 —— F9a 的粒度下拉会把 `scale=character` 一起发给 `/plot-templates/search` |
| `backend/app/models/orm.py` | `PlotTemplateORM.scale` 注释补 character（原写 `arc \| segment`，与实际数据漂移） |
| `backend/app/services/plan_crud.py` | ①`_book_context` 新增键 `article_summary`（取 `articles.summary` 截 200 字，容错同原逻辑）；②`_fallback_queries` 主查询优先级改「卷概要 > 篇概览 > 篇名」（`volume_summary` 空时用 `article_summary`，两者皆空才退化篇名），副查询与角色串不变 |
| `backend/tests/unit/test_plan_crud.py` | +5 例：`TestFallbackQueries` 三档优先级 3 例（卷概览在/仅篇概览/双空）+ `TestBookContextArticleSummary` 2 例（收 article_summary 并截 200、缺概览时为空串） |
| `backend/tests/unit/test_plot_templates.py` | +`TestScaleWhitelist` 5 例（arc/segment/character 参数化通过、默认 arc、bogus scale 仍 422） |
| `frontend/src/views/PlanView.vue` | ①`watch(genVisible)` 打开时 `genForm.force_free = false`（docs/04 C6 复位模式，弹窗常驻挂载）；②新增 `noOverviewWarn` computed（当前篇在 `store.structure` 里且卷概览与篇概览**都空**才为真）+ 口述框上方行内小字提示 `.pv-gen-warn`（不弹窗打断） |

**验收逐条实测（2026-10-03，本机：后端 8000（`.venv` 起，见下"环境"）/前端 5173/网关 9377 在线）**：

1. ✅ **第 1 件 scale 契约**：
   - 单测 5 例（含反证 bogus → 422）。
   - HTTP 同值整条 PUT（前端 `changeStatus` 的请求形状 `{...detail, status}`）→ **200**（改前 422）；只读快照逐字段比对 → **name/scale/genre_tags/logline/structure/pitfalls/rhythm/source_stats/status 九字段零变化**，仅 `updated_at` 变（`10-02 00:59:27 → 10-03 11:14:48`）。
   - 真实 UI 路径复测：模板库页 粒度=角色模板（265 个）→ 开「蛮横抢宝型首领」抽屉（下拉显示「现役」）→ 切实役→已审阅→现役，**两次 PUT 均 200**（网络面板 reqid 3/5），事后 DB 复核 `status=active`、active 角色模板仍 265 条。截图 `f9c/00`、`01`、`02`
   - 反证 PUT（`scale=novel`）→ 422 `String should match pattern '^(arc|segment|character)$'`，白名单仍在守门。
2. ✅ **第 2 件 force_free 复位**：开弹窗 → 开关打开（DOM `is-checked` + `aria-checked=true`，截图 `04`）→ 点取消关闭 → 重开 → 开关为**关**（`switchChecked:false`、class 无 `is-checked`，截图 `05`）。同一次弹窗内开关照常生效（复位只在打开瞬间触发）。
3. ✅ **第 3 件兜底 query 质量**：
   - 真库只读 `_book_context`（原神启动/第一篇）→ `article_summary` 已收集，len=191（≤200 截断契约）。
   - 三档优先级实测：卷概览在→main=卷概览；卷概览人为置空→**main=篇概览**（修复前此处会退化成品名）；双空→main=篇名。
   - **真向量检索前后对比**（同一篇真实概览，`mode=vector`）：
     - 修复后（main=篇概览「陈峰在回春堂暗中藏匿五块灰石…」）命中 `谈判交涉·设伏偷袭·追击追杀·反杀复仇`、**`炼丹炼药`**、`围困被困·追击追杀·破阵解谜`、**`参悟传承`** —— 与"药铺藏灵石/采药"剧情同场；
     - 修复前同款退化路径（main=篇名「第一篇」）命中混进 `群殴混战·越阶硬撼·单挑决斗·…`、`明道宫定策`（《绍宋》历史系骨架）—— 语义漂移。
   - **王从天降/第一篇真机生成（不写口述）**：弹窗出现新提示「本篇无卷概览/篇概览，自动匹配可能不准 —— 建议写句口述，或到「概览」页补写」（截图 `03`）→ 点开始生成 → `origin=template`、8 行、UI 徽章「模板规划」（截图 `06`）；fallback 账 `{"queries":["第一篇","沈砚青"],"random_ids":[]}`。
4. ✅ 全量单测 **580 passed / exit 0**（基线 570 + 新增 10）；`npm run build` exit 0。

**执行决策/坑**：
- **本单第 3 件对「王从天降」不产生 queries 变化**（如实记录）：该书**卷概览、篇概览、小说总概览三项全空**，主查询按设计仍退化成品名，F9b 观察到的「鄢陵定鼎」漂移**仍在**。真正兜住它的是新增的行内提示（引导作者补概览或写口述）；query 质量提升在有概览的书上成立（上面原神启动的前后对比）。
- 提示文案用项目自己的名词「卷概览/篇概览」并指路「概览」页（`OverviewView` 的编辑入口），比任务单字面的"卷概要/简介"更好找。
- 只读核对全程 `mode=ro` URI；本单写库仅两处：角色模板整条 PUT（同值保存 + 状态复原）、王从天降第一篇 **draft** 计划重新生成（任务单指定动作；该篇原状态是 draft，不是 confirmed，未覆盖作者已拍板的计划）。
- **🔴 A15 的识别方法有误报，本单实测坐实**（已回写 docs/04 A15）：`.venv\Scripts\python.exe` 是**转发器**，会再拉起基础解释器 `Python312\python.exe` 当真正服务进程 —— 于是 CIM 里监听 8000 的 PID 显示成"系统 Python"，但环境其实是 venv。本单靠 `Get-Process | Modules` 看到 `vec0.DLL <= E:\AI小说创作\.venv\Lib\site-packages\sqlite_vec\vec0.DLL` 才判实。**别只看 CommandLine 就杀进程**。
- 附带纠正：`search` 返回 `mode=vector` **不能**证明 sqlite_vec 已加载 —— 扩展缺失时 `vector_store` 回退 `BruteVectorStore`（纯 Python 点积，结果正确只是慢），照样 `mode=vector`。判向量真伪要看 `vec0.DLL` 是否加载，`mode=fallback_tags` 只说明连 embedding 那段都没走到。

**新发现（超本单范围，留给 PM 定夺）**：
- **双空概览的书仍拿不到好兜底**：分支④对「王从天降」永远只有 篇名 + 角色名 两路。可选方向（本单未动）：把 `projects.summary`（小说总概览）或前情摘要 `prev_arc` 也纳入兜底料源；或在概览双空时把「AI 帮我写口述」按钮做成显眼引导。
- **`plan_crud._refine_hint_prompt` / `_plan_prompt` 里仍只写「卷概要」**（`ctx.get('volume_summary')`），篇概览没进规划/拟口述的 prompt 料。本单只按任务单改检索 query 串，prompt 侧要不要同步补料请 PM 定。

