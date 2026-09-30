# -*- coding: utf-8 -*-
"""玄幻模板重构：删八/九/十三节（转存为写作 SKILL）+ 新增六、天赋体系 + 节号重排 + 两表同步入库。"""
import json
import sqlite3
import uuid
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
MD = "E:/AI小说创作/outputs/setting-templates/玄幻小说设定.md"

raw = open(MD, encoding="utf-8").read()
lines = raw.splitlines(keepends=True)

# 1) 截取到妖兽表结束（## 八 之前），抽出八九十原文
idx_eight = next(i for i, l in enumerate(lines) if l.startswith("## 八、"))
body_keep = "".join(lines[:idx_eight]).rstrip() + "\n"
sections_removed = "".join(lines[idx_eight:])

# 2) 节号重排：六→七，七→八
body_keep = body_keep.replace("## 六、货币体系", "## 七、货币体系")
body_keep = body_keep.replace("## 七、物价体系", "## 八、物价体系")

# 3) 天赋体系（用户机制原文扩写；区间号一律全角 ～）
talent = """## 六、天赋体系（灵根 × 体质 × 功法）

### 6.0 总纲：灵气元素模型

天地灵气并非单一，而是混杂多种元素灵力：金木水火土五行之外，还有冰、雷、风、暗、空间等稀有元素。灵根就是修士对某种元素灵力的「吸引 + 容纳」器官——有什么灵根，才能吸收什么元素的灵力。三条硬规则：

1. 同一瞬间只能运行一种元素灵力（中途换元素要重新运功，有时间成本）。
2. 一瞬间能吸收的总灵力有生理上限（由经脉宽度与肉身强度决定）。
3. 吸进来但与自身灵根不符的灵力无法直接炼化，只能慢慢排出，或靠功法转换——这就是多灵根修得慢的根源。

### 6.1 灵根分类（修仙速度：单异灵根 > 单灵根 > 双 > 三 > 四 > 五杂灵根）

| 灵根 | 稀有度 | 速度 | 一般上限 | 说明 |
| --- | --- | --- | --- | --- |
| 单灵根（天灵根） | 万中无一 | 极快 | 合体～大乘，理论可渡劫飞升 | 只吸收一条元素，零浪费零内耗 |
| 双灵根 | 百里挑一 | 较快 | 炼虚～合体 | 主修一条副修一条，浪费约三成 |
| 三灵根 | 较常见 | 中等 | 化神 | 浪费过半 |
| 四灵根 | 常见 | 慢 | 元婴 | 四线互抢 |
| 五灵根（杂灵根） | 最普遍 | 极慢 | 结丹～元婴（瓶颈如天堑） | 五行全吸、互相争抢，炼化不过来 |
| 异灵根（单） | 稀有中的稀有 | 极快 | 合体～大乘，甚至更高 | 只吸一种稀有元素，兼具「纯」与「稀缺」 |
| 异灵根（多） | 稀有 | 快 | 视组合而定，低于单异 | 稀缺但仍受「多根互抢、运行唯一」限制 |

【为什么单灵根快】世界灵气混杂，单灵根像一根只过一种水的管子——吸进来的全是能炼化的，一分不浪费，纯度还高。
【为什么多灵根慢】五种灵根像五张网一起撒——把大量用不到的元素灵力也吸进来；同一瞬间又只能运行一种，剩下的只能慢慢排掉。吸收同样多的灵气，能用的不到一半。
【为什么异灵根也快】两条原因：① 变异元素灵力本身精纯霸道，炼化收益高；② 世上绝大多数人都是五行灵根，彼此争抢同一类修炼资源，而异灵根人少——走的路没人抢，资源竞争不激烈，所以快。
【异灵根（单）≠ 异灵根（多）】单异灵根兼具「纯」与「稀缺」双重优势，是天花板级天赋；多异灵根虽也稀缺，速度仍要打折。写作时必须区分，不能把多变异元素的角色写成单异灵根的速度。

### 6.2 体质（决定吸收效率与上限，可突破灵根一般上限）

体质是灵根之外的第二轴：同灵根不同体质，吸收速率与最终上限可以差数倍。

| 体质 | 机制 | 上限 | 代价 / 限制 |
| --- | --- | --- | --- |
| 普通体质 | 无加成，全看灵根与功法 | ＝灵根一般上限 | — |
| 玄冰之体 | 对冰元素灵力亲和：吸收冰灵力速率数倍于常人，寒灵力纯度更高 | 冰系道路直指大乘；修非冰系功法反而慢于常人 | 体内寒气易积，需火属性丹药调和；性情偏冷 |
| 妖兽体质（炼化妖血） | 肉身强度远超同阶，可兼修妖兽炼体，妖丹妖血可直接炼化吸收 | 上限受血脉浓度限制：浓度越高上限越高 | 兽性侵蚀，浓度过高会兽化失控 |
| 大帝血脉 | 上古大帝后裔，血脉分段觉醒，每次觉醒实力跃迁 | 血脉顶点直指大帝境界，无视灵根一般上限 | 觉醒需机缘与资源；初期不一定比同阶强 |
| 神魔之体 | 神魔遗种，肉身即「容器」，百种灵力皆可直接容纳炼化（天然免疫多灵根内耗） | 直指神魔 | 易被神魔意志残留侵染，夺舍与心魔风险高于常人 |

### 6.3 功法（决定浪费多少、吸多快）

功法不产生灵力，只决定「利用率」，分三类（可复合）：

- 筛选型：运功时只吸收所需元素，其余当杂质滤掉——好功法的基本功，也是同灵根修速差很多的原因。
- 转换型：把吸错的其他元素灵力转换成所需元素——杂灵根的救命稻草（有转换损耗，通常五成左右）。
- 提速型：提升单位时间吸收总量——但吸收速率与肉身强度挂钩，超出经脉承受力会胀裂经脉。

【境界限制的原理】高阶功法要求的吸收速率远超低阶肉身的承受上限——不是门派保守，是生理上根本用不了。这就是「功法需要境界限制」的世界观依据。

### 6.4 AI 综合评定角色天赋的方法

评定一个角色的修炼天赋，按三轴叠加，不能只看灵根：

1. 灵根＝能吸什么（元素面 + 速度基线）；
2. 体质＝吸多快、上限多高（效率与天花板，可突破灵根一般上限）；
3. 功法＝浪费多少（利用率）。

叠加示例：

- 单灵根 + 普通体质 + 普通功法 ＝ 一流天才：快，但天花板受功法拖累。
- 异灵根（单）+ 玄冰之体 + 冰系功法 ＝ 乘算级天赋（冰元素速率 × 体质加成 × 零浪费），同阶无敌手。
- 五灵根 + 玄冰之体 + 冰系转换功法 ＝ 经典「废柴逆袭」配置：灵根拖底，但体质与功法把利用率救回来。
- 大帝血脉 / 神魔之体可直接无视灵根一般上限——「五灵根但神魔之体」完全成立，这正是逆袭流的体系依据。
"""

