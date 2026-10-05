# -*- coding: utf-8 -*-
"""[DEV-SK05C] F03/F07 四拍同步（本单唯一允许的库写：atomic_events 的 F03、F07 两行 beat 四列）。

    .venv\\Scripts\\python.exe backend/scripts/sk05c_lexicon_beats.py --dry-run
    ... --apply / --verify / --restore-check      （可加 --db <副本> 先试跑）

依据：[DEV-SK05B] 已落库的新定义「F03 造已知的 / F07 发明新的」，旧拍面未随定义同步
（F03 缺「修复/改装」字句、F07 缺「首次试制」字句）。四段式与幂等口径照 sk05b_lexicon_fix.py。
守卫：改前态必须 ∈ {旧拍（基线）, 新拍（目标）}，二者皆非 → 疑似并发改动，拒写（E19）。
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
OUT = ROOT / "outputs" / "sk05c"
TABLE = "atomic_events"
COLUMNS = ["id", "name", "category_id", "definition", "beat_start", "beat_mid",
           "beat_turn", "beat_end", "is_core_capable", "domain", "scope_tags",
           "status", "created_at"]
BEAT_COLS = ["beat_start", "beat_mid", "beat_turn", "beat_end"]
EXPECT = {"total": 77, "active": 76, "candidate": ["D08"]}

# 新拍（照新定义改写：造已知的含修复/改装；发明新的强调首次试制与划界）
NEW_BEATS = {
    "F03": {
        "beat_start": "需制造、修复或改装一件已有器物（兵器、法宝、器械、工具）",
        "beat_mid": "照已知做法备料施工：或铸新器、或修旧损、或改装加料，工序反复",
        "beat_turn": "材料不济、器灵失控、雷劫引动或被人夺器",
        "beat_end": "得到可用的成品、修复件或改装件，战力或生产效率提升",
    },
    "F07": {
        "beat_start": "现有器物或做法不敷使用，定下研发试制新工艺、新器物的目标",
        "beat_mid": "反复配料试作、拆解改良、记录失败参数，逐步逼近可行方案",
        "beat_turn": "关键工序卡死、材料不济或被人窃密抢先",
        "beat_end": "新工艺或新器物首次试制成功，技术优势落到己方手里；照此仿制量产转归 F03",
    },
}
# 旧拍（改前基线，2026-10-05 盘点实测值；用于并发守卫与 rollback 核对）
OLD_BEATS = {
    "F03": {
        "beat_start": "需某件兵器、器械或工具，或受托为他人打造",
        "beat_mid": "寻材打造，反复失败，器胚渐成",
        "beat_turn": "器灵失控、雷劫引动或被人夺器",
        "beat_end": "得到成品器物，用途与战力/生产效率提升",
    },
    "F07": {
        "beat_start": "现有器物不敷使用，定下试制新器新法的目标",
        "beat_mid": "反复配料试作、拆解改良，记录失败参数逐步逼近",
        "beat_turn": "关键工序卡死、材料不济或被人窃密抢先",
        "beat_end": "试制成功并定型量产，一项技术优势落到己方手里",
    },
}
WHITELIST = set(NEW_BEATS)
# 定义列引用已由 SK05B 落库的文本，verify 时逐字比对
DEFINITIONS = {
    "F03": "制造、修复或改装已有器物（兵器、法宝、器械、工具）；划界：F03 造已知的，"
           "新工艺/新器物的首次试制归 F07",
    "F07": "研发试制新工艺或新器物（创新过程，如新配方的首次试制成功）；划界：F07 发明新的，"
           "照已知做法制造修复归 F03",
}


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


def beats_of(row: dict) -> dict:
    return {c: (row.get(c) or "") for c in BEAT_COLS}


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


def state_of(cur: dict, k: str) -> str:
    now = beats_of(cur[k])
    if now == NEW_BEATS[k]:
        return "target"
    if now == OLD_BEATS[k]:
        return "baseline"
    return "conflict"


def dry_run(con) -> int:
    cur = rows_of(con)
    print(f"现状 {len(cur)} 行 / 目标白名单 {sorted(WHITELIST)}")
    for k in sorted(WHITELIST):
        st = state_of(cur, k)
        print(f"\n  {k} {cur[k]['name']}  状态={st}")
        for c in BEAT_COLS:
            mark = "==" if (cur[k].get(c) or "") == NEW_BEATS[k][c] else "->"
            print(f"    {mark} {c}: 现={cur[k].get(c)!r}")
            if mark == "->":
                print(f"          新={NEW_BEATS[k][c]!r}")
    print(f"\n越界自检：只准备触碰 {sorted(WHITELIST)} × {BEAT_COLS}；其余 {len(cur) - len(WHITELIST)} 行零改动")
    return 0


def apply_db(con, force: bool) -> int:
    if not guard(str(con.execute("PRAGMA database_list").fetchone()[2]), force):
        return 1
    cur = rows_of(con)
    states = {k: state_of(cur, k) for k in WHITELIST}
    bad = [k for k, s in states.items() if s == "conflict"]
    if bad:
        print(f"[apply] 拒绝：{bad} 拍面既非基线也非目标（疑似并发改动）")
        return 1
    todo = {k: NEW_BEATS[k] for k in WHITELIST if states[k] == "baseline"}
    print(f"[apply] 写前 {len(cur)} 行；状态 {states}；待改写 {sorted(todo) or '无'}")
    if not todo:
        print("[apply] 已达标 → 幂等跳过（不写库、不覆盖备份）")
        return 0
    backup(con, OUT / "atomic_events_备份.json")
    with con:
        for k, beats in todo.items():
            assert k in WHITELIST
            sets = ", ".join(f"{c}=?" for c in BEAT_COLS)
            con.execute(f"UPDATE {TABLE} SET {sets} WHERE id=?", (*[beats[c] for c in BEAT_COLS], k))
            print(f"   UPDATE {k}.{'/'.join(BEAT_COLS)}")
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
    chk("行数/状态未变（只改四拍）", len(cur) == EXPECT["total"]
        and sum(1 for v in cur.values() if v["status"] == "active") == EXPECT["active"]
        and sorted(k for k, v in cur.items() if v["status"] != "active") == EXPECT["candidate"])
    for k in sorted(WHITELIST):
        chk(f"{k} 四拍已落库且逐字一致", beats_of(cur[k]) == NEW_BEATS[k],
            "现拍≠新拍" if beats_of(cur[k]) != NEW_BEATS[k] else "")
        chk(f"{k} 定义（SK05B 落库文本）未被动", cur[k]["definition"] == DEFINITIONS[k])
    # 拍面与定义对照：F03 含修复/改装、F07 含首次试制
    chk("F03 拍面与「造已知的」定义对照一致（含制造/修复/改装）",
        all(w in NEW_BEATS["F03"]["beat_start"] + NEW_BEATS["F03"]["beat_mid"]
            for w in ("制造", "修复", "改装")))
    chk("F07 拍面与「发明新的」定义对照一致（含试制/首次）",
        "试制" in NEW_BEATS["F07"]["beat_start"] and "首次试制" in NEW_BEATS["F07"]["beat_end"])
    # 备份对照：仅白名单行的 beat 四列变化，其余一切零改动
    p = OUT / "atomic_events_备份.json"
    if p.exists():
        old = {x["id"]: x for x in json.loads(p.read_text(encoding="utf-8"))["rows"]}
        diffs = []
        for k, o in old.items():
            c = cur.get(k)
            if c is None:
                diffs.append((k, "行丢失"))
                continue
            if k in WHITELIST:
                for f in COLUMNS:
                    if f in BEAT_COLS:
                        continue
                    if c.get(f) != o.get(f):
                        diffs.append((k, f))
                for f in BEAT_COLS:
                    if c.get(f) != NEW_BEATS[k][f]:
                        diffs.append((k, f, "≠新拍"))
            elif c != o:
                diffs.append((k, "非白名单行被改动"))
        chk("备份对照：仅 F03/F07 的 beat 四列变化，余 75 行与余列零改动", not diffs, diffs[:5])
    else:
        print("  ! 备份缺失，跳过备份对照")
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
    sql = [f"-- [DEV-SK05C] 回滚（备份 {data.get('backup_ts')}）"]
    for k in sorted(WHITELIST):
        nowb, oldb = beats_of(cur[k]), beats_of(old[k])
        changed = [c for c in BEAT_COLS if nowb[c] != oldb[c]]
        if changed:
            diffs.extend(f"{k}.{c}" for c in changed)
            sets = ", ".join(f"{c}={_q(oldb[c])}" for c in BEAT_COLS)
            sql.append(f"UPDATE {TABLE} SET {sets} WHERE id='{k}';   -- 还原 {changed}")
        else:
            sql.append(f"-- {k} 四拍已是备份值，无需还原")
    # 备份保真核对：旧拍值须与硬编码基线一致（防备份被顶替）
    fidelity = [k for k in WHITELIST if beats_of(old[k]) != OLD_BEATS[k]]
    (OUT / "sk05c_回滚.sql").write_text("\n".join(sql), encoding="utf-8")
    print(f"待还原字段 {diffs or '无'}；备份保真 {'OK' if not fidelity else f'异常 {fidelity}'}")
    print(f"回滚 SQL → {OUT / 'sk05c_回滚.sql'}")
    ok = len(old) == 77 and len(cur) == 77 and not fidelity
    print(f"restore-check: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--restore-check", action="store_true")
    ap.add_argument("--db", default=str(PROD_DB))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    mode = ("dry_run" if a.dry_run else "apply" if a.apply else
            "verify" if a.verify else "restore_check")
    sys.stdout = _Tee(OUT / f"sk05c_beats_{mode}.txt")
    con = sqlite3.connect(a.db)
    try:
        fn = {"dry_run": dry_run, "apply": lambda c: apply_db(c, a.force),
              "verify": verify, "restore_check": restore_check}[mode]
        rc = fn(con)
    finally:
        con.close()
    print(f"[DEV-SK05C] {mode} rc={rc}")
    sys.stdout.flush()
    sys.exit(rc)


if __name__ == "__main__":
    main()
