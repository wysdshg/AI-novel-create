# -*- coding: utf-8 -*-
"""[DEV-SK07] x2 只读盘点生产库现状：SK07 apply 换代前置条件核对（不写库）"""
import io
import json
import sqlite3
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
DB = r"C:\Users\w3013\.ai_novel\data\novel_agent.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
print("表清单:", [r[0] for r in con.execute(
    "select name from sqlite_master where type='table' order by name")])

rows = [dict(r) for r in con.execute(
    "select id, name, scale, status, source_stats from plot_templates")]
def origin_of(ss):
    try:
        d = json.loads(ss) if isinstance(ss, str) else (ss or {})
    except Exception:
        return "?"
    if isinstance(d, str):
        return "skel_v4" if "skel_v4" in d else ("v3" if "atomic_merge" in d else "其他")
    o = str(d.get("origin") or "")
    return o or ("skel_v4" if "skel_v4" in (ss or "") else "无origin")

c = Counter((r["scale"], r["status"], origin_of(r["source_stats"])) for r in rows)
print("\nplot_templates 现状（scale, status, origin → 条数）:")
for k, v in sorted(c.items(), key=lambda x: -x[1]):
    print(f"  {k} → {v}")
arc_act = [r for r in rows if r["scale"] == "arc" and r["status"] == "active"]
origin_c = Counter(origin_of(r["source_stats"]) for r in arc_act)
ch_act = [r for r in rows if r["scale"] == "character" and r["status"] == "active"]
print(f"\nactive arc {len(arc_act)} 张｜origin 分布 {dict(origin_c)}"
      f"｜active character {len(ch_act)} 张"
      "（E24：换代集合必须与这 265 张零相交，别把它们当「归档残留」）")
print("\n向量覆盖按 **维度** 拆（E24 正解：join 回 plot_templates 取 scale/status 再 group，"
      "不要只 count(distinct source_id) 就解释）:")
for r in con.execute("""select p.scale, p.status, v.source_type,
                               count(distinct v.source_id) srcs, count(*) chunks
                        from vector_chunks v join plot_templates p on p.id = v.source_id
                        where v.source_type in ('plot_template','plot_cast','char_archetype')
                        group by 1,2,3 order by 1,2,3"""):
    print("  ", tuple(r))

for t in ("vector_chunks", "vec_index"):
    try:
        n = con.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"{t}: {n} 行")
    except Exception as e:
        print(f"{t}: 读不到（{type(e).__name__} {e}）")
try:
    by = {r["source_type"]: r["n"] for r in con.execute(
        "select source_type, count(*) as n from vector_chunks group by source_type")}
    print("vector_chunks 按 source_type:", by)
    ids = {r["id"] for r in rows}
    for r in con.execute("select source_type, count(distinct source_id) as d,"
                         " count(distinct case when source_id not in (select id from plot_templates)"
                         " then source_id end) as orph from vector_chunks group by source_type"):
        print(f'  {r["source_type"]}: 去重源 {r["d"]}｜不在 plot_templates 的源 {r["orph"]}')
    act_arc = {r["id"] for r in rows if r["scale"] == "arc" and r["status"] == "active"}
    act_ch = {r["id"] for r in rows if r["scale"] == "character" and r["status"] == "active"}
    arch_arc = {r["id"] for r in rows if r["scale"] == "arc" and r["status"] != "active"}
    n = con.execute("select count(*) from vector_chunks where source_type='plot_template'").fetchone()[0]
    got = {r[0] for r in con.execute("select distinct source_id from vector_chunks"
                                      " where source_type='plot_template'")}
    print(f"  plot_template 块共 {n}｜有块的源 {len(got)}"
          f"｜active arc {len(act_arc)} 张缺块 {len(act_arc - got)}"
          f"｜active character {len(act_ch)} 张缺块 {len(act_ch - got)}"
          f"｜archived arc 残留块源 {len(got & arch_arc)}（换代后应为 0）")
except Exception as e:
    print("vector_chunks 明细读取失败:", e)
con.close()
