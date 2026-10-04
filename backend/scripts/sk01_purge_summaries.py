# -*- coding: utf-8 -*-
"""[DEV-SK01] 库内残留小清点：删隔离章概括残留 + 处置噪声概括行。

零 LLM 调用：清单驱动 + 确定性截尾，人工逐条判定的处置表写在本文件 NOISE_DECISIONS（可审阅）。

    .venv\\Scripts\\python.exe backend/scripts/sk01_purge_summaries.py --dry-run
    .venv\\Scripts\\python.exe backend/scripts/sk01_purge_summaries.py --apply
    .venv\\Scripts\\python.exe backend/scripts/sk01_purge_summaries.py --verify

四段式（照 clean_novel_chapters.py）：
  --dry-run  按清单+处置表打印将删/将改/待定，不写库
  --apply    先把所有将被删除/改写的**整行** dump 到 SK01_删除备份.json，再动库（事务）
  --verify   按 DB 与文件系统现状重查核账（docs/04 A14：不信返回值）
  --restore-check  只用备份核对可还原性（列齐全 + 示例 INSERT），不改库

口径：
  删除 = SK01_待删行清单.txt 的 (book_name, chapter_no)（B/C 隔离章仍留在库的概括行，65 行）
         ∪ 噪声清单里人工判为 delete 的行（整行就是通知/感言的概括）
  改写 = 噪声清单里人工判为 trim 的行：尾部噪声碎片掐掉，正文保留（summary + summary_raw 同步）
  待定 = hold 态：不动库，逐条进报告交 PM 裁决
"""
from __future__ import annotations

import argparse
import io
import json
import re
import socket
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
SKEL = ROOT / "outputs" / "skel_v4"
DELETE_LIST = SKEL / "SK01_待删行清单.txt"
NOISE_LIST = SKEL / "SK01_噪声概括行清单.txt"
LEDGER = ROOT / "outputs" / "data_clean" / "清洗台账.json"
BACKUP = SKEL / "SK01_删除备份.json"

TABLE = "chapter_summaries"
NOISE_WORDS = ["翻页", "本章未完", "本章已完成", "求票", "求月票", "求推荐票", "求收藏",
               "点击收藏", "未完待续", "请点击下一页", "ps:"]
TRIM_MAX_CHARS = 20   # 碎片上限：超出说明整行大半是噪声，应判 delete 而非 trim

# 🔴 人工逐条过目后的处置表（21 行噪声清单里、不在 65 待删清单的 7 行；其余 14 行随待删清单删）
# delete：整行就是通知/感言的概括，零剧情信息；trim：噪声只是尾部碎片；hold：不动，交 PM 裁决。
NOISE_DECISIONS: dict[tuple[str, int], tuple[str, str]] = {
    ("修真四万年", 692): ("delete",
        "整行为感言单章的概括（2015 最后一天开单章求月票+元旦问候+写作困难与经济压力自述），无任何剧情事件"),
    ("回到明朝当王爷", 67): ("trim",
        "正文为第67章三场景概括，噪声仅尾部碎片『本章未完。』→ 掐掉碎片，正文保留"),
    ("蛊真人", 101): ("delete",
        "整行为上架感言概括（致谢+写作初衷+求月票首订订阅+VIP 群），且自证『新出场人物：无。重要物品/地点：无。』"),
    ("蛊真人", 152): ("delete",
        "整行为求月票提示的概括，自述『未涉及具体情节和人物事件，仅包含上传进度和读者互动信息』"),
    ("赘婿", 336): ("delete",
        "整行为推书+月票+写作计划单章的概括（推《九星天辰诀》、每月 25~30 章计划），无剧情"),
    ("赘婿", 403): ("delete",
        "整行为拉票单章的概括（求月票+更新慢自述+《隐杀》起承转合谈话），无剧情事件"),
    ("遮天", 225): ("hold",
        "『源天书自动翻页』是正文剧情用语，属噪声词误命中；整行是叶凡松林困局+源天纹络的正常剧情概括 → 不动"),
}

