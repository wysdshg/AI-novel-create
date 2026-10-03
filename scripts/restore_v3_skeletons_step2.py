# -*- coding: utf-8 -*-
"""T1 续跑：向量重建 + 复核（UPDATE 已由主脚本完成并 commit，本脚本不碰 status）。"""
import sys
from pathlib import Path

PROJ = Path(r"E:\AI小说创作")
sys.path.insert(0, str(PROJ / "backend"))

import app.core.database as dbmod  # noqa: E402
dbmod.get_engine()  # 初始化 SessionLocal（必须经模块属性访问，from-import 会绑死 None）

import json
import sqlite3  # noqa: E402
from app.services import plot_template_crud as tpl  # noqa: E402

DB_PATH = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
SWAP_BACKUP = PROJ / "outputs" / "_backup" / "plot_templates_backup_100_20261002_085312.json"

ids = sorted({r["id"] for r in json.loads(SWAP_BACKUP.read_text(encoding="utf-8"))})

db = dbmod.SessionLocal()
try:
    targets = db.query(tpl.PlotTemplateORM).filter(tpl.PlotTemplateORM.id.in_(ids)).all()
    print(f"待重建向量：{len(targets)} 条（期望 100）")
    bad = [t.name for t in targets if t.status != "active"]
    if bad:
        print(f"  ⚠️ 有非 active 条目，中止: {bad[:5]}")
        sys.exit(1)
    total = 0
    for i, t in enumerate(targets, 1):
        total += tpl.index_template(db, t)
        if i % 20 == 0 or i == len(targets):
            db.commit()
            print(f"  向量重建 {i}/{len(targets)}，累计 {total} 块")
    print(f"[向量重建完成] {len(targets)} 条模板，{total} 块")
finally:
    db.close()

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
cur = conn.cursor()
print("[跨进程复核]")
for r in cur.execute("SELECT status, scale, COUNT(*) FROM plot_templates GROUP BY status, scale"):
    print("  plot_templates:", tuple(r))
for r in cur.execute("SELECT source_type, COUNT(*) FROM vector_chunks GROUP BY source_type"):
    print("  vector_chunks:", tuple(r))
conn.close()
