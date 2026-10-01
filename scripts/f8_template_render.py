# -*- coding: utf-8 -*-
"""F8 模板画廊渲染：tpl_*.json → templates_gallery.html（档5~3 全字段卡，档2~1 紧凑卡）"""
import glob
import html
import json
import os
from datetime import datetime

GRADE_NAME = {5: "全书级/主角", 4: "小说级", 3: "卷级", 2: "篇章级", 1: "断续配角"}
AGG = r"E:\AI小说创作\outputs\f8_p0\aggregate_final\aggregate.json"
TPL_DIR = r"E:\AI小说创作\outputs\f8_p0\templates"
OUT = r"E:\AI小说创作\outputs\f8_p0\templates\templates_gallery.html"

TRAIT_LABEL = {"altruism": "利他↔自私", "honor": "信义↔背信", "mercy": "仁慈↔狠辣",
               "resolve": "坚毅↔易摧", "decisiveness": "果决↔犹豫", "discipline": "自律↔放纵",
               "risk": "冒险↔稳健", "rationality": "理性↔冲动", "guile": "城府↔直率",
               "idealism": "理想↔务实", "warmth": "热忱↔冷漠", "dominance": "强势↔随和"}

def trait_bar(name, val):
    pct = abs(val) / 10 * 50
    bar = '<div style="flex:1;background:#e5e7eb;border-radius:3px;height:12px;position:relative">'
    if val:
        left = f"left:{50 - pct}%" if val < 0 else "left:50%"
        color = "#f59e0b" if val < 0 else "#3b82f6"
        bar += f'<div style="position:absolute;top:0;bottom:0;{left};width:{pct}%;background:{color};border-radius:3px"></div>'
    bar += '<div style="position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#9ca3af"></div></div>'
    return (f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;font-size:11px">'
            f'<span style="width:96px;color:#374151">{TRAIT_LABEL[name]}</span>{bar}'
            f'<span style="width:30px;text-align:right;font-weight:600">{val:+d}</span></div>')

def full_card(nm, t, meta):
    traits = t.get("traits", {})
    basis = t.get("trait_basis", {})
    bars = "".join(trait_bar(k, traits.get(k, 0)) for k in TRAIT_LABEL)
    basis_html = "".join(f'<li><b>{TRAIT_LABEL.get(k, k)}</b>：{html.escape(str(v))}</li>' for k, v in basis.items())
    v = t.get("voice", {})
    refs = "".join(f'<span style="background:#f3f4f6;border-radius:4px;padding:1px 6px;margin-right:6px;font-size:11px">'
                   f'第{r.get("chapter")}章 {html.escape(str(r.get("scene",""))[:60])}</span>' for r in v.get("sample_refs", []))
    pats = "".join(f'<li>{html.escape(str(p))}</li>' for p in t.get("behavior_patterns", []))
    rels = "".join(f'<li>{html.escape(h.get("target",""))}：{html.escape(str(h.get("pattern","")))}</li>'
                   for h in t.get("relation_patterns", []))
    total_on = meta.get("total_on", "?")
    return f'''<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;margin:10px 0">
<div style="display:flex;align-items:center;gap:8px"><span style="font-size:17px;font-weight:700">{html.escape(nm)}</span>
<span style="background:#dbeafe;color:#1d4ed8;border-radius:999px;padding:1px 9px;font-size:11px;font-weight:600">{html.escape(str(t.get("slot","?")))}</span>
<span style="background:#dcfce7;color:#15803d;border-radius:999px;padding:1px 9px;font-size:11px">{html.escape(str(t.get("mode","?")))}</span>
<span style="color:#6b7280;font-size:11px">出场{total_on}章</span></div>
<p style="color:#374151;margin:8px 0 2px;font-size:13px">{html.escape(str(t.get("desc","")))}</p>
<p style="color:#6b7280;font-size:12px;margin:2px 0"><b>位阶：</b>{html.escape(str(t.get("ranks","")))}</p>
<div style="margin:8px 0">{bars}</div>
{"<ul style='color:#374151;font-size:12px;margin:4px 0'>" + basis_html + "</ul>" if basis_html else ""}
<div style="background:#f9fafb;border-radius:8px;padding:8px 12px;margin:8px 0;font-size:12px;color:#374151">
<b>voice</b>｜语域：{html.escape(str(v.get("语域","")))}｜幽默：{html.escape(str(v.get("幽默类型","")))}｜攻防：{html.escape(str(v.get("攻防模式","")))}｜注意力：{html.escape(str(v.get("注意力偏向","")))}<br>
<b>记忆点：</b>{html.escape(str(v.get("记忆点","")))}<br><b>样本指针：</b>{refs}</div>
<div style="display:flex;gap:14px"><div style="flex:1.2"><b style="font-size:12px">行为模式</b><ul style="font-size:12px;color:#374151;margin:3px 0;padding-left:16px">{pats}</ul></div>
<div style="flex:1"><b style="font-size:12px">关系模式</b><ul style="font-size:12px;color:#374151;margin:3px 0;padding-left:16px">{rels}</ul></div></div></div>'''