new_content = body_keep + talent
open(MD, "w", encoding="utf-8", newline="").write(new_content)

# 4) 更新 setting_templates
engine = create_engine(f"sqlite:///{DB}", poolclass=StaticPool,
                       connect_args={"check_same_thread": False})
s = sessionmaker(bind=engine)()
now = datetime.utcnow().isoformat(sep=" ")
r = s.execute(text("UPDATE setting_templates SET content=:c, updated_at=:u WHERE name=:n"),
              {"c": new_content, "u": now, "n": "玄幻小说设定"})

# 5) 三节纪律原文 → 写作 SKILL（通用类，chapter 触发，可叠加）
skill_name = "修真世界观写作纪律（灵气感知/物品手感/品级铁律）"
prompt_body = (
    "以下为修真玄幻题材的正文写作硬纪律（来自全局设定库），写作时逐条遵守：\n\n"
    + sections_removed.strip() + "\n"
)
existing = s.execute(text("SELECT id FROM custom_skills WHERE name=:n"), {"n": skill_name}).fetchone()
if existing:
    s.execute(text("UPDATE custom_skills SET prompt_body=:p, updated_at=:u WHERE id=:i"),
              {"p": prompt_body, "u": now, "i": existing[0]})
    skill_action = "updated"
else:
    s.execute(text("""INSERT INTO custom_skills
        (id, name, description, prompt_body, trigger, enabled, tags, category, priority, builtin, created_at, updated_at)
        VALUES (:id, :name, :desc, :p, 'chapter', 1, :tags, '通用', 260, 0, :ca, :ua)"""),
        {"id": uuid.uuid4().hex, "name": skill_name,
         "desc": "玄幻/修真正文防错纪律：灵气感知边界（知识泄漏禁忌）、灵石丹药符箓手感（禁发热发光）、品级使用铁律。原为设定模板第八/九/十节，2026-09-26 迁出。",
         "p": prompt_body,
         "tags": json.dumps(["修真", "纪律", "防错"], ensure_ascii=False),
         "ca": now, "ua": now})
    skill_action = "inserted"

s.commit()
ck = s.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
s.close()
print(json.dumps({"tpl_updated": r.rowcount, "skill": skill_action,
                  "skill_chars": len(prompt_body), "new_content_chars": len(new_content),
                  "checkpoint": list(ck)}, ensure_ascii=False))

# 6) 跨进程核账
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
tpl = c.execute("SELECT length(content), instr(content, '天赋体系'), instr(content, '灵气感知规则') FROM setting_templates WHERE name='玄幻小说设定'").fetchone()
sk = c.execute("SELECT name, length(prompt_body) FROM custom_skills WHERE name=:n", {"n": skill_name}).fetchone()
c.close()
print(f"[verify 跨进程] 模板 chars={tpl[0]} 含天赋体系={tpl[1] > 0} 已无感知规则节={tpl[2] == 0}")
print(f"[verify 跨进程] SKILL {sk[0]} prompt_chars={sk[1]}")
