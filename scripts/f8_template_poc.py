# -*- coding: utf-8 -*-
"""
F8 P0 预览 POC：给指定角色合成「单书模板草稿」，预览 P1 提取器的最终产物形态。

输入：密度 JSON + 分诊批次 JSON + 原文（抽取台词参考，仅作 few-shot 输入）
输出：每角色一个 JSON + 一个 HTML 预览页（三张模板卡）

纪律：
- 全部字段基于观测材料（分诊行为摘要 + 密度统计 + 真书原句参考），禁止编造。
- 台词原句只作输入，输出只留指针（章号+场景描述）与模式提炼——侵权纪律。
- core_conflict / contrast 固定 null（D4：走案例库归纳，禁止从单书原文硬提）。
- traits 沿用库内 ±10 刻度、9 锚点（0,±1,±4,±7,±10），12 维语义冻结（D2）。
"""
import argparse
import glob
import html
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

NUM_PREFIX = re.compile(r"^(\d+)")

# ---------- 网关（与 f8_triage_batch 同源） ----------

def get_gateway_key():
    db = os.path.expanduser(r"~\.ai_novel\data\novel_agent.db")
    con = sqlite3_connect(f"file:{db}?mode=ro")
    row = con.execute("SELECT value FROM app_configs WHERE key='llm.gateway_key'").fetchone()
    con.close()
    return json.loads(row[0]) if row[0].strip().startswith('"') else row[0]

def sqlite3_connect(uri):
    import sqlite3
    return sqlite3.connect(uri, uri=True)