DEL_LINE = re.compile(r"^(?P<book>\S+?)\s{2,}c(?P<no>\d+)\s{2,}(?P<title>.+?)\s{2,}(?P<file>\S.*?)$")
NOISE_HEAD = re.compile(r"^(?P<book>\S+?)\s+c(?P<no>\d+)《(?P<title>.*)》$")
SEQ = re.compile(r"^(\d{3,5})_")
# IGNORECASE 与 SQLite LIKE 的 ASCII 大小写不敏感口径对齐（词表里的 ps:）
NOISE_RE = re.compile("|".join(re.escape(w) for w in NOISE_WORDS), re.IGNORECASE)
BASELINE_DOWNSTREAM = {"atomic_variants": 335, "plot_templates": 529, "event_skeletons": 6}
BASELINE_TOTAL = 16758


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


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_delete_list() -> list[dict]:
    rows = []
    for raw in io.open(DELETE_LIST, encoding="utf-8"):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith(("SK01", "格式:")):
            continue
        m = DEL_LINE.match(line)
        if not m:
            raise ValueError(f"待删清单解析失败: {line!r}")
        rows.append({"book": m["book"], "no": int(m["no"]), "title": m["title"], "file": m["file"]})
    return rows


def parse_noise_list() -> list[dict]:
    rows, cur = [], None
    for raw in io.open(NOISE_LIST, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        m = NOISE_HEAD.match(line.strip())
        if m:
            cur = {"book": m["book"], "no": int(m["no"]), "title": m["title"], "preview": ""}
            rows.append(cur)
        elif cur is not None and line.startswith("  "):
            cur["preview"] = line.strip()
    return rows


def read_rows(con: sqlite3.Connection, keys: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
    out: dict[tuple[str, int], list[dict]] = {}
    for book, no in keys:
        rs = con.execute(f"select * from {TABLE} where book_name=? and chapter_no=?", (book, no)).fetchall()
        out[(book, no)] = [dict(r) for r in rs]
    return out


def first_noise_pos(text: str) -> tuple[int, str] | None:
    m = NOISE_RE.search(text or "")
    return (m.start(), m.group()) if m else None


def trim_tail(text: str) -> tuple[str, str] | None:
    """掐掉行中第一处噪声词起至结尾的碎片；碎片超 TRIM_MAX_CHARS 则返回 None（不该 trim）。"""
    hit = first_noise_pos(text)
    if hit is None:
        return None
    idx = hit[0]
    frag = text[idx:]
    new = text[:idx].rstrip()
    if not new or len(frag) > TRIM_MAX_CHARS:
        return None
    if new[-1] in "，、；：,;":
        new = new.rstrip("，、；：,;") + "。"
    return new, frag


def build_plan(con: sqlite3.Connection) -> dict:
    """只读构建执行计划：将删 / 将改 / 待定 / 跳过（含原因）。"""
    cols = [r[1] for r in con.execute(f"pragma table_info({TABLE})")]
    dels = parse_delete_list()
    noises = parse_noise_list()
    del_keys = {(d["book"], d["no"]) for d in dels}

    to_delete, to_update, held, skipped = [], [], [], []
    del_skipped, del_gone = set(), set()

    for d in dels:
        hits = read_rows(con, [(d["book"], d["no"])])[(d["book"], d["no"])]
        key = {"book": d["book"], "no": d["no"], "list_title": d["title"], "source": "待删清单(隔离章)"}
        dkey = (d["book"], d["no"])
        if not hits:
            del_gone.add(dkey)
        elif len(hits) > 1:
            del_skipped.add(dkey)
            skipped.append({**key, "reason": f"命中 {len(hits)} 行，不唯一，拒绝盲删"})
        elif (hits[0]["title"] or "").strip() != d["title"].strip():
            del_skipped.add(dkey)
            skipped.append({**key, "db_title": hits[0]["title"],
                            "reason": "DB title 与清单不一致，跳过待人工"})
        else:
            to_delete.append({**key, "row": hits[0], "reason": "B/C 隔离章的概括残留"})

    for n in noises:
        key = (n["book"], n["no"])
        if key in del_keys:
            # 两清单重叠：以删行为准；只有待删那步被跳过时才算连带未处置
            if key in del_skipped:
                skipped.append({"book": n["book"], "no": n["no"], "list_title": n["title"],
                                "source": "噪声清单∩待删清单",
                                "reason": "待删清单里此行被跳过（见上），噪声行随之未删"})
            continue
        act, why = NOISE_DECISIONS.get(key, ("hold", "无处置记录 → 默认不动"))
        hits = read_rows(con, [key])[key]
        item = {"book": n["book"], "no": n["no"], "list_title": n["title"], "source": "噪声清单",
                "reason": why, "action": act}
        if not hits:
            del_gone.add(key)
        elif len(hits) > 1:
            skipped.append({**item, "reason": f"命中 {len(hits)} 行，不唯一，拒绝盲删"})
        elif (hits[0]["title"] or "").strip() != n["title"].strip():
            skipped.append({**item, "db_title": hits[0]["title"],
                            "reason": "DB title 与清单不一致，跳过待人工"})
        elif act == "delete":
            to_delete.append({**item, "row": hits[0]})
        elif act == "hold":
            held.append({**item, "row": hits[0]})
        else:  # trim
            row = hits[0]
            if first_noise_pos(row["summary"] or "") is None:
                del_gone.add(key)   # 已改干净：幂等，无需再动
                continue
            res = trim_tail(row["summary"] or "")
            if res is None:
                skipped.append({**item, "reason": "碎片过长或正文为空，trim 不安全 → 转人工"})
                continue
            new_summary, frag = res
            res_raw = trim_tail(row["summary_raw"] or "") if row["summary_raw"] else None
            again = first_noise_pos(new_summary)
            if again is not None:
                skipped.append({**item, "reason": f"掐一次后仍含噪声词 {again[1]!r} → 转人工"})
                continue
            to_update.append({**item, "row": row, "fragment": frag, "new_summary": new_summary,
                              "new_summary_raw": (res_raw[0] if res_raw else None)})

    return {"cols": cols, "to_delete": to_delete, "to_update": to_update,
            "held": held, "skipped": skipped, "gone": sorted(del_gone),
            "n_del_list": len(dels), "n_noise_list": len(noises)}


def derive_ledger_keys(con: sqlite3.Connection) -> tuple[set, Counter]:
    """重跑待删清单推导逻辑：台账 B/C 隔离文件 -> (书名, 章号) -> ∩ 库现状。"""
    books = [r["book_name"] for r in con.execute(f"select distinct book_name from {TABLE}")]
    ledger = json.load(io.open(LEDGER, encoding="utf-8"))
    keys, unmapped = set(), Counter()
    for r in ledger:
        if r["action"] not in ("B", "C"):
            continue
        parts = re.split(r"[\\/]", r.get("new_rel") or r["rel"])
        dirname, fname = parts[-2], parts[-1]
        if dirname in books:
            book = dirname
        else:
            base = re.sub(r"[（(].*?[)）]$", "", re.split(r"\s+-\s+", dirname)[0]).strip()
            cands = [b for b in books if base == b or base.startswith(b)]
            book = cands[0] if len(cands) == 1 else None
        if book is None:
            unmapped[dirname] += 1
            continue
        m = SEQ.match(fname)
        if not m:
            unmapped[f"{dirname}:{fname}"] += 1
            continue
        keys.add((book, int(m.group(1))))
    still = set()
    for book, no in keys:
        if con.execute(f"select 1 from {TABLE} where book_name=? and chapter_no=? limit 1",
                       (book, no)).fetchone():
            still.add((book, no))
    return still, unmapped


def noise_hits(con: sqlite3.Connection, col: str = "summary") -> list[dict]:
    clause = " or ".join([f"{col} like ?"] * len(NOISE_WORDS))
    rows = con.execute(
        f"select book_name, chapter_no, title, {col} from {TABLE} where {clause} order by book_name, chapter_no",
        [f"%{w}%" for w in NOISE_WORDS]).fetchall()
    return [dict(r) for r in rows]


def backend_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 8000)) == 0


