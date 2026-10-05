# -*- coding: utf-8 -*-
"""[DEV-SK05B] F03/F07 定义校订（本单唯一允许的库写：atomic_events 的 F03、F07 两行 definition）。

    .venv\\Scripts\\python.exe backend/scripts/sk05b_lexicon_fix.py --dry-run
    ... --apply / --verify / --restore-check      （可加 --db <副本> 先试跑）

依据：任务单 [DEV-SK05B] §一.3 划界「F03 造已知的，F07 发明新的」。
不碰其它行、不碰其它表；改前全表备份。四段式与幂等口径照 SK05 的 sk05_lexicon_apply.py。
"""
from __future__ import annotations

import argparse
import io
import json
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD_DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
OUT = ROOT / "outputs" / "sk05b"
TABLE = "atomic_events"
COLUMNS = ["id", "name", "category_id", "definition", "beat_start", "beat_mid",
           "beat_turn", "beat_end", "is_core_capable", "domain", "scope_tags",
           "status", "created_at"]
EXPECT = {"total": 77, "active": 76, "candidate": ["D08"]}

DEFINITIONS = {
    "F03": "制造、修复或改装已有器物（兵器、法宝、器械、工具）；划界：F03 造已知的，"
           "新工艺/新器物的首次试制归 F07",
    "F07": "研发试制新工艺或新器物（创新过程，如新配方的首次试制成功）；划界：F07 发明新的，"
           "照已知做法制造修复归 F03",
}
WHITELIST = set(DEFINITIONS)


class _Tee:
    def __init__(self, path: Path) -> None:
        self._f = io.open(path, "w", encoding="utf-8")
        self._con = sys.__stdout__

    def write(self, s: str) -> int:
        self._f.write(s)
        self._con.write(s.encode("gbk", "replace").decode("gbk"))
        return len(s)

    def flush(self) -> None:
        self._f.flush()
        self._con.flush()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rows_of(con) -> dict[str, dict]:
    con.row_factory = sqlite3.Row
    return {r["id"]: dict(r) for r in con.execute(f"SELECT * FROM {TABLE}")}


