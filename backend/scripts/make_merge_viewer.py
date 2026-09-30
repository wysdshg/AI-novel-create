# -*- coding: utf-8 -*-
"""
跨书归并结果查看器：merge_<书们>.json → 单文件零依赖 HTML
=============================================================
用法：
    python scripts/make_merge_viewer.py --json ../outputs/_atomic_raw/merge_flat_三书.json \
        --out ../outputs/三书跨书归并结果.html
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
from collections import Counter
from pathlib import Path

DB_RO = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"
ROOT = Path(__file__).resolve().parents[2]

BOOK_COLORS = {
    "凡人修仙传": "#2563eb", "斗破苍穹": "#ea580c", "太荒吞天诀": "#7c3aed",
}
BOOK_SHORT = {"凡人修仙传": "凡", "斗破苍穹": "斗", "太荒吞天诀": "太"}


def classify(g: dict) -> str:
    books = g["books"]
    if g["n_members"] == 1:
        return "单弧"
    if len(books) == 1:
        return "单书多弧"
    if len(books) == 2:
        return "两书多弧"
    label = "三书多弧" if len(books) == 3 else "跨书多弧"
    from collections import Counter
    bc = Counter(m["book"] for m in g["members"])
    if any(v >= 2 for v in bc.values()):
        label += "·含一书多弧"
    return label


CLASS_STYLE = {
    "单弧": ("灰", "#6b7280", "#f3f4f6"),
    "单书多弧": ("蓝", "#1d4ed8", "#dbeafe"),
    "两书多弧": ("青", "#0e7490", "#cffafe"),
    "三书多弧·含一书多弧": ("金", "#b45309", "#fef3c7"),
    "三书多弧": ("红", "#b91c1c", "#fee2e2"),
    "跨书多弧·含一书多弧": ("金", "#b45309", "#fef3c7"),
    "跨书多弧": ("红", "#b91c1c", "#fee2e2"),
}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def render(g: dict, vocab: dict) -> str:
    label = classify(g)
    txt, fg, bg = CLASS_STYLE.get(label, ("黑", "#111", "#eee"))
    books = g["books"]
    bc = Counter(m["book"] for m in g["members"])
    n_ref = g.get("n_references") or 0
    ref_books = g.get("ref_books") or []
    ref_note = (f' ｜ <span style="color:#b45309;font-weight:600">参考挂靠 {n_ref}'
                f'（{esc("、".join(ref_books))}）</span>') if n_ref else ""
    book_chips = " ".join(
        f'<span class="bchip" style="color:{BOOK_COLORS.get(b, "#333")}">'
        f'{esc(b)} ×{bc[b]}</span>' for b in books)
    cores = " ".join(
        f'<span class="core" title="{esc(vocab.get(c, {}).get("name", c))}">{esc(c)} {esc(vocab.get(c, {}).get("name", c))}</span>'
        for c in g["cores"]) or '<span class="muted">（无核心——靠包含度归并）</span>'

    rings_html = []
    for r in g["rings"]:
        rep = f'<sup class="rep">×{r["repeat"]}</sup>' if r["repeat"] > 1 else ""
        by = r.get("covered_by") or []
        seen, chips = set(), []
        for x in by:
            book = x.split("·", 1)[0]
            if book in seen:
                continue
            seen.add(book)
            chips.append(f'<span class="dot" style="background:{BOOK_COLORS.get(book, "#888")}"></span>'
                         f'{esc(BOOK_SHORT.get(book, book))}')
        by_html = "".join(chips) if chips else '<span class="muted">—</span>'
        cat = r.get("cat") or ""
        rings_html.append(
            f'<div class="ring"><div class="rid">{esc(r["atomic_id"])}{rep}</div>'
            f'<div class="rname">{esc(r["name"])}</div>'
            f'<div class="rcat">{esc(cat)}</div>'
            f'<div class="rby">{by_html}</div></div>')
    seq_join = '<div class="arrow">→</div>'.join(rings_html)

    rows = []
    for m in g["members"]:
        color = BOOK_COLORS.get(m["book"], "#333")
        seq_str = " → ".join(m["seq"])
        core_str = "、".join(m["cores"]) if m["cores"] else "—"
        detail = []
        for i, s in enumerate(m.get("summaries") or []):
            if not s:
                continue
            aid = m["seq"][i] if i < len(m["seq"]) else "?"
            aname = vocab.get(aid, {}).get("name", aid)
            tags = " ".join(f'<span class="tag">{esc(t)}</span>' for t in (m["tags"][i] if i < len(m.get("tags") or []) else []))
            detail.append(f'<div class="atom-sum"><b>{esc(aid)} {esc(aname)}</b>{tags}<p>{esc(s)}</p></div>')
        rows.append(
            f'<tr><td style="color:{color};font-weight:600">{esc(m["book"])}</td>'
            f'<td>{esc(m["arc_name"])}</td><td class="mono">c{m["ch_lo"]}~{m["ch_hi"]}</td>'
            f'<td class="mono">{esc(core_str)}</td>'
            f'<td class="mono small">{esc(seq_str)}</td>'
            f'<td><details><summary>概括</summary>{"".join(detail)}</details></td></tr>')

    ref_section = ""
    if n_ref:
        ref_rows = []
        for m in g.get("references") or []:
            color = BOOK_COLORS.get(m["book"], "#333")
            ref_rows.append(
                f'<tr><td style="color:{color};font-weight:600">{esc(m["book"])}</td>'
                f'<td>{esc(m["arc_name"])}</td><td class="mono">c{m["ch_lo"]}~{m["ch_hi"]}</td>'
                f'<td class="mono small">{esc(" → ".join(m["seq"]))}</td></tr>')
        ref_section = (
            f'<details class="members"><summary>参考挂靠 {n_ref} 条'
            f'（完全包含于骨架；只做走法参考，不改骨架）</summary>'
            f'<table><thead><tr><th>书</th><th>弧名</th><th>章区间</th><th>原子序列</th></tr></thead>'
            f'<tbody>{"".join(ref_rows)}</tbody></table></details>')

    return f'''
<section class="card" id="g{g["gid"]}">
  <div class="card-h">
    <span class="gid">组#{g["gid"]}</span>
    <span class="cls" style="color:{fg};background:{bg}">{esc(label)}</span>
    <span class="meta">骨架成员 <b>{g["n_members"]}</b>{ref_note} ｜ {book_chips} ｜ 链长 <b>{len(g["seq"])}</b> 环</span>
  </div>
  <div class="cores-lab">核心原子（缺一不可）</div>
  <div class="cores">{cores}</div>
  <div class="rings-lab">合并骨架（每环下方 = 走过此环的书）</div>
  <div class="rings">{seq_join}</div>
  <details class="members"><summary>骨架成员 {g["n_members"]} 条（点开看序列与概括）</summary>
    <table><thead><tr><th>书</th><th>弧名</th><th>章区间</th><th>核心</th><th>原子序列</th><th>起承转合</th></tr></thead>
    <tbody>{"".join(rows)}</tbody></table>
  </details>
  {ref_section}
</section>'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    d = json.loads(Path(args.json).read_text(encoding="utf-8"))
    con = sqlite3.connect(DB_RO, uri=True)
    vocab = {r[0]: {"name": r[1], "cat": r[2]} for r in con.execute(
        "SELECT e.id, e.name, c.name FROM atomic_events e "
        "LEFT JOIN atomic_categories c ON c.id = e.category_id").fetchall()}
    con.close()

    labels = [classify(g) for g in d["groups"]]
    stat = Counter(labels)
    cards = "".join(render(g, vocab) for g in d["groups"])

    chips = "".join(
        f'<span class="stat"><b>{v}</b>{esc(k)}</span>' for k, v in sorted(stat.items()))
    books_str = "、".join(d["books"])

    gate = d.get("gate") or {}
    if gate:
        gate_desc = (f"LCS/min ≥ 动态门槛（较长方 &lt;{gate.get('short_lt')}环 → {gate.get('min_short')}；"
                     f"{gate.get('short_lt')}~{gate.get('long_ge', 10) - 1}环 → {gate.get('min_mid')}；"
                     f"≥{gate.get('long_ge')}环 → {gate.get('min_long')}）")
    else:
        gate_desc = f"LCS/min ≥ {d.get('min_ratio', 0.7)}"

    page = f'''<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>三书跨书归并结果</title>
<style>
  :root {{ --line:#e5e7eb; --muted:#6b7280; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Microsoft YaHei", system-ui, sans-serif; margin: 0; background: #f8fafc; color: #111827; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
  h1 {{ font-size: 22px; margin: 6px 0 2px; }}
  .sub {{ color: var(--muted); font-size: 13px; margin-bottom: 14px; }}
  .top {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }}
  .stat {{ background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px; font-size: 13px; }}
  .stat b {{ font-size: 17px; margin-right: 4px; }}
  .params {{ color: var(--muted); font-size: 12px; margin-bottom: 16px; }}
  .card {{ background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
  .card-h {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }}
  .gid {{ font-weight: 700; font-size: 16px; }}
  .cls {{ border-radius: 999px; padding: 2px 10px; font-size: 12px; font-weight: 600; }}
  .meta {{ font-size: 13px; color: #374151; }}
  .bchip {{ font-weight: 600; margin: 0 4px; font-size: 13px; }}
  .cores-lab, .rings-lab {{ font-size: 12px; color: var(--muted); margin: 8px 0 4px; }}
  .core {{ display: inline-block; border: 1px solid #fca5a5; background: #fef2f2; color: #b91c1c; border-radius: 6px; padding: 2px 8px; margin: 0 6px 4px 0; font-size: 12px; font-weight: 600; }}
  .rings {{ display: flex; flex-wrap: wrap; align-items: stretch; gap: 4px; }}
  .ring {{ min-width: 96px; max-width: 150px; border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; background: #f9fafb; }}
  .rid {{ font-weight: 700; font-size: 13px; }}
  .rep {{ color: #b45309; font-size: 11px; }}
  .rname {{ font-size: 12px; margin: 2px 0; }}
  .rcat {{ font-size: 11px; color: var(--muted); }}
  .rby {{ font-size: 11px; margin-top: 4px; border-top: 1px dashed var(--line); padding-top: 3px; }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin: 0 2px 0 4px; }}
  .arrow {{ align-self: center; color: #9ca3af; font-size: 16px; padding: 0 2px; }}
  details.members {{ margin-top: 10px; }}
  details.members summary {{ cursor: pointer; font-size: 13px; color: #1d4ed8; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 12px; }}
  th, td {{ border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f3f4f6; }}
  .mono {{ font-family: Consolas, monospace; }}
  .small {{ font-size: 11px; }}
  .muted {{ color: var(--muted); }}
  .atom-sum {{ border-left: 3px solid #e5e7eb; margin: 6px 0; padding-left: 8px; }}
  .atom-sum p {{ margin: 2px 0 6px; line-height: 1.55; }}
  .tag {{ display: inline-block; background: #eef2ff; color: #4338ca; border-radius: 4px; padding: 0 6px; font-size: 11px; margin-left: 6px; }}
</style></head>
<body><div class="wrap">
<h1>跨书归并结果：{esc(books_str)}</h1>
<div class="sub">总弧 {d["n_arcs"]} 条 → 归并 {d["n_groups"]} 组 ｜ 生成于 make_merge_viewer.py（docs/10 §11 生成式骨架管线 第②③步）</div>
<div class="top">{chips}</div>
<div class="params">判据（2026-09-19 拍板②）：{gate_desc} 且 LCS/max ≥ {d.get("max_ratio", 0.5)}｜链长上限 {d["max_len"]}（超限仅「完全包含」挂参考，否则另起模板）｜核心单向覆盖（新成员 ⊇ 组核心）｜flat{"/跨书边优先" if d.get("cross_first") else "（分数降序）"}</div>
{cards}
</div></body></html>'''

    out = Path(args.out)
    out.write_text(page, encoding="utf-8")
    print(f"✅ 已生成 {out}（{out.stat().st_size/1024:.0f} KB）｜组 {d['n_groups']}｜分类 {dict(stat)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
