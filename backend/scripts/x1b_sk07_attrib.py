# -*- coding: utf-8 -*-
"""[DEV-SK07] x1b dry-run 归因：978 张模板的结构解释（只读）"""
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")
OUT = r"E:\AI小说创作\outputs"
rows = json.load(io.open(os.path.join(OUT, "sk06b", "_work", "判类_全量1033.json"),
                         encoding="utf-8"))["rows"]

byk = defaultdict(list)
for r in rows:
    byk[r["母题"]].append(r)
k1 = sum(1 for v in byk.values() if len(v) == 1)
k_ge2 = sum(1 for v in byk.values() if len(v) >= 2)
m_ge2 = sum(len(v) for v in byk.values() if len(v) >= 2)
print(f"判类键 {len(byk)} 个｜单成员键 {k1}｜成员≥2 键 {k_ge2}（覆盖弧 {m_ge2}）")
print(f"  理论上界：键内零合并 → 1033 张；每键全并成 1 张 → {len(byk)} 张")

t = io.open(os.path.join(OUT, "sk07", "SK07_dryrun.txt"), encoding="utf-8").read().splitlines()
pat = re.compile(r"^  (.+?)--(.*?) ?(?:\[孤例类\])?: 成员 (\d+) → 簇 (\d+) → 模板 (\d+)（单弧 (\d+)）")
keys = []
for ln in t:
    m = pat.match(ln)
    if m:
        keys.append({"key": m.group(1) + "--" + m.group(2), "members": int(m.group(3)),
                     "clusters": int(m.group(4)), "tpl": int(m.group(5))})
tot_tpl = sum(k["tpl"] for k in keys)
zero = [k for k in keys if k["members"] >= 2 and k["clusters"] == k["members"]]
part = [k for k in keys if k["members"] >= 2 and k["clusters"] < k["members"]]
print(f"\ndry-run 逐键 {len(keys)} 行｜成员 {sum(k['members'] for k in keys)}｜模板 {tot_tpl}")
print(f"  成员≥2 的键 {len(zero) + len(part)} 个：一键未合 {len(zero)} 个｜发生合并 {len(part)} 个")
print(f"  合并吸收弧数 = 1033 − {tot_tpl} = {1033 - tot_tpl}")
print("  合并最多的键：", [(k["key"], f"{k['members']}→{k['tpl']}")
      for k in sorted(part, key=lambda x: -(x["members"] - x["tpl"]))[:10]])
big = [k for k in zero if k["members"] >= 6]
print("  成员≥6 却零合并的键：", [(k["key"], k["members"]) for k in sorted(big, key=lambda x: -x["members"])][:10])

beats = [int(r["拍数"]) for r in rows]
print(f"\n拍数：≤3 拍 {sum(1 for b in beats if b <= 3)} 条（短档 0.50）｜>3 拍 {sum(1 for b in beats if b > 3)} 条（长档 0.75）")
for b in ("块1", "块2", "块3"):
    v = [r for r in rows if r["块"] == b]
    print(f"  {b}：{len(v)} 条，≤3 拍 {sum(1 for r in v if int(r['拍数']) <= 3)}"
          f"｜不同 (书,弧名) 数 {len({(r['书'], r['弧名']) for r in v})}")
print("\n对照 SK03：616 弧 → 616 张（吸收 0）；本轮 1033 弧 → 978 张")