def backup(con, path: Path) -> None:
    rows = list(rows_of(con).values())
    if path.exists():
        print(f"[backup] 原始备份已存在，保留不覆盖: {path.name}（{len(rows)} 行是当前态）")
        snap = path.with_name(path.stem + "_写前快照.json")
        snap.write_text(json.dumps({"rows": rows, "ts": now_utc()}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print(f"[backup] 本次写前快照 → {snap.name}")
        return
    path.write_text(json.dumps({"table": TABLE, "columns": COLUMNS, "backup_ts": now_utc(),
                                "source_db": str(con.execute("PRAGMA database_list").fetchone()[2]),
                                "row_count": len(rows), "rows": rows},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[backup] 全表 {len(rows)} 行 × {len(COLUMNS)} 列 → {path}")


def guard(db_arg: str, force: bool) -> bool:
    if force or Path(db_arg).resolve() != PROD_DB.resolve():
        print(f"[apply] 目标非生产库或已 --force，跳过端口守卫（db={db_arg}）")
        return True
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", 8000)) == 0:
            print("[apply] 拒绝：生产库 + 8000 有进程在听，须先停后端（详见坑库 E16）")
            return False
    return True


def dry_run(con) -> int:
    cur = rows_of(con)
    print(f"现状 {len(cur)} 行 / 目标白名单 {sorted(WHITELIST)}")
    for _id, newdef in DEFINITIONS.items():
        now_def = cur.get(_id, {}).get("definition")
        if now_def is None:
            print(f"  ! {_id} 不在库")
            return 1
        print(f"\n  {_id} {cur[_id]['name']}")
        print(f"    现定义: {now_def}")
        print(f"    新定义: {newdef}")
        print(f"    变化: {'需改写' if now_def != newdef else '已达标（跳过）'}")
        print(f"    四拍保持不动（本单只校定义）: 起={cur[_id]['beat_start'][:24]}…")
    print(f"\n越界自检：只准备触碰 {sorted(WHITELIST)}；其余 {len(cur) - len(WHITELIST)} 行零改动")
    return 0


def apply_db(con, force: bool) -> int:
    if not guard(str(con.execute("PRAGMA database_list").fetchone()[2]), force):
        return 1
    cur = rows_of(con)
    todo = {k: v for k, v in DEFINITIONS.items() if cur.get(k, {}).get("definition") != v}
    print(f"[apply] 写前 {len(cur)} 行；待改写 {sorted(todo) or '无'}")
    if not todo:
        print("[apply] 已达标 → 幂等跳过（不写库、不覆盖备份）")
        return 0
    backup(con, OUT / "atomic_events_备份.json")
    with con:
        for k, v in todo.items():
            assert k in WHITELIST
            con.execute(f"UPDATE {TABLE} SET definition=? WHERE id=?", (v, k))
            print(f"   UPDATE {k}.definition")
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
    after = rows_of(con)
    print(f"[apply] 写后 {len(after)} 行 active {sum(1 for r in after.values() if r['status']=='active')}")
    return 0


def verify(con) -> int:
    r = []

    def chk(name, ok, detail=""):
        r.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail else ''}")

    cur = rows_of(con)
    chk("行数/状态未变（只改定义）", len(cur) == EXPECT["total"]
        and sum(1 for v in cur.values() if v["status"] == "active") == EXPECT["active"]
        and sorted(k for k, v in cur.items() if v["status"] != "active") == EXPECT["candidate"])
    for k, v in DEFINITIONS.items():
        chk(f"{k} 定义已落库且逐字一致", cur.get(k, {}).get("definition") == v,
            f"现值 {cur.get(k, {}).get('definition')!r}")
    chk("F03/F07 定义互斥可判（各含对方名作划界锚）",
        all("F0" in DEFINITIONS[k] for k in DEFINITIONS))
    drift = [x["id"] for x in cur.values()
             if x["id"] not in WHITELIST and x["definition"] in DEFINITIONS.values()]
    chk("没有第三行被写进这两个定义", not drift, drift)
    for t, want in (("plot_templates", 1145), ("event_skeletons", 6), ("atomic_variants", 335)):
        got = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        chk(f"{t} 未被动（基线 {want}）", got == want, got)
    print(f"\nverify: {sum(r)}/{len(r)} PASS")
    return 0 if all(r) else 1


def restore_check(con) -> int:
    p = OUT / "atomic_events_备份.json"
    if not p.exists():
        print("备份缺失")
        return 1
    data = json.loads(p.read_text(encoding="utf-8"))
    old = {r["id"]: r for r in data["rows"]}
    cur = rows_of(con)
    print(f"备份 {len(old)} 行 ts={data.get('backup_ts')}；现库 {len(cur)} 行")
    diffs = []
    sql = [f"-- [DEV-SK05B] 回滚（备份 {data.get('backup_ts')}）"]
    for k in sorted(WHITELIST):
        if cur.get(k, {}).get("definition") != old[k]["definition"]:
            diffs.append(k)
            sql.append(f"UPDATE {TABLE} SET definition="
                       f"'{old[k]['definition'].replace(chr(39), chr(39) * 2)}' WHERE id='{k}';")
        else:
            sql.append(f"-- {k} 已是备份值，无需还原")
    (OUT / "sk05b_回滚.sql").write_text("\n".join(sql), encoding="utf-8")
    print(f"待还原行 {diffs or '无'}；回滚 SQL → {OUT / 'sk05b_回滚.sql'}")
    ok = len(old) == 77 and len(cur) == 77
    print(f"restore-check: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--restore-check", action="store_true")
    ap.add_argument("--db", default=str(PROD_DB))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    mode = ("dry_run" if a.dry_run else "apply" if a.apply else
            "verify" if a.verify else "restore_check")
    sys.stdout = _Tee(OUT / f"sk05b_lexicon_{mode}{a.tag}.txt")
    con = sqlite3.connect(a.db)
    try:
        fn = {"dry_run": dry_run, "apply": lambda c: apply_db(c, a.force),
              "verify": verify, "restore_check": restore_check}[mode]
        rc = fn(con)
    finally:
        con.close()
    print(f"[DEV-SK05B] {mode} rc={rc}")
    sys.stdout.flush()
    sys.exit(rc)


if __name__ == "__main__":
    main()
