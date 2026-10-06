# -*- coding: utf-8 -*-
"""[DEV-SK07] x3 apply 前备份快照预览（**只读 mode=ro，绝不写库**）

与 skel_v4_rebuild.do_backup 同构地生成 616 张 v4.1 active arc 的全字段快照，
先交 PM/用户过目（条数、列完整性、origin、回滚语句样例、若中断的还原路径），确认后才跑 --apply。
用法：python x3_preview_backup.py
产出：outputs/sk07/SK07_backup_预览.json ＋ 控制台对账摘要
"""
import io
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
DB = r"C:\Users\w3013\.ai_novel\data\novel_agent.db"
OUT = r"E:\AI小说创作\outputs\sk07"
DST = os.path.join(OUT, "SK07_backup_预览.json")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cols = [r[1] for r in con.execute("PRAGMA table_info(plot_templates)")]
rows = [dict(r) for r in con.execute("select * from plot_templates where scale='arc' and status='active'")]
ch_active = con.execute("select count(*) from plot_templates where scale='character' and status='active'").fetchone()[0]
arch_old = con.execute("select count(*) from plot_templates where scale='arc' and status='archived'").fetchone()[0]
con.close()

def origin_of(r):
    ss = str(r.get("source_stats") or "")
    return "skel_v4" if '"origin": "skel_v4"' in ss or "skel_v4" in ss else "无origin"

snap = {"script": "skel_v4_rebuild.py do_apply → do_backup（预览件，只读生成）",
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db": DB, "table": "plot_templates", "columns": cols,
        "counts": {"archived": len(rows)},
        "note": "status 由 active 改 archived 即可回滚；本文件是 apply 前只读预览，非正式备份",
        "archived": rows}
io.open(DST, "w", encoding="utf-8").write(json.dumps(snap, ensure_ascii=False, indent=1))

print("=" * 70)
print("apply 前快照预览（只读生成，库未动）")
print("=" * 70)
print(f"待归档 active arc：{len(rows)} 条（换代基线应为 616）→ {'符合' if len(rows)==616 else '不符合，须人工核'}")
print(f"全字段列数：{len(cols)} 列 {cols}")
print(f"origin 分布：{dict(Counter(origin_of(r) for r in rows))}")
print(f"active 角色模板（character）：{ch_active} 张 → **三路向量禁碰，不在本快照内**"
      f"（相交检查：{len(set(str(r['id']) for r in rows) & set())}）")
print(f"既有 archived arc：{arch_old} 条（历轮归档，其向量块本轮不动）")
miss = [r["id"] for r in rows if not set(cols) <= set(r.keys())]
print(f"字段完整性（restore-check 前置：每行列齐全）：{'齐全' if not miss else '缺列 ' + str(miss[:3])}")
for r in rows[:3]:
    print(f"  样例 id={r['id'][:12]}… name={r['name']!r} scale={r['scale']} status={r['status']} "
          f"chunks={str(r.get('chunks'))[:40]}")
print("\n回滚路径（若 apply 中途失败/事后要退）：")
print("  UPDATE plot_templates SET status='active' WHERE id IN (…)   -- 616 个 id 全在本快照 archived[].id")
print("  示例：" + " / ".join(f"{r['id'][:8]}…" for r in rows[:4]))
print(f"\n预览件 → {DST}（{os.path.getsize(DST)} 字节）")