def print_plan(plan: dict) -> None:
    print(f"[计划] 待删清单 {plan['n_del_list']} 行 / 噪声清单 {plan['n_noise_list']} 行")
    print(f"  将删 {len(plan['to_delete'])} 行  将改 {len(plan['to_update'])} 行  "
          f"待定 {len(plan['held'])} 行  跳过 {len(plan['skipped'])} 行  "
          f"已无需动作 {len(plan['gone'])} 键（行已删或已干净）")
    print(f"  将删按书: {dict(Counter(x['book'] for x in plan['to_delete']))}")
    print("\n-- 将删（逐行）--")
    for x in plan["to_delete"]:
        print(f"  {x['book']} c{x['no']} id={x['row']['id']} 《{x['row']['title']}》 "
              f"[{x['source']}] 概括 {len(x['row']['summary'] or '')} 字")
    print("\n-- 将改（逐行，前后对照）--")
    for x in plan["to_update"]:
        print(f"  {x['book']} c{x['no']} id={x['row']['id']} 《{x['row']['title']}》")
        print(f"    掐掉碎片: {x['fragment']!r}")
        print(f"    before: …{(x['row']['summary'] or '')[-60:]}")
        print(f"    after : …{x['new_summary'][-60:]}")
        print(f"    summary_raw 同步: {x['new_summary_raw'] is not None}")
    print("\n-- 待定（不动库）--")
    for x in plan["held"]:
        print(f"  {x['book']} c{x['no']} 《{x['row']['title']}》 理由: {x['reason']}")
    if plan["skipped"]:
        print("\n-- 跳过（需人工）--")
        for x in plan["skipped"]:
            print(f"  {x['book']} c{x['no']} 《{x.get('list_title','')}》 {x['reason']}")


