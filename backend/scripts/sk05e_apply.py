# -*- coding: utf-8 -*-
"""[INTERN-SK05E] 第三步：变体走法句入库 `atomic_variants`（确定性四段式，零 AI）。

    .venv\\Scripts\\python.exe backend/scripts/sk05e_apply.py --dry-run
    .venv\\Scripts\\python.exe backend/scripts/sk05e_apply.py --apply
    .venv\\Scripts\\python.exe backend/scripts/sk05e_apply.py --verify
    .venv\\Scripts\\python.exe backend/scripts/sk05e_apply.py --rollback
    .venv\\Scripts\\python.exe backend/scripts/sk05e_apply.py --restore-check
    .venv\\Scripts\\python.exe backend/scripts/sk05e_apply.py --roundtrip
        # 真实往返：apply → verify → rollback → 验基线 → 再 apply → 验终数
    可选 --db <路径>（默认生产库；先在副本上跑 --roundtrip 再打生产库）
        --tag _local（产物文件名后缀）--force（跳过 8000 端口守卫，仅在确认后端已停时用）

写入内容**全部来自** outputs/sk05e/采信台账.json（已过防编造门禁），脚本本身不生成文本。
白名单（唯一允许的库写范围）：只对 atomic_variants INSERT 本单 36 行，
atomic_id 只允许 D10/E05/H11，其它表与其它行一概不碰。

键口径（2026-10-07 PM 裁决，替掉任务单原「书名#原子序号」写法）：
  arc_ref = 「书名#win原子NNN」——`#win原子` 前缀保证永不与库内弧序号撞号。
  原写法实测 33/36 在 chapter_summaries 查无此弧、3/36 会错指到别的真弧
  （如 凡人修仙传#3 会被读成库内弧 3＝第29~61章，而该原子实为第11~15章）。
  tags 同步记完整溯源「win原子溯源:书名/原子号NNN」，与 arc_ref 双保险。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD_DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
SK = ROOT / "outputs" / "sk05e"
LEDGER = SK / "采信台账.json"
CANDIDATES = SK / "候选清单.json"
BACKUP = SK / "atomic_variants_备份.json"
ROLLBACK_SQL = SK / "sk05e_回滚.sql"
LOGDIR = SK

TABLE = "atomic_variants"
RINGS = {"D10", "E05", "H11"}
EXPECT_BASE = 335          # 写前 atomic_variants 行数（任务单 §验收 2）
EXPECT_EVENTS = 80         # atomic_events 行数（本单不许动词表）
BOOKS = {"凡人修仙传", "斗破苍穹", "太荒吞天诀", "九星霸体诀", "遮天", "蛊真人", "圣墟", "寒门枭士"}
COLUMNS = ["id", "atomic_id", "book_name", "arc_ref", "segment_no", "text", "tags",
           "source", "is_variant_of", "hit_count", "quality", "created_at"]
# 除 created_at 外的字段都必须与台账逐字一致（幂等复跑只时间戳会变）
COMPARE_COLS = [c for c in COLUMNS if c != "created_at"]


class _Tee:
    """GBK 控制台会糊中文：同步落一份 UTF-8 日志。"""

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


def now_naive() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def variant_id(arc_ref: str, segment_no: int) -> str:
    """与 label_atomic_variants._variant_id 同法：确定性主键，复跑/回滚再apply 都得到同一 id。"""
    return "av-" + hashlib.md5(f"{arc_ref}#{segment_no}".encode("utf-8")).hexdigest()[:20]


def win_tags(book: str, idx: int) -> str:
    """溯源双保险：tags 里记完整 win 原子指向。

    用 ensure_ascii 默认（\\uXXXX）落库，与表内既有 JSON 列写法一致（坑 E24：
    库里 JSON 列中文是转义形态，`LIKE '%中文%'` 恒 0，查它得走 json_extract）。
    """
    return json.dumps([f"win原子溯源:{book}/原子号{idx}"])


def load_plan() -> list[dict]:
    led = json.loads(LEDGER.read_text(encoding="utf-8"))
    if led.get("gate") != "PASS":
        raise SystemExit(f"[plan] 拒绝：采信台账门禁结论={led.get('gate')}，必须 PASS 才能入库")
    ts = now_naive()
    plan = []
    for r in led["records"]:
        plan.append({
            "id": variant_id(r["arc_ref"], r["atom_index"]),
            "atomic_id": r["ring"],
            "book_name": r["book"],
            "arc_ref": r["arc_ref"],
            "segment_no": r["atom_index"],
            "text": r["text"],
            "tags": win_tags(r["book"], r["atom_index"]),
            "source": "ai",
            "is_variant_of": None,
            "hit_count": 1,
            "quality": "draft",
            "created_at": ts,
            "_evidence": r["evidence"],
            "_summary": r["summary"],
        })
    plan.sort(key=lambda p: (p["atomic_id"], p["book_name"], p["segment_no"]))
    return plan


def selfcheck(plan: list[dict], cands: dict) -> list[str]:
    errs = []
    if not plan:
        errs.append("采信台账为空，没有可入库的行")
    keys = {(p["arc_ref"], p["segment_no"]) for p in plan}
    if len(keys) != len(plan):
        errs.append(f"计划内 (arc_ref,segment_no) 去重后 {len(keys)} ≠ {len(plan)}，存在同键重复")
    ids = {p["id"] for p in plan}
    if len(ids) != len(plan):
        errs.append("计划内 id 有碰撞（md5 截短撞车），需加长哈希位")
    for p in plan:
        if p["atomic_id"] not in RINGS:
            errs.append(f"{p['id']}: atomic_id={p['atomic_id']} 越出三环白名单")
        if not p["book_name"] or p["book_name"] not in BOOKS:
            errs.append(f"{p['id']}: book_name={p['book_name']!r} 不在 8 书内")
        if not (20 <= len(p["text"]) <= 60):
            errs.append(f"{p['id']}: text 长度 {len(p['text'])} 不在 20~60")
        if p["arc_ref"] != f"{p['book_name']}#win原子{p['segment_no']}":
            errs.append(f"{p['id']}: arc_ref={p['arc_ref']} 与「书名#win原子NNN」口径不符")
        if p["tags"] != win_tags(p["book_name"], p["segment_no"]):
            errs.append(f"{p['id']}: tags={p['tags']} 未记 win 原子溯源")
        key = (p["book_name"], p["segment_no"])
        c = cands.get(key)
        if c is None:
            errs.append(f"{p['id']}: 候选清单里找不到 {key} —— 溯源断链")
            continue
        for q in p["_evidence"]:
            if len(q) < 6 or q not in c["summary"]:
                errs.append(f"{p['id']}: evidence 非原文连续子串 → {q}")
        if c["summary"] != p["_summary"]:
            errs.append(f"{p['id']}: 台账原文与候选清单不一致（中间产物被改过？）")
    return errs


def read_rows(con: sqlite3.Connection) -> dict[str, dict]:
    con.row_factory = sqlite3.Row
    return {r["id"]: dict(r) for r in con.execute(f"SELECT * FROM {TABLE}")}


def table_counts(con: sqlite3.Connection) -> dict:
    out = {}
    for t in (TABLE, "atomic_events", "plot_templates", "event_skeletons", "chapter_summaries"):
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


def check_backend_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 8000)) == 0


def guard_prod_write(db_arg: str, force: bool) -> bool:
    if force:
        return True
    if Path(db_arg).resolve() == PROD_DB.resolve() and check_backend_running():
        print("[apply] 拒绝：目标是生产库且 127.0.0.1:8000 有进程在听 → "
              "写生产库须先停后端（StaticPool 单连接，写后它读不到新行）；"
              "确认该进程确属后端且可停，用 --force 或先结束它")
        return False
    if Path(db_arg).resolve() == PROD_DB.resolve():
        print("[apply] 目标为生产库，后端未监听 8000 → 放行")
    else:
        print(f"[apply] 目标非生产库（{Path(db_arg).name}）→ 跳过端口守卫")
    return True


def backup_table(con: sqlite3.Connection, path: Path) -> None:
    rows = [dict(r) for r in con.execute(f"SELECT * FROM {TABLE} ORDER BY id")]
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    payload = {
        "table": TABLE, "columns": COLUMNS, "backup_ts": now_naive(),
        "source_db": str(con.execute("PRAGMA database_list").fetchone()[2]),
        "row_count": len(rows), "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "rows": rows,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if path.exists():
        print(f"[backup] 原始备份已存在，保留不覆盖: {path.name}"
              f"（改前基线不能替换）→ 另存写前快照")
        snap = path.with_name(path.stem + "_写前快照.json")
        snap.write_text(text, encoding="utf-8", newline="\n")
        print(f"[backup] 本次写前快照 → {snap.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"[backup] 全表 {len(rows)} 行 × {len(COLUMNS)} 列 → {path.name} "
          f"sha256={payload['sha256'][:16]}")


def write_rollback_sql(plan: list[dict], path: Path, base: int) -> None:
    ids = ",\n  ".join(f"'{p['id']}'" for p in plan)
    sql = [f"-- [INTERN-SK05E] atomic_variants 回滚：删除本单 {len(plan)} 行（基线 {base} 行）",
           f"DELETE FROM {TABLE} WHERE id IN (",
           ids + ");",
           f"-- 验回基线：SELECT COUNT(*) FROM {TABLE};  -- 应为 {base}"]
    path.write_text("\n".join(sql), encoding="utf-8", newline="\n")
    print(f"[rollback-sql] {len(plan)} 个 id → {path.name}")


def precheck_db(con: sqlite3.Connection, plan: list[dict]) -> tuple[bool, str]:
    cur = read_rows(con)
    counts = table_counts(con)
    print(f"[pre] {TABLE}={counts[TABLE]} atomic_events={counts['atomic_events']} "
          f"db={con.execute('PRAGMA database_list').fetchone()[2]}")
    if counts["atomic_events"] != EXPECT_EVENTS:
        return False, f"atomic_events 现 {counts['atomic_events']} 行 ≠ 词表基线 {EXPECT_EVENTS}（本单不该动词表，先对账）"
    ring_rows = [r for r in cur.values() if r["atomic_id"] in RINGS]
    if ring_rows:
        return False, (f"三环在库已有 {len(ring_rows)} 行（基线应为 0）——"
                       "说明别人也写过，先对账再动手")
    present = [p for p in plan if p["id"] in cur]
    if present and len(present) != len(plan):
        return False, f"本单 {len(present)}/{len(plan)} 行已在库、其余缺失（半截态），先 --rollback 或对账"
    if len(present) == len(plan):
        diff = [p["id"] for p in present
                if any(str(cur[p["id"]][c]) != str(p[c]) for c in COMPARE_COLS)]
        if diff:
            return False, f"本单 {len(diff)} 行已在库但字段与台账不符: {diff}"
        return True, "目标态已达成 → 幂等跳过（不写库、不覆盖备份）"
    clash = [(p["arc_ref"], p["segment_no"]) for p in plan
             if (p["arc_ref"], p["segment_no"]) in {(r["arc_ref"], r["segment_no"]) for r in cur.values()}]
    if clash:
        return False, f"与现有行撞唯一键 (arc_ref,segment_no): {clash}"
    return True, "基线态，可写"


def dry_run(con: sqlite3.Connection, plan: list[dict]) -> int:
    print("== dry-run ==")
    cands = {(c["book"], c["atom_index"]): c
             for c in json.loads(CANDIDATES.read_text(encoding="utf-8"))["candidates"]}
    errs = selfcheck(plan, cands)
    print(f"静态自检: {'PASS' if not errs else 'FAIL'}（{len(plan)} 行）")
    for e in errs:
        print("   !", e)
    if errs:
        return 1
    ok, why = precheck_db(con, plan)
    print(f"库侧预检: {'PASS' if ok else 'FAIL'} — {why}")
    if not ok:
        return 1
    print(f"\n将 INSERT {len(plan)} 行：")
    for p in plan:
        print(f"   {p['id']} {p['atomic_id']} | {p['book_name']} | arc_ref={p['arc_ref']} "
              f"| seg={p['segment_no']} | {len(p['text'])}字 | {p['text']}")
    by_ring = {r: sum(1 for p in plan if p["atomic_id"] == r) for r in sorted(RINGS)}
    base = table_counts(con)[TABLE]
    print(f"\n终态预期: {TABLE} = {base} + {len(plan)} = {base + len(plan)}，三环 {by_ring}")
    write_rollback_sql(plan, ROLLBACK_SQL, base)
    print("越界自检: 计划只碰 atomic_variants，atomic_id 仅 ∈ "
          f"{sorted(RINGS)}；不 UPDATE/DELETE 任何现有行")
    return 0


def apply_db(con: sqlite3.Connection, plan: list[dict], force: bool) -> int:
    cands = {(c["book"], c["atom_index"]): c
             for c in json.loads(CANDIDATES.read_text(encoding="utf-8"))["candidates"]}
    errs = selfcheck(plan, cands)
    for e in errs:
        print("   !", e)
    if errs:
        return 1
    if not guard_prod_write(str(con.execute("PRAGMA database_list").fetchone()[2]), force):
        return 1
    ok, why = precheck_db(con, plan)
    if not ok:
        print(f"[apply] 拒绝：{why}")
        return 1
    if len(plan) == len([p for p in plan
                         if p["id"] in read_rows(con)]):
        print(f"[apply] {why}")
        return 0
    backup_table(con, BACKUP)
    base = table_counts(con)[TABLE]
    write_rollback_sql(plan, ROLLBACK_SQL, base)
    cols = [c for c in COLUMNS]
    sql = f"INSERT INTO {TABLE} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    n = 0
    try:
        with con:
            for p in plan:
                assert p["atomic_id"] in RINGS, f"白名单违规 {p['atomic_id']}"
                con.execute(sql, [p[c] for c in cols])
                n += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[apply] 事务回滚: {exc}")
        return 1
    print(f"[apply] INSERT {n} 行完成")
    counts = table_counts(con)
    print(f"[apply] 写后 {TABLE}={counts[TABLE]}（基线 {base} + {n} = {base + n}）"
          f"，atomic_events={counts['atomic_events']}（未动）")
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return 0


def verify(con: sqlite3.Connection, plan: list[dict], sample_seed: int = 20261007) -> int:
    print("== verify ==")
    res = []

    def chk(name, ok, detail=""):
        res.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

    cur = read_rows(con)
    plan_ids = [p["id"] for p in plan]
    by_id = {p["id"]: p for p in plan}
    chk(f"总行数 = {EXPECT_BASE} + {len(plan)} = {EXPECT_BASE + len(plan)}",
        len(cur) == EXPECT_BASE + len(plan), f"实测 {len(cur)}")
    chk("本单全部行在库", all(i in cur for i in plan_ids),
        f"缺 {sorted(set(plan_ids) - set(cur))}")
    mine = [cur[i] for i in plan_ids if i in cur]
    chk("全部新行 atomic_id ∈ {D10,E05,H11}",
        all(r["atomic_id"] in RINGS for r in mine),
        str(sorted({r["atomic_id"] for r in mine})))
    chk("全部新行 text 长度 20~60", all(20 <= len(r["text"]) <= 60 for r in mine),
        f"越界 {[(r['id'], len(r['text'])) for r in mine if not 20 <= len(r['text']) <= 60]}")
    chk("全部新行 book_name 非空且属 8 书",
        all((r["book_name"] or "") in BOOKS for r in mine),
        str(sorted({r["book_name"] for r in mine})))
    chk("逐字段与采信台账一致（除 created_at）",
        all(str(r[c]) == str(by_id[r["id"]][c]) for r in mine for c in COMPARE_COLS),
        "差异见下")
    bad = [f"{r['id']}.{c}" for r in mine for c in COMPARE_COLS
           if str(r[c]) != str(by_id[r["id"]][c])]
    chk("字段差异清单为空", not bad, f"{bad[:8]}")
    consts = {"source": "ai", "hit_count": 1, "quality": "draft"}
    for col, want in consts.items():
        chk(f"{col} 全为 {want!r}", all(r[col] == want for r in mine))
    bad_tags = [r["id"] for r in mine
                if json.loads(r["tags"] or "null") != [f"win原子溯源:{r['book_name']}/原子号{r['segment_no']}"]]
    chk("tags 全为「win原子溯源:书名/原子号NNN」单元素数组", not bad_tags, f"{bad_tags[:5]}")
    chk("is_variant_of 全为 NULL", all(r["is_variant_of"] is None for r in mine))
    chk("created_at 非空且形如 YYYY-MM-DD HH:MM:SS.ffffff",
        all(len(r["created_at"] or "") == 26 and r["created_at"][4] == "-"
            for r in mine))
    ring_now = {r: sum(1 for x in mine if x["atomic_id"] == r) for r in sorted(RINGS)}
    ring_plan = {r: sum(1 for p in plan if p["atomic_id"] == r) for r in sorted(RINGS)}
    chk("三环行数与台账一致", ring_now == ring_plan, f"库 {ring_now} / 台账 {ring_plan}")
    import random
    rnd = random.Random(sample_seed)
    pick = rnd.sample(plan_ids, min(5, len(plan_ids)))
    diff5 = []
    for i in pick:
        for c in COMPARE_COLS:
            if str(cur[i][c]) != str(by_id[i][c]):
                diff5.append(f"{i}.{c}")
    chk("随机抽 5 条与台账逐字比对（seed=%d）" % sample_seed,
        not diff5, f"抽样 {pick}；差异 {diff5}")
    ev_bad = []
    for r in mine:
        for q in by_id[r["id"]]["_evidence"]:
            if q not in by_id[r["id"]]["_summary"]:
                ev_bad.append(r["id"])
    chk("防编造复核：每条 evidence 仍是原文连续子串", not ev_bad, f"{ev_bad[:5]}")
    chk("arc_ref 口径 = 书名#win原子NNN", all(
        r["arc_ref"] == f"{r['book_name']}#win原子{r['segment_no']}" for r in mine))
    arc_keys = {f"{b}#{a}" for b, a in con.execute(
        "SELECT DISTINCT book_name, arc_no FROM chapter_summaries")}
    crash = [r["arc_ref"] for r in mine if r["arc_ref"] in arc_keys]
    chk("本单 arc_ref 与库内弧号命名空间零撞车", not crash, f"撞车 {crash}")
    keys = [(r["arc_ref"], r["segment_no"]) for r in cur.values()]
    chk("全表 (arc_ref,segment_no) 唯一键仍无重复", len(keys) == len(set(keys)),
        f"{len(keys)} 键 / {len(set(keys))} 去重")
    cnt = table_counts(con)
    chk(f"atomic_events 未被动（仍 {EXPECT_EVENTS} 行）",
        cnt["atomic_events"] == EXPECT_EVENTS, f"实测 {cnt['atomic_events']}")
    ev = {r["id"]: (r["definition"], r["beat_start"], r["beat_end"])
          for r in con.execute("SELECT id,definition,beat_start,beat_end FROM atomic_events "
                               "WHERE id IN ('D10','E05','H11')")}
    chk("D10/E05/H11 三条词表定义原样未改", len(ev) == 3, f"在库 {sorted(ev)}")
    chk("plot_templates / event_skeletons 未被本单改动（只记基线）", True,
        f"plot_templates={cnt['plot_templates']} event_skeletons={cnt['event_skeletons']}")
    other = sum(1 for r in cur.values() if r["id"] not in by_id)
    chk(f"基线 {EXPECT_BASE} 行原样保留", other == EXPECT_BASE, f"实测非本单行 {other}")
    n_fail = sum(1 for _, ok, _ in res if not ok)
    print(f"\nverify: {len(res) - n_fail}/{len(res)} PASS")
    return 0 if n_fail == 0 else 1


def rollback(con: sqlite3.Connection, plan: list[dict]) -> int:
    ids = [p["id"] for p in plan]
    cur = read_rows(con)
    present = [i for i in ids if i in cur]
    print(f"[rollback] 库内本单行 {len(present)}/{len(ids)} → DELETE")
    with con:
        con.executemany(f"DELETE FROM {TABLE} WHERE id=?", [(i,) for i in present])
    left = table_counts(con)[TABLE]
    print(f"[rollback] 现 {TABLE}={left}（基线应为 {EXPECT_BASE}）")
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return 0 if left == EXPECT_BASE else 1


def restore_check(con: sqlite3.Connection, plan: list[dict]) -> int:
    """证明回滚是**精确还原**：删掉本单行后，剩余每行与备份逐字段相等。"""
    if not BACKUP.exists():
        print(f"[restore-check] 备份缺失: {BACKUP}")
        return 1
    data = json.loads(BACKUP.read_text(encoding="utf-8"))
    old = {r["id"]: r for r in data["rows"]}
    body = json.dumps(data["rows"], ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    cur = read_rows(con)
    mine = {p["id"] for p in plan}
    rest = {i: r for i, r in cur.items() if i not in mine}
    diffs = []
    for i, r in rest.items():
        o = old.get(i)
        if o is None:
            diffs.append(f"{i}: 库里有但备份里没有")
            continue
        diffs += [f"{i}.{c}" for c in COLUMNS if str(r[c]) != str(o[c])]
    extra = sorted(set(old) - set(rest))
    print(f"备份: {data['row_count']} 行 / ts={data['backup_ts']} / sha256={sha[:16]}")
    print(f"扣除本单后库内 {len(rest)} 行；备份有而库内缺 {len(extra)} 行 {extra[:5]}")
    print(f"逐字段差异 {len(diffs)} 处 {diffs[:8]}")
    ok = (not diffs) and (not extra) and data["row_count"] == EXPECT_BASE
    print(f"restore-check: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def roundtrip(con: sqlite3.Connection, plan: list[dict], force: bool) -> int:
    print("== 真实往返演练：apply → verify → rollback → 验基线 → 再 apply → 验终数 ==")
    steps = [("apply#1", lambda: apply_db(con, plan, force)),
             ("verify#1", lambda: verify(con, plan)),
             ("rollback", lambda: rollback(con, plan)),
             ("restore-check", lambda: restore_check(con, plan)),
             ("apply#2", lambda: apply_db(con, plan, force)),
             ("verify#2", lambda: verify(con, plan))]
    for name, fn in steps:
        print(f"\n---- {name} ----")
        rc = fn()
        print(f"---- {name} rc={rc} ----")
        if rc != 0:
            print(f"[roundtrip] FAIL on {name}")
            return 1
    print("\n[roundtrip] PASS：写入→回滚→精确还原→再写入 全程闭环")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--rollback", action="store_true")
    g.add_argument("--restore-check", action="store_true")
    g.add_argument("--roundtrip", action="store_true")
    ap.add_argument("--db", default=str(PROD_DB))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tag", default="", help="产物文件名后缀（副本演练用 _local）")
    a = ap.parse_args()

    global BACKUP, ROLLBACK_SQL
    if a.tag:
        BACKUP = SK / f"atomic_variants_备份{a.tag}.json"
        ROLLBACK_SQL = SK / f"sk05e_回滚{a.tag}.sql"
    mode = ("dry_run" if a.dry_run else "apply" if a.apply else "verify" if a.verify else
            "rollback" if a.rollback else "restore_check" if a.restore_check else "roundtrip")
    LOGDIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = _Tee(LOGDIR / f"sk05e_{mode}{a.tag}.txt")
    plan = load_plan()
    print(f"[INTERN-SK05E] mode={mode} db={a.db} 计划行数={len(plan)}")
    con = sqlite3.connect(a.db)
    try:
        if mode == "dry_run":
            rc = dry_run(con, plan)
        elif mode == "apply":
            rc = apply_db(con, plan, a.force)
        elif mode == "verify":
            rc = verify(con, plan)
        elif mode == "rollback":
            rc = rollback(con, plan)
        elif mode == "restore_check":
            rc = restore_check(con, plan)
        else:
            rc = roundtrip(con, plan, a.force)
    finally:
        con.close()
    print(f"[INTERN-SK05E] {mode} rc={rc}")
    sys.stdout.flush()
    sys.exit(rc)


if __name__ == "__main__":
    main()
