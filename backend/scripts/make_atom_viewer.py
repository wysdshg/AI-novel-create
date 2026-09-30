# -*- coding: utf-8 -*-
"""把窗口化切原子的 JSON 结果渲染成**单文件 HTML 浏览器**（零依赖、双击即看）。

为什么要有它：JSON / markdown 表格看不出"效果"——
一本书 300 个原子 + 90 条弧，需要的是**可搜索、可按弧筛、能一眼看覆盖**的界面。

用法：
    ..\\.venv\\Scripts\\python.exe scripts/make_atom_viewer.py \
        --json outputs/_atomic_raw/win_太荒吞天诀.json \
        --out  outputs/太荒全书切原子结果.html
"""
import argparse
import html
import json
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DB_RO = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"

CSS = """
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
     color:#1f2328;background:#f6f7f9}
.wrap{max-width:1280px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:20px;font-weight:500;margin:0 0 4px}
.sub{color:#6b7280;font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px}
.card .k{font-size:12px;color:#6b7280}
.card .v{font-size:20px;font-weight:500;margin-top:2px}
.card .n{font-size:11px;color:#9ca3af;margin-top:2px}
.bar{margin:18px 0 24px}
.bar .track{height:10px;background:#e5e7eb;border-radius:5px;overflow:hidden;display:flex}
.bar .seg{height:100%}
.bar .lg{display:flex;gap:16px;font-size:12px;color:#6b7280;margin-top:6px;flex-wrap:wrap}
.bar .lg i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
h2{font-size:15px;font-weight:500;margin:26px 0 10px}
.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
input[type=search],select{font:13px/1.5 inherit;padding:6px 10px;border:1px solid #d1d5db;border-radius:7px;background:#fff;min-width:180px}
button{font:13px/1.5 inherit;padding:6px 12px;border:1px solid #d1d5db;border-radius:7px;background:#fff;cursor:pointer}
button.on{background:#1d4ed8;border-color:#1d4ed8;color:#fff}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:8px 10px;text-align:left;vertical-align:top;border-bottom:1px solid #f1f3f5}
th{background:#fafbfc;font-weight:500;color:#4b5563;font-size:12px;position:sticky;top:0}
tr:last-child td{border-bottom:none}
tr.hl{background:#fff7ed}
td.num{white-space:nowrap;color:#374151}
code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12px}
.tag{display:inline-block;background:#f3f4f6;color:#4b5563;border-radius:4px;padding:0 5px;font-size:11px;margin-right:3px}
.fix{color:#b45309;font-size:11px}
.arclist{display:flex;flex-direction:column;gap:8px}
.arc{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px 14px;cursor:pointer}
.arc:hover{border-color:#9ca3af}
.arc.sel{border-color:#1d4ed8;box-shadow:0 0 0 2px #dbeafe}
.arc .top{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.arc .nm{font-weight:500}
.arc .rg{color:#6b7280;font-size:12px;white-space:nowrap}
.arc .chain{margin-top:6px;font-size:12px;color:#374151;word-break:break-all}
.arc .meta{font-size:11px;color:#9ca3af;margin-top:4px}
.empty{color:#9ca3af;padding:24px;text-align:center;background:#fff;border:1px dashed #d1d5db;border-radius:10px}
"""

