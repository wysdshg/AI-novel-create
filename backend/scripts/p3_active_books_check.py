# -*- coding: utf-8 -*-
"""[DEV-P3] 现役书零改动自证：P3 全程前后对 3 本现役书 + 全库关键表做指纹/差集。

P3 允许写的表：projects/volumes/articles/article_plans/chapters/…（测试书自己的行）
P3 **不允许**写：plot_templates / event_skeletons（实测单不是开发单）
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "p3_inject"
BOOK = "P3注入实测"
ACTIVE = ("原神启动", "原神是怎样练成的", "王从天降")


def h(rows) -> str:
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()[:16]


def snap(con, table: str, where: str = "1=1", args: tuple = ()) -> list:
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(f"select * from {table} where {where}", args)]
    con.row_factory = None
    return out


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "snap"
    con = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)
    res: dict = {"tag": tag, "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # 1) 现役书逐本指纹（整本所有相关表）
    con.row_factory = sqlite3.Row
    pids = {r["name"]: r["id"] for r in con.execute("select id,name from projects")}
    con.row_factory = None
    res["projects_now"] = {n: pids.get(n) for n in ACTIVE}
    res["books"] = {}
    for name in ACTIVE:
        pid = pids.get(name)
        if not pid:
            res["books"][name] = {"missing": True}
            continue
        entry = {}
        for t in ("projects", "volumes", "articles", "chapters", "characters",
                  "chapter_summaries", "article_plans", "chapter_memories",
                  "entity_relations", "foreshadows"):
            try:
                if t == "projects":
                    rows = snap(con, t, "id=?", (pid,))
                else:
                    rows = snap(con, t, "project_id=?", (pid,))
            except sqlite3.Error as e:
                entry[t] = f"<{e}>"
                continue
            entry[t] = {"n": len(rows), "sha": h(rows)}
        res["books"][name] = entry
    res["books_fingerprint"] = h(res["books"])

    # 2) 测试书（新建的，预期非零行）
    pid = pids.get(BOOK)
    res["test_book_id"] = pid
    if pid:
        for t in ("volumes", "articles", "chapters", "article_plans", "chapter_memories"):
            try:
                res.setdefault("test_book", {})[t] = len(snap(con, t, "project_id=?", (pid,)))
            except sqlite3.Error as e:
                res["test_book"][t] = f"<{e}>"

    # 3) 禁写表
    res["plot_templates"] = len(snap(con, "plot_templates"))
    con.row_factory = sqlite3.Row        # snap() 会把 row_factory 复位，这里用完再取回
    res["plot_templates_by_status"] = {
        f"{r['scale']}/{r['status']}": r["n"] for r in con.execute(
            "select scale,status,count(*) n from plot_templates group by 1,2")}
    con.row_factory = None
    res["plot_templates_sha"] = h([[r["id"], r["status"], r["updated_at"]]
                                   for r in snap(con, "plot_templates")])
    res["event_skeletons"] = len(snap(con, "event_skeletons"))
    res["event_skeletons_sha"] = h(snap(con, "event_skeletons"))
    res["vector_chunks"] = con.execute("select count(*) from vector_chunks").fetchone()[0]
    con.close()

    p = OUT / f"现役书自证_{tag}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "books"},
                     ensure_ascii=False, indent=1))
    print(f"\n现役书总指纹 books_fingerprint = {res['books_fingerprint']}")
    print(f"→ {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())