def compact_card(nm, t, meta):
    top = sorted(t.get("traits", {}).items(), key=lambda x: -abs(x[1]))[:4]
    traits = "、".join(f"{TRAIT_LABEL.get(k, k)}{v:+d}" for k, v in top if v)
    pats = "；".join(html.escape(str(p)) for p in t.get("behavior_patterns", [])[:2])
    return (f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:8px 12px;margin:6px 0;font-size:12px">'
            f'<b>{html.escape(nm)}</b> <span style="color:#6b7280">{html.escape(str(t.get("slot","")))}｜{html.escape(str(t.get("mode","")))}｜出场{meta.get("total_on","?")}章</span><br>'
            f'<span style="color:#374151">{html.escape(str(t.get("desc",""))[:90])}</span><br>'
            f'<span style="color:#6b7280">{traits}</span><br><span style="color:#9ca3af">{pats}</span></div>')

agg = json.load(open(AGG, encoding="utf-8"))
meta = {r["name"]: {"total_on": r["total_on"], "grade": r["final_grade"]} for r in agg["rows"]}
tpls = {}
for p in glob.glob(os.path.join(TPL_DIR, "tpl_*.json")):
    try:
        t = json.load(open(p, encoding="utf-8"))
        nm = os.path.basename(p)[4:-5]
        tpls[nm] = t
    except Exception:
        pass
by_grade = {g: [] for g in range(5, 0, -1)}
for nm, t in tpls.items():
    g = t.get("grade") or meta.get(nm, {}).get("grade")
    if g in by_grade:
        by_grade[g].append((nm, t))
for g in by_grade:
    by_grade[g].sort(key=lambda x: -(meta.get(x[0], {}).get("total_on") or 0))

sections = []
for g in range(5, 0, -1):
    items = by_grade[g]
    if not items:
        continue
    if g >= 3:
        cards = "".join(full_card(nm, t, meta.get(nm, {})) for nm, t in items)
    else:
        cards = "".join(compact_card(nm, t, meta.get(nm, {})) for nm, t in items)
    sections.append(f'<h2 style="font-size:16px;margin:16px 0 6px">档{g} {GRADE_NAME[g]}'
                    f'<span style="color:#6b7280;font-size:12px;font-weight:400">（已合成 {len(items)} 个）</span></h2>{cards}')

page = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>F8 模板画廊</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f3f4f6;margin:0;padding:22px;max-width:940px;margin:0 auto">
<h1 style="font-size:20px">F8 角色模板画廊 <span style="font-size:13px;color:#6b7280;font-weight:400">凡人修仙传｜档5~3 全字段｜档2~1 紧凑｜误命中候选未建</span></h1>
<p style="color:#6b7280;font-size:12px">traits=±10 刻度 12 维（D2 冻结）｜样本指针不存原文（合规）｜core_conflict/contrast 待 P2 案例库｜生成于 {datetime.now():%m-%d %H:%M}</p>
{''.join(sections)}
</body></html>'''
open(OUT, "w", encoding="utf-8").write(page)
print(f"-> {OUT}（{sum(len(v) for v in by_grade.values())} 个模板）")
