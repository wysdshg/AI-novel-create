# -*- coding: utf-8 -*-
"""玄幻模板：节号重排（货币=六、物价=七、天赋=八）+ 修复 8.2 断表 + 半角~转全角 + 入库。"""
import json
import sqlite3
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
MD = "E:/AI小说创作/outputs/setting-templates/玄幻小说设定.md"

t = open(MD, encoding="utf-8").read()

# 1) 节号重排：货币 七→六，物价 八→七（子节 7.x 恰好吻合），天赋保持八
t = t.replace("## 七、货币体系", "## 六、货币体系")
t = t.replace("## 八、物价体系（药品 / 车子 / 房子三类比）", "## 七、物价体系（药品 / 车子 / 房子三类比）")

# 2) 修复 8.2 断表：把空行隔开的孤立三行并回主表
broken = """| 玄冰之体 | 对冰元素灵力亲和：吸收冰灵力速率数倍于常人，寒灵力纯度更高 | 冰系道路直指大乘；修非冰系功法反而慢于常人 | 体内寒气易积，需火属性丹药调和；性情偏冷 |


| 妖兽体质（炼化妖血） | 肉身强度远超同阶，可兼修妖兽炼体，妖丹妖血可直接炼化吸收 | 上限受血脉浓度限制：浓度越高上限越高 | 兽性侵蚀，浓度过高会兽化失控 |  
| 大帝血脉 | 上古大帝后裔，血脉分段觉醒，每次觉醒实力跃迁 | 血脉顶点直指大帝境界，无视灵根一般上限 | 觉醒需机缘与资源；初期不一定比同阶强 |  
| 神魔之体 | 神魔遗种，肉身即「容器」，百种灵力皆可直接容纳炼化（天然免疫多灵根内耗） | 直指神魔 | 易被神魔意志残留侵染，夺舍与心魔风险高于常人 |"""
fixed = """| 玄冰之体 | 对冰元素灵力亲和：吸收冰灵力速率数倍于常人，寒灵力纯度更高 | 冰系道路直指大乘；修非冰系功法反而慢于常人 | 体内寒气易积，需火属性丹药调和；性情偏冷 |
| 妖兽体质（炼化妖血） | 肉身强度远超同阶，可兼修妖兽炼体，妖丹妖血可直接炼化吸收 | 上限受血脉浓度限制：浓度越高上限越高 | 兽性侵蚀，浓度过高会兽化失控 |
| 大帝血脉 | 上古大帝后裔，血脉分段觉醒，每次觉醒实力跃迁 | 血脉顶点直指大帝境界，无视灵根一般上限 | 觉醒需机缘与资源；初期不一定比同阶强 |
| 神魔之体 | 神魔遗种，肉身即「容器」，百种灵力皆可直接容纳炼化（天然免疫多灵根内耗） | 直指神魔 | 易被神魔意志残留侵染，夺舍与心魔风险高于常人 |"""
assert broken in t, "8.2 断表原文未匹配"
t = t.replace(broken, fixed)

# 3) 半角 ~ → 全角 ～（防 Markdown 删除线）
n_tilde = t.count("~")
t = t.replace("~", "～")

open(MD, "w", encoding="utf-8", newline="").write(t)

# 4) 入库
engine = create_engine(f"sqlite:///{DB}", poolclass=StaticPool,
                       connect_args={"check_same_thread": False})
s = sessionmaker(bind=engine)()
now = datetime.utcnow().isoformat(sep=" ")
r = s.execute(text("UPDATE setting_templates SET content=:c, updated_at=:u WHERE name=:n"),
              {"c": t, "u": now, "n": "玄幻小说设定"})
s.commit()
ck = s.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
s.close()
print(json.dumps({"db_rows": r.rowcount, "tilde_replaced": n_tilde,
                  "chars": len(t), "checkpoint": list(ck)}, ensure_ascii=False))

# 5) 核账：节号唯一性 + 无半角 ~
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
content = c.execute("SELECT content FROM setting_templates WHERE name='玄幻小说设定'").fetchone()[0]
c.close()
import re
heads = [l for l in content.splitlines() if l.startswith("## ")]
print("[verify] 二级标题：")
for h in heads:
    print("  ", h[:40])
print("[verify] 残留半角~ =", content.count("~"))
