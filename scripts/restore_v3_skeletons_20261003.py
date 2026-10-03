# -*- coding: utf-8 -*-
"""T1 骨架池精确恢复（2026-10-03，PM 执行，用户已拍板）。

背景：10-02 f8_p3_swap 用 retire --status active 一刀切，把 100 条 v3 情节骨架
（scale=arc，genre_tags 含「原子骨架」+「v3生成」）误归档，篇规划模板检索池被清空。
本脚本：备份 → id 双源核对（10-02 备份文件 vs DB 标签判据，必须完全一致才动库）
→ 精确 UPDATE 100 条 archived → active → 逐行重建三路向量 → 跨进程复核。
164 条旧聚类模板（09-19 正常归档）不动。
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJ = Path(r"E:\AI小说创作")
BACKUP_DIR = PROJ / "outputs" / "_backup"
DB_PATH = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
SWAP_BACKUP = BACKUP_DIR / "plot_templates_backup_100_20261002_085312.json"


def main() -> int:
    now = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ---- 1. 备份当前 264 条 archived arc（全字段）----
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT * FROM plot_templates WHERE scale='arc' AND status='archived'"
    ).fetchall()
    assert len(rows) == 264, f"archived arc 期望 264 条，实为 {len(rows)}，中止"
    bak = BACKUP_DIR / f"plot_templates_archived264_{now}.json"
    bak.write_text(
        json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"[1/5] 备份 264 条 archived arc → {bak.name} ({bak.stat().st_size//1024} KB)")

    # ---- 2. id 双源核对 ----
    ids_from_swapfile = {r["id"] for r in json.loads(SWAP_BACKUP.read_text(encoding="utf-8"))}
    ids_from_tags = {
        r["id"] for r in rows
        if {"原子骨架", "v3生成"} <= set(json.loads(r["genre_tags"] or "[]"))
    }
    print(f"[2/5] 换血备份文件 id 数={len(ids_from_swapfile)}，DB 标签判据 id 数={len(ids_from_tags)}")
    only_a = ids_from_swapfile - ids_from_tags
    only_b = ids_from_tags - ids_from_swapfile
    if only_a or only_b:
        print(f"  两源不一致！仅备份文件有:{len(only_a)} 仅标签判据有:{len(only_b)} —— 中止，人工核对")
        return 1
    print("  两源完全一致 ✓")

    # ---- 3. 精确 UPDATE archived → active ----
    conn.close()
    w = sqlite3.connect(str(DB_PATH))
    wc = w.cursor()
    id_list = sorted(ids_from_tags)
    ph = ",".join("?" * len(id_list))
    n = wc.execute(
        f"UPDATE plot_templates SET status='active', updated_at=CURRENT_TIMESTAMP "
        f"WHERE id IN ({ph}) AND status='archived'", id_list
    ).rowcount
    w.commit()
    w.execute("PRAGMA wal_checkpoint(PASSIVE)")
    print(f"[3/5] 已 UPDATE {n} 条 → active（期望 100）")
    if n != 100:
        print("  数量不符，中止后续步骤")
        return 1
    w.close()

    # ---- 4. 重建三路向量（复用后端服务层，幂等 index_template）----
    sys.path.insert(0, str(PROJ / "backend"))
    from app.core.database import init_db, SessionLocal
    init_db()
    from app.services import plot_template_crud as tpl
    db = SessionLocal()
    try:
        targets = (db.query(tpl.PlotTemplateORM)
                   .filter(tpl.PlotTemplateORM.id.in_(id_list)).all())
        total = 0
        for i, t in enumerate(targets, 1):
            n_blk = tpl.index_template(db, t)
            total += n_blk
            if i % 20 == 0 or i == len(targets):
                db.commit()
                print(f"  向量重建 {i}/{len(targets)}，累计 {total} 块")
        print(f"[4/5] 向量重建完成：{len(targets)} 条模板，{total} 块")
    finally:
        db.close()

    # ---- 5. 跨进程只读复核 ----
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    print("[5/5] 复核：")
    for r in cur.execute("SELECT status, scale, COUNT(*) FROM plot_templates GROUP BY status, scale"):
        print("  plot_templates:", tuple(r))
    for r in cur.execute("SELECT source_type, COUNT(*) FROM vector_chunks GROUP BY source_type"):
        print("  vector_chunks:", tuple(r))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