JS = """
const A = __ATOMS__, AR = __ARCS__;
const bySeq = {}; A.forEach(a => bySeq[a.seq] = a);
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
let curArc = null, q = '';
function render(){
  const term = q.trim().toLowerCase();
  const rows = A.filter(a=>{
    if(curArc!==null && !curArc.atoms.includes(a.seq)) return false;
    if(!term) return true;
    return (String(a.atomic_id)+' '+String(a.summary)+' '+String(a.tags)+' '+a.chapter_start+'-'+a.chapter_end).toLowerCase().includes(term);
  });
  document.getElementById('cnt').textContent = rows.length;
  document.getElementById('atoms').innerHTML = rows.length ? rows.map(a=>`
    <tr class="${a.span_repaired?'hl':''}">
      <td class="num">${a.seq}</td>
      <td class="num">${a.chapter_start}~${a.chapter_end}</td>
      <td class="num"><code>${esc(a.atomic_id)}</code></td>
      <td>${esc(a.atomic_name||'')}</td>
      <td class="num">${(a.summary||'').length}${a.span_repaired?'<br><span class="fix">已修复区间</span>':''}</td>
      <td>${(a.tags||[]).map(t=>'<span class="tag">'+esc(t)+'</span>').join('')}</td>
      <td>${esc(a.summary)}</td>
    </tr>`).join('') : '<tr><td colspan="7"><div class="empty">没有匹配的原子</div></td></tr>';
}
function pickArc(i){
  curArc = (curArc && curArc.i===i) ? null : Object.assign({i:i}, AR[i]);
  document.querySelectorAll('.arc').forEach((el,k)=>el.classList.toggle('sel', !!curArc && k===i));
  render();
}
document.getElementById('search').addEventListener('input', e=>{q=e.target.value;render();});
document.getElementById('clr').addEventListener('click', ()=>{
  curArc=null; q=''; document.getElementById('search').value='';
  document.querySelectorAll('.arc').forEach(el=>el.classList.remove('sel')); render();});
render();
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", default=DB_RO)
    args = ap.parse_args()

    d = json.loads(Path(args.json).read_text(encoding="utf-8"))
    book = d.get("book") or "未知"
    atoms, arcs, wlog = d["atoms"], d["arcs"], d.get("windows") or []

    con = sqlite3.connect(args.db, uri=True)
    names = dict(con.execute("SELECT id, name FROM atomic_events").fetchall())
    cmin, cmax = con.execute(
        "SELECT MIN(chapter_no), MAX(chapter_no) FROM chapter_summaries WHERE book_name=?",
        (book,)).fetchone()
    v3 = con.execute("""SELECT arc_no, MIN(arc_name), MIN(chapter_no), MAX(chapter_no)
                        FROM chapter_summaries WHERE book_name=? AND arc_no IS NOT NULL
                        GROUP BY arc_no ORDER BY arc_no""", (book,)).fetchall()
    con.close()
    for a in atoms:
        a["atomic_name"] = names.get(str(a.get("atomic_id")), "")
        a.setdefault("seq", atoms.index(a) + 1)

    # 覆盖统计
    cov: list[int] = []
    for a in atoms:
        s0, s1 = a.get("chapter_start"), a.get("chapter_end")
        if isinstance(s0, int) and isinstance(s1, int):
            cov.extend(range(s0, s1 + 1))
    full = set(range(cmin, cmax + 1))
    miss = sorted(full - set(cov))
    dup = sorted({x for x in cov if cov.count(x) > 1})
    rep = d.get("partition_repair") or {}
    n_fix = sum(1 for a in atoms if a.get("span_repaired"))
    win_fix = sum(1 for w in wlog if w.get("miss"))

    # 弧 ↔ v3 弧配对
    for i, a in enumerate(arcs):
        a0, a1 = int(a["chapter_start"]), int(a["chapter_end"])
        best, ov = None, -1
        for r in v3:
            o = max(0, min(a1, r[3]) - max(a0, r[2]) + 1)
            if o > ov:
                best, ov = r, o
        a["_i"] = i
        a["v3"] = (f"弧{best[0]} {best[1]}（{best[2]}~{best[3]}）" if best else "—")
    # 原子挂上所属弧，便于按弧筛
    for a in arcs:
        a["atoms"] = [s for s in (a.get("atoms") or [])]

    cnt = Counter(str(a.get("atomic_id")) for a in atoms)
    L = [len(str(a.get("summary") or "")) for a in atoms]
    seg = [int(a["chapter_end"]) - int(a["chapter_start"]) + 1 for a in atoms]
    nl = [int(a["chapter_end"]) - int(a["chapter_start"]) + 1 for a in arcs]

    ok = "✅" if not miss and not dup else "❌"
    html_doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(book)} · 全书切原子结果</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{html.escape(book)} · 全书切原子结果</h1>
<div class="sub">第 {cmin}~{cmax} 章｜窗口 {wlog[0]['end'] - wlog[0]['start'] + 1 if wlog else '?'} 章 × {len(wlog)} 窗（含回炉）｜
数据源 {html.escape(Path(args.json).name)}</div>
<div class="cards">
  <div class="card"><div class="k">弧</div><div class="v">{len(arcs)}</div><div class="n">中位 {statistics.median(nl):.0f} 章｜最长 {max(nl)}</div></div>
  <div class="card"><div class="k">原子</div><div class="v">{len(atoms)}</div><div class="n">中位 {statistics.median(seg):.0f} 章/原子</div></div>
  <div class="card"><div class="k">章覆盖</div><div class="v">{ok} {len(set(cov))}/{len(full)}</div><div class="n">缺 {len(miss)}｜重叠 {len(dup)}</div></div>
  <div class="card"><div class="k">词表使用</div><div class="v">{len(cnt)}/64</div><div class="n">{len(cnt) / 64 * 100:.0f}% 的原子被用到</div></div>
  <div class="card"><div class="k">概括字数</div><div class="v">{statistics.median(L):.0f}</div><div class="n">min {min(L)}｜max {max(L)}｜<80 字 {sum(1 for x in L if x < 80)}</div></div>
  <div class="card"><div class="k">区间被修复</div><div class="v">{n_fix}</div><div class="n">吸收 {rep.get('absorbed', 0)} 章｜原生缺章窗 {win_fix}</div></div>
</div>

<div class="bar"><div class="track"><div class="seg" style="width:{(len(full) - len(miss)) / len(full) * 100:.2f}%;background:#1d4ed8"></div></div>
<div class="lg"><span><i style="background:#1d4ed8"></i>已覆盖 {len(full) - len(miss)} 章</span>
<span><i style="background:#e5e7eb"></i>缺口 {len(miss)} 章</span>
<span>v3 篇章弧 {len(v3)} 条（本书）</span></div></div>

<h2>1. 弧一览（点一条筛出它的原子）</h2>
<div class="arclist" id="arclist">{''.join(
        f'''<div class="arc" onclick="pickArc({a['_i']})">
  <div class="top"><span class="nm">{a['_i'] + 1}. {html.escape(str(a.get('arc_name') or ''))}</span>
  <span class="rg">{a.get('chapter_start')}~{a.get('chapter_end')}（{int(a['chapter_end']) - int(a['chapter_start']) + 1} 章）</span></div>
  <div class="chain">{' → '.join(f"{esc_atom(s, atoms)}" for s in (a.get('atoms') or []))}</div>
  <div class="meta">核心 <code>{html.escape(str(a.get('core_atomics')))}</code>｜对照 {html.escape(str(a.get('v3')))}</div>
</div>''' for a in arcs)}</div>

<h2>2. 原子明细（<span id="cnt">{len(atoms)}</span> / {len(atoms)}）</h2>
<div class="tools">
  <input type="search" id="search" placeholder="搜索：原子号 / 关键词 / 章号 / tag…">
  <button id="clr">清除筛选</button>
</div>
<table><thead><tr><th>seq</th><th>章区间</th><th>原子</th><th>名称</th><th>字数</th><th>tags</th><th>概括（起｜承｜转｜合）</th></tr></thead>
<tbody id="atoms"></tbody></table>
<div class="sub" style="margin-top:10px">橙色行 = 章区间经过确定性修复（模型漏切，已就近吸收）</div>
</div>
<script>{JS.replace('__ATOMS__', json.dumps(atoms, ensure_ascii=False)).replace('__ARCS__', json.dumps(arcs, ensure_ascii=False))}</script>
</body></html>"""
    Path(args.out).write_text(html_doc, encoding="utf-8")
    print(f"✅ 已生成 {args.out}（{len(html_doc) / 1024:.0f} KB）"
          f"｜弧 {len(arcs)}｜原子 {len(atoms)}｜覆盖 {len(set(cov))}/{len(full)}")
    return 0


def esc_atom(seq, atoms):
    a = next((x for x in atoms if x.get("seq") == seq), None)
    return html.escape(str(a.get("atomic_id"))) if a else f"?{seq}"


if __name__ == "__main__":
    sys.exit(main())