def chat_stream(base_url, api_key, model, messages, temperature=0.2, max_tokens=6000, timeout=600):
    body = json.dumps({"model": model, "messages": messages, "stream": True,
                       "temperature": temperature, "max_tokens": max_tokens}).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    content, t0 = [], time.time()
    with opener.open(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            for ch in chunk.get("choices") or []:
                if (ch.get("delta") or {}).get("content"):
                    content.append(ch["delta"]["content"])
    return "".join(content), time.time() - t0

def extract_json(text):
    text = re.sub(r"```(?:json)?", "", text).strip()
    lo, hi = text.find("{"), text.rfind("}")
    if lo < 0 or hi <= lo:
        raise ValueError("无 JSON")
    return json.loads(text[lo:hi + 1])

# ---------- 观测材料收集 ----------

def collect_material(char, density, triage_dir):
    """从密度 JSON 与各批次分诊 JSON 收集该角色的观测材料。"""
    dres = next((r for r in density["results"] if r["name"] == char), None)
    names = {char} | set(dres["aliases"]) if dres else {char}
    behaviors, aliases, chapters_seen = [], set(), set()
    for p in sorted(glob.glob(os.path.join(triage_dir, "batch_*.json"))):
        d = json.load(open(p, encoding="utf-8"))
        meta = d["_meta"]
        for c in d.get("characters", []):
            if c["name"] == char:
                chs = c.get("present_chapters", [])
                chapters_seen.update(chs)
                if chs:
                    behaviors.append((min(chs), max(chs),
                                      f"第{min(chs)}~{max(chs)}章: {c.get('behavior','')}"))
                aliases.update(c.get("aliases", []))
            else:
                for a in c.get("aliases", []):
                    if a in names:
                        pass
        for c in d.get("new_characters", []):
            if c["name"] in names and c["name"] not in {x[0] for x in behaviors}:
                chs = c.get("present_chapters", [])
                if chs:
                    behaviors.append((min(chs), max(chs),
                                      f"第{min(chs)}~{max(chs)}章[补漏]: {c.get('behavior','')}"))
    behaviors.sort()
    return dres, behaviors, sorted(aliases)

def pick_quotes(book_dir, chapter_map, names, max_quotes=3, max_len=120):
    """抽取含角色名+对话引号的段落作台词参考（只作 LLM 输入，不落盘进模板）。
    引号兼容：「」与 “...”（凡人等多数网文源用后者）。"""
    quotes = []
    seen_ch = set()
    for chno, fn in chapter_map:
        if len(quotes) >= max_quotes:
            break
        if chno in seen_ch:
            continue
        text = open(os.path.join(book_dir, fn), encoding="utf-8", errors="ignore").read()
        for para in text.split("\n"):
            para = para.strip()
            if len(para) < 10:
                continue
            if ("「" not in para) and ("“" not in para and '"' not in para):
                continue
            if any(nm in para for nm in names):
                q = para[:max_len]
                quotes.append((chno, q))
                seen_ch.add(chno)
                break
    return quotes

# ---------- 模板合成 ----------

SYS_PROMPT = """你是角色模板提取器。给你某角色在小说连续章节中的观测材料：密度统计、分批行为摘要、别称、台词参考（含章号）。任务：按 F8 字段结构产出该角色的单书模板草稿。

输出严格 JSON（无围栏）：
{
 "slot": "主角位|核心配角位|功能配角位",
 "desc": "两三句功能定位概括",
 "mode": "助力|阻碍|亦师亦敌|…（选最贴的一种）",
 "ranks": "位阶/实力描述（按观测材料写；材料没提就写「未观测」）",
 "traits": {"altruism":0,"honor":0,"mercy":0,"resolve":0,"decisiveness":0,"discipline":0,"risk":0,"rationality":0,"guile":0,"idealism":0,"warmth":0,"dominance":0},
 "trait_basis": {"维度名": "一句话真书依据（最多给6个最显著的维度）"},
 "voice": {"语域": "…", "幽默类型": "…", "攻防模式": "…", "注意力偏向": "…",
           "记忆点": "1~2 个口头禅或标志性小动作（须有真书依据）",
           "sample_refs": [{"chapter": 章号, "scene": "2~3 句场景概括（谁在哪做了什么、场面与结果）"}]},
 "behavior_patterns": ["行为模式，每条一句；其中一条须是「立场演变模式」（对威胁/对利益/对人的态度如何随处境变化）"],
 "relation_patterns": [{"target": "上位者|同伴|敌人|亲人|陌生人", "pattern": "对该类人的典型态度模式"}]
}

12 维Traits刻度（-10~+10，双极）：altruism利他↔自私、honor信义↔背信、mercy仁慈↔狠辣、resolve坚毅↔易摧、decisiveness果决↔犹豫、discipline自律↔放纵、risk冒险↔稳健、rationality理性↔冲动、guile城府↔直率、idealism理想↔务实、warmth热忱↔冷漠、dominance强势↔随和。

纪律：
- 只准用 0、±1、±4、±7、±10；给 ±7/±10 的维度必须在 trait_basis 里给真书依据。
- 全部依据观测材料，禁止编造材料里没有的行为；材料没覆盖的维度写 0 并少话。
- 台词参考只用于提炼腔调与记忆点，输出里禁止整句照抄原文（只留章号指针与模式描述）。
- sample_refs 每条 scene 用你自己的话写 2~3 句场景概括，禁止抄原句。
- relation_patterns 只写「对某类人的态度模式」，禁止绑定具体人名（2026-10-01 口径）。
- 输出没有 arc_stance_curve 字段（已移出模板，2026-10-01 口径）。"""

def build_user_prompt(char, dres, behaviors, aliases, quotes, n_chapters_total):
    L = [f"【角色】{char}" + (f"（别称：{'、'.join(aliases)}）" if aliases else "")]
    if dres:
        L.append(f"【密度统计】全书{n_chapters_total}章，本批窗口出场{dres['total_on']}章，"
                 f"密度预判档{dres['grade']}（{dres['rule']}），有效段 "
                 + "、".join(f"{s['start']}~{s['end']}" for s in dres["eff_segs"][:8]))
    L += ["", "【分批行为摘要（按时间序）】"]
    L += [b[2] for b in behaviors] or ["（无）"]
    if aliases:
        L += ["", f"【别称】{'、'.join(aliases)}"]
    if quotes:
        L += ["", "【台词参考（仅供提炼腔调，输出禁止照抄）】"]
        L += [f"第{ch}章: {q}" for ch, q in quotes]
    return "\n".join(L)

# ---------- HTML 渲染 ----------

TRAIT_LABEL = {"altruism": "利他↔自私", "honor": "信义↔背信", "mercy": "仁慈↔狠辣",
               "resolve": "坚毅↔易摧", "decisiveness": "果决↔犹豫", "discipline": "自律↔放纵",
               "risk": "冒险↔稳健", "rationality": "理性↔冲动", "guile": "城府↔直率",
               "idealism": "理想↔务实", "warmth": "热忱↔冷漠", "dominance": "强势↔随和"}

def trait_bar(name, val):
    pct = abs(val) / 10 * 50
    left = val < 0
    bar = f'<div style="flex:1;background:#e5e7eb;border-radius:3px;height:14px;position:relative">'
    if val != 0:
        style = f"left:50%;width:{pct}%" if not left else f"left:{50 - pct}%;width:{pct}%"
        color = "#3b82f6" if not left else "#f59e0b"
        bar += f'<div style="position:absolute;top:0;bottom:0;{style};background:{color};border-radius:3px"></div>'
    bar += '<div style="position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#9ca3af"></div></div>'
    return (f'<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px">'
            f'<span style="width:110px;color:#374151">{TRAIT_LABEL[name]}</span>{bar}'
            f'<span style="width:34px;text-align:right;font-weight:600;color:#111827">{val:+d}</span></div>')

def render_html(all_templates, out_path):
    cards = []
    for t in all_templates:
        name = t["char"]
        d = t["template"]
        traits = d.get("traits", {})
        basis = d.get("trait_basis", {})
        bars = "".join(trait_bar(k, traits.get(k, 0)) for k in TRAIT_LABEL)
        basis_html = "".join(f'<li><b>{TRAIT_LABEL.get(k, k)}</b>：{html.escape(v)}</li>'
                             for k, v in basis.items())
        voice = d.get("voice", {})
        refs = "".join(f'<span style="background:#f3f4f6;border-radius:4px;padding:1px 6px;'
                       f'margin-right:6px;font-size:11px">第{r.get("chapter")}章 {html.escape(r.get("scene",""))}</span>'
                       for r in voice.get("sample_refs", []))
        hooks = "".join(f'<li>{html.escape(h.get("target",""))}：{html.escape(h.get("pattern",""))}</li>'
                        for h in d.get("relation_patterns", d.get("relation_hooks", [])))
        pats = "".join(f'<li>{html.escape(p)}</li>' for p in d.get("behavior_patterns", []))
        cards.append(f'''
<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 22px;margin:14px 0">
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:19px;font-weight:700">{html.escape(name)}</span>
    <span style="background:#dbeafe;color:#1d4ed8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600">{html.escape(d.get("slot","?"))}</span>
    <span style="background:#dcfce7;color:#15803d;border-radius:999px;padding:2px 10px;font-size:12px">{html.escape(d.get("mode","?"))}</span>
    <span style="color:#6b7280;font-size:12px">密度档{t.get("grade")}</span>
  </div>
  <p style="color:#374151;margin:10px 0 4px">{html.escape(d.get("desc",""))}</p>
  <p style="color:#6b7280;font-size:13px;margin:2px 0"><b>位阶：</b>{html.escape(str(d.get("ranks","")))}</p>
  <div style="margin:10px 0">{bars}</div>
  {"<ul style='color:#374151;font-size:13px;margin:4px 0'>" + basis_html + "</ul>" if basis_html else ""}
  <div style="background:#f9fafb;border-radius:8px;padding:10px 14px;margin:10px 0;font-size:13px;color:#374151">
    <b>voice</b>｜语域：{html.escape(str(voice.get("语域","")))}｜幽默：{html.escape(str(voice.get("幽默类型","")))}｜攻防：{html.escape(str(voice.get("攻防模式","")))}｜注意力：{html.escape(str(voice.get("注意力偏向","")))}<br>
    <b>记忆点：</b>{html.escape(str(voice.get("记忆点","")))}<br>
    <b>样本指针：</b>{refs}
  </div>
  <div style="display:flex;gap:18px">
    <div style="flex:1.2"><b style="font-size:13px">行为模式（含立场演变）</b><ul style="font-size:13px;color:#374151;margin:4px 0;padding-left:18px">{pats}</ul></div>
    <div style="flex:1"><b style="font-size:13px">关系模式（对类不对人）</b><ul style="font-size:13px;color:#374151;margin:4px 0;padding-left:18px">{hooks}</ul></div>
  </div>
  <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:8px 12px;font-size:12px;color:#92400e;margin-top:10px">
    core_conflict / contrast 按设计（D4）留空：这两项走「案例库两级归纳」，禁止从单书原文硬提，P2 阶段填充。
  </div>
</div>''')
    page = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>F8 模板预览（P0 POC）</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f3f4f6;margin:0;padding:24px;max-width:980px;margin:0 auto">
<h1 style="font-size:22px">F8 角色模板预览 <span style="font-size:13px;color:#6b7280;font-weight:400">P0 POC｜观测窗口：凡人修仙传 第2~101章｜产物为草稿，非入库数据</span></h1>
<p style="color:#6b7280;font-size:13px">样本指针=章号+场景概括（合规：不存原文片段）｜traits=库内 ±10 刻度 12 维（D2 冻结）｜生成于 {datetime.now():%m-%d %H:%M}</p>
{''.join(cards)}
</body></html>'''
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)

# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", required=True)
    ap.add_argument("--density", required=True)
    ap.add_argument("--triage-dir", required=True)
    ap.add_argument("--chars", required=True, help="逗号分隔，须在密度花名册内")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--gateway", default="http://127.0.0.1:9377/v1")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    density = json.load(open(args.density, encoding="utf-8"))
    api_key = get_gateway_key()
    chapter_map = []
    for fn in sorted(os.listdir(args.book_dir)):
        m = NUM_PREFIX.match(fn)
        if fn.lower().endswith(".txt") and m:
            chapter_map.append((int(m.group(1)), fn))

    def work(char):
        dres, behaviors, aliases = collect_material(char, density, args.triage_dir)
        names = {char} | (set(dres["aliases"]) if dres else set())
        quotes = pick_quotes(args.book_dir, chapter_map, names)
        prompt = build_user_prompt(char, dres, behaviors, aliases, quotes, density["n_chapters"])
        content, el = chat_stream(args.gateway, api_key, args.model,
                                  [{"role": "system", "content": SYS_PROMPT},
                                   {"role": "user", "content": prompt}])
        tpl = extract_json(content)
        tpl["_meta"] = {"char": char, "elapsed_s": round(el, 1),
                        "behaviors": len(behaviors), "quotes_input": len(quotes),
                        "finished_at": datetime.now().isoformat(timespec="seconds")}
        with open(os.path.join(args.out_dir, f"tpl_{char}.json"), "w", encoding="utf-8") as f:
            json.dump(tpl, f, ensure_ascii=False, indent=1)
        return char, dres, tpl, el

    results = []
    with ThreadPoolExecutor(max_workers=min(3, len(args.chars.split(",")))) as ex:
        futs = [ex.submit(work, c.strip()) for c in args.chars.split(",") if c.strip()]
        for fu in as_completed(futs):
            char, dres, tpl, el = fu.result()
            results.append({"char": char, "grade": dres["grade"] if dres else None, "template": tpl})
            print(f"{char} ok {el:.0f}s", flush=True)

    results.sort(key=lambda r: -(r["grade"] or 0))
    render_html(results, os.path.join(args.out_dir, "template_preview.html"))
    print("HTML ->", os.path.join(args.out_dir, "template_preview.html"))

if __name__ == "__main__":
    main()