def do_apply(con: sqlite3.Connection, plan: dict) -> None:
    if not plan["to_delete"] and not plan["to_update"]:
        print("[apply] 计划为空（幂等：库已清干净）→ 不动库、不覆盖备份")
        return

    snap = {"script": "sk01_purge_summaries.py", "ts_utc": now_utc(), "db": str(DB),
            "columns": plan["cols"],
            "counts": {"deleted": len(plan["to_delete"]), "updated": len(plan["to_update"]),
                       "held": len(plan["held"]), "skipped": len(plan["skipped"])},
            "deleted": [{**{"_source": x["source"], "_reason": x["reason"]}, **x["row"]}
                        for x in plan["to_delete"]],
            "updated": [{**{"_source": x["source"], "_reason": x["reason"],
                            "_fragment_removed": x["fragment"],
                            "_new_summary": x["new_summary"],
                            "_new_summary_raw": x["new_summary_raw"]}, **x["row"]}
                        for x in plan["to_update"]]}
    BACKUP.write_text(json.dumps(snap, ensure_ascii=False, indent=1), "utf-8")
    print(f"[apply] 备份落盘 {BACKUP.name}: deleted={len(snap['deleted'])} "
          f"updated={len(snap['updated'])} → 备份行数 {len(snap['deleted']) + len(snap['updated'])}")

    try:
        for x in plan["to_delete"]:
            cur = con.execute(f"delete from {TABLE} where id=? and book_name=? and chapter_no=?",
                              (x["row"]["id"], x["book"], x["no"]))
            print(f"  DEL {x['book']} c{x['no']} rowcount={cur.rowcount}")
        for x in plan["to_update"]:
            new_raw = x["new_summary_raw"]
            if new_raw is None:
                new_raw = x["row"].get("summary_raw")
            cur = con.execute(f"update {TABLE} set summary=?, summary_raw=? where id=?",
                              (x["new_summary"], new_raw, x["row"]["id"]))
            print(f"  UPD {x['book']} c{x['no']} rowcount={cur.rowcount}")
        con.commit()
        print("[apply] 已提交")
    except Exception as e:
        con.rollback()
        sys.exit(f"[apply] 回滚: {e}")


