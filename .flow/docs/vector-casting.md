# 角色向量选角（P2）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-30（pytest 全量单测复验 542/542，含 test_casting.py 27 例 + test_plot_cast.py；09-13 真机标定 MIN_SCORE=0.58）

## 为什么做

计划里的"引路人师长""对手"这类功能槽位，要自动找库里合适的角色来演，不能让死人复活、也不能靠名字硬猜。

## 解决什么

计划每个槽位用向量检索从角色库选角，带状态门与阈值控制，作者可手改。

## 怎么实现

- cast 槽位向量库（source_type="plot_cast"，与模板级分开索引/清理）
- 显式余弦选角（不用 store 的 1/(1+L2) 近似）
- 状态门四态：alive/dormant/departed/dead，死人默认不进池
- 阈值 0.58 来自真机标定（正例最低 0.585 / 负例最高 0.596）
- plan_castings 表 UNIQUE(plan_id,slot)，manual 标记不被重算覆盖
- 平局两级判据：共现 → 活跃度

## 怎么扩展

- 🔴 F8：骨架槽位缺 ranks/traits → 「功能位参考」返回空（待拍板重跑）
- 换 embedding 模型后需重新标定阈值

## 代码位置

- backend/app/services/casting_crud.py、routers/casting.py（4 端点）

## 关联

- 上游 P1；下游 P4（选角面板）；F8 是数据质量前提
