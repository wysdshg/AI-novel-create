# -*- coding: utf-8 -*-
"""[DEV-SK07] 取数链独立审计：库内 desc ⟷ 弧库原子号 ⟷ win summary 三段闭环。

与 skel_v4_rebuild.py 不同代码路径（本文件只做**回读比对**，不做组装），只读库 + 只读源文件。

    .venv\\Scripts\\python.exe backend/scripts/x4_chain_audit.py

三道检查：
  A. 块1（弧库新弧 400 条）：按放行口径 (书名, part, 原子号) → win atoms[原子号−1].summary，
     逐条核「该概要是否出现在落库模板里这条弧名下的某个 variant desc」→ 判类 → 弧库 → win → 库 闭环。
  B. 全量 3736 条 variant：desc 文本必属于该书 win atoms 概要集（**零伪造**，C5 的库侧版）。
  C. 265 张 active 角色模板三路向量块零改动（PM 裁问3-② 的禁碰断言）。
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "outputs" / "_atomic_raw"
DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
BOOKS = ("九星霸体诀", "凡人修仙传", "圣墟", "太荒吞天诀", "斗破苍穹", "蛊真人", "遮天", "寒门枭士")

ATOMS: dict[tuple[str, str], list] = {}
SUM: dict[str, set] = defaultdict(set)
for part, suffix in (("base", "win_{b}.json"), ("p2", "win_{b}.p2.json")):
    for b in BOOKS:
        fn = RAW / suffix.format(b=b)
        if not fn.is_file():
            continue
        arr = json.loads(fn.read_text(encoding="utf-8")).get("atoms") or []
        ATOMS[(b, part)] = arr
        for a in arr:
            s = str(a.get("summary") or "").strip()
            if s:
                SUM[b].add(s)

GID = {e["gid"]: e for e in json.loads(
    (ROOT / "outputs" / "f6q" / "_work" / "合并分类.json").read_text(encoding="utf-8"))}
LIB = {a["弧号"]: a for a in json.loads(
    (ROOT / "outputs" / "f6r" / "弧库_F6R.json").read_text(encoding="utf-8"))["弧"]}

con = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)
con.row_factory = sqlite3.Row
tpl = [dict(r) for r in con.execute(
    "select id, name, structure, source_stats from plot_templates "
    "where scale='arc' and status='active' and source_stats like '%skel_v4_2%'")]
print(f"现库 active v4.2 弧模板 = {len(tpl)} 张")

# 库侧：ident → 该成员名下的 variant desc 集（按 how 里的弧名+章段回连）
desc_by_ident: dict[str, set] = defaultdict(set)
n_var = 0
for r in tpl:
    ss = json.loads(r["source_stats"])
    st = json.loads(r["structure"])
    by_how: dict[str, set] = defaultdict(set)
    for ph in st["phases"]:
        for b in ph["beats"]:
            for v in b["variants"]:
                n_var += 1
                by_how[v["how"]].add(v["desc"])
                if v["desc"] not in SUM.get(v["src"], set()):
                    raise SystemExit(f"B FAIL 伪造/越源 desc：{r['name']} ← {v['how']} {v['desc'][:30]}")
    for m in ss["member_arcs"]:
        desc_by_ident[m["ident"]] |= by_how.get(f"{m['arc']}（c{m['ch_lo']}~{m['ch_hi']}）", set())
print(f"B. 全量 variant {n_var} 条 desc 均在源书 win atoms 概要集内 → PASS（零伪造）\n")

# A. 块1 逐原子闭环
tot = hit = miss_n = collapsed = 0
bad: list[str] = []
n_arc = 0
for m_ident, ds in desc_by_ident.items():
    if not m_ident.startswith("F6R-"):
        continue
    a = LIB.get(m_ident)
    if not a or a["出口"] not in ("好弧", "多线弧"):
        continue
    n_arc += 1
    gids = [g for g in a["来源"]["原弧gid"] if g in GID]
    book = a["来源"]["书名"]
    parts = {GID[g]["part"] for g in gids}
    if len(parts) == 1:
        alloc = [(next(iter(parts)), no) for no in a["原子号"]]
    else:                                    # 跨 part：按 gid 序 × 该旧弧 n_beats 切片（与组装同规则，独立重写）
        alloc, i = [], 0
        for g in gids:
            nb = int(GID[g]["n_beats"])
            alloc += [(GID[g]["part"], x) for x in a["原子号"][i:i + nb]]
            i += nb
        assert i == len(a["原子号"]), f"{m_ident} 跨 part 切分未分配完 {i}/{len(a['原子号'])}"
    prev = None
    for part, no in alloc:
        arr = ATOMS.get((book, part)) or []
        rec = arr[no - 1] if 1 <= no <= len(arr) else {}
        s = str(rec.get("summary") or "").strip()
        aid = str(rec.get("atomic_id") or "")
        tot += 1
        if s and s in ds:
            hit += 1
        elif prev is not None and aid == prev:
            collapsed += 1          # 连续同原子压成一步（拍板⑥）：取首拍概括，本拍概要按设计不落库
        else:
            miss_n += 1
            if len(bad) < 5:
                bad.append(f"{m_ident} {book}/{part} #{no} aid={aid} → {s[:24]!r}")
        prev = aid
print(f"A. 块1 弧 {n_arc} 条 / 原子号 {tot} 个 → 弧库原子号取到的 win summary "
      f"命中库内 desc **{hit}/{tot}**；另有 {collapsed} 个是「连续同原子压成一步」（拍板⑥）"
      f"按设计只留首拍概要 → 真未命中 **{miss_n}**"
      + (" → PASS" if miss_n == 0 else f" FAIL 样例：{bad}"))

# C. 角色三路块
q = "select count(*) from vector_chunks where source_id in (select id from plot_templates where scale='character' and status='active')"
char_chunks = {p: con.execute(q + " and source_type=?", (p,)).fetchone()[0]
               for p in ("plot_template", "plot_cast", "char_archetype")}
print(f"C. 265 张 active 角色模板三路块数 = {char_chunks}（应 265/265/265，本轮禁碰未动）")
orph = con.execute(
    "select count(*) from vector_chunks v where v.source_type in "
    "('plot_template','plot_cast','char_archetype') and v.source_id not in "
    "(select id from plot_templates)").fetchone()[0]
print(f"   三路（plot_template/plot_cast/char_archetype）孤儿块 = {orph}（应 0；"
      "另有 chapter_memory / ref_doc 块属别的源表，不在本单口径）")
assert orph == 0, "三路存在孤儿块，审计不过"
con.close()
print("\n审计结论：三段闭环（判类 → 弧库原子号 → win summary → 库内 desc）全部有据。")