def do_verify(con: sqlite3.Connection, plan: dict) -> None:
    print("[verify-1] 重跑待删清单推导 → 剩余应为 0")
    still, unmapped = derive_ledger_keys(con)
    print(f"  台账 B/C 推导键 ∩ 库现状 = {len(still)} 行  {'PASS' if not still else 'FAIL ' + str(sorted(still)[:10])}")
    print(f"  未映射目录（那些书本就未入库，无害）: {len(unmapped)} 个")
    list_keys = {(d["book"], d["no"]) for d in parse_delete_list()}
    remain_list = [k for k in sorted(list_keys)
                   if con.execute(f"select 1 from {TABLE} where book_name=? and chapter_no=? limit 1", k).fetchone()]
    print(f"  65 清单逐键复查残留 = {len(remain_list)} 行  "
          f"{'PASS' if not remain_list else 'FAIL ' + str(remain_list[:10])}")

    print("\n[verify-2] 重跑噪声词查询 → 0 行（待定行除外）")
    held_keys = {(x["book"], x["no"]) for x in plan["held"]}
    for col in ("summary", "summary_raw"):
        hits = noise_hits(con, col)
        exc = [h for h in hits if (h["book_name"], h["chapter_no"]) in held_keys]
        bad = [h for h in hits if (h["book_name"], h["chapter_no"]) not in held_keys]
        print(f"  {col}: 命中 {len(hits)}，待定除外 {len(exc)}，未解释 {len(bad)} "
              f"{'PASS' if not bad else 'FAIL'}")
        for h in bad:
            print(f"    [未解释] {h['book_name']} c{h['chapter_no']} {h['title']}")
        for h in exc:
            print(f"    [待定除外] {h['book_name']} c{h['chapter_no']} 《{h['title']}》 "
                  f"…{(h[col] or '')[-40:]}")

    print("\n[verify-3] 备份行数 = 删除 + 改写")
    if not BACKUP.exists():
        print("  FAIL 备份文件不存在！")
        return
    snap = json.load(io.open(BACKUP, encoding="utf-8"))
    n_del, n_upd = len(snap["deleted"]), len(snap["updated"])
    print(f"  backup deleted={n_del} updated={n_upd} sum={n_del + n_upd}")
    still_there = sum(1 for r in snap["deleted"]
                      if con.execute(f"select 1 from {TABLE} where id=? limit 1", (r["id"],)).fetchone())
    print(f"  备份中仍存在于库的 deleted id（应为 0）= {still_there} "
          f"{'PASS' if still_there == 0 else 'FAIL'}")
    miss_col = [r["id"] for r in snap["deleted"] + snap["updated"]
                if not set(snap["columns"]) <= set(r.keys())]
    print(f"  字段不全（不可逐行还原）的行 = {len(miss_col)}  "
          f"{'PASS' if not miss_col else 'FAIL ' + str(miss_col[:5])}")
    for r in snap["updated"]:
        row = con.execute(f"select summary from {TABLE} where id=?", (r["id"],)).fetchone()
        ok = row and row["summary"] == r["_new_summary"]
        print(f"  改写落地核对 {r['book_name']} c{r['chapter_no']}: {'PASS' if ok else 'FAIL MISMATCH'}")

    print("\n[verify-4] 清单外零改动（下游表 + 总数）")
    for t, base in BASELINE_DOWNSTREAM.items():
        n = con.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"  {t}: {n} (基线 {base}) {'PASS' if n == base else 'FAIL'}")
    total = con.execute(f"select count(*) from {TABLE}").fetchone()[0]
    exp = BASELINE_TOTAL - n_del
    print(f"  {TABLE} 总数 = {total}，期望 {BASELINE_TOTAL}-{n_del}={exp} "
          f"{'PASS' if total == exp else 'FAIL'}")


def do_restore_check(con: sqlite3.Connection) -> None:
    snap = json.load(io.open(BACKUP, encoding="utf-8"))
    cols = snap["columns"]
    print(f"[restore-check] 备份列 {len(cols)}，deleted={len(snap['deleted'])} updated={len(snap['updated'])}")
    bad = [r.get("id") for r in snap["deleted"] + snap["updated"] if not set(cols) <= set(r)]
    print(f"  列缺失行 = {len(bad)} {'PASS' if not bad else 'FAIL'}")
    if snap["deleted"]:
        r = snap["deleted"][0]
        vals = ", ".join(repr(r[c]) for c in cols)
        print(f"  示例还原 SQL: INSERT INTO {TABLE} ({', '.join(cols)}) VALUES ({vals[:200]}…);")
    if snap["updated"]:
        r = snap["updated"][0]
        old = repr(r["summary"])[:120]
        print(f"  改写回滚 SQL: UPDATE {TABLE} SET summary={old}… WHERE id={r['id']!r};")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--restore-check", action="store_true")
    a = ap.parse_args()
    mode = next(k for k, v in vars(a).items() if v)

    if mode == "apply" and backend_running():
        sys.exit("[apply] 拒绝执行：127.0.0.1:8000 有后端在跑，破坏性操作须先停服务（任务单坑条目）")

    ro = mode != "apply"
    con = sqlite3.connect(DB.as_uri() + ("?mode=ro" if ro else ""), uri=True)
    con.row_factory = sqlite3.Row
    plan = build_plan(con)
    print(f"SK01 {mode} @ {now_utc()}\n")
    if mode == "dry_run":
        print_plan(plan)
    elif mode == "apply":
        print_plan(plan)
        print()
        do_apply(con, plan)
    elif mode == "verify":
        do_verify(con, plan)
    else:
        do_restore_check(con)
    con.close()


if __name__ == "__main__":
    LOG_NAME = {"--dry-run": "SK01_dryrun.txt", "--apply": "SK01_apply.txt",
                "--verify": "SK01_verify.txt", "--restore-check": "SK01_restore_check.txt"}
    flag = next((k for k in LOG_NAME if k in sys.argv), None)
    if flag is None:
        raise SystemExit("用法: --dry-run | --apply | --verify | --restore-check")
    sys.stdout = _Tee(SKEL / LOG_NAME[flag])
    try:
        main()
    finally:
        sys.stdout.flush()
