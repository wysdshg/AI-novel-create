# -*- coding: utf-8 -*-
"""
F8 官方模板流水线（P3 预演，不碰数据库）：
  ① 档5~3 全字段模板 → LLM 匿名化重写（去角色名/门派/功法/法宝/地名/界域）
  ② 档2 篇章级 100 人 → LLM 聚类归纳成 8~15 个「篇章级功能模板」→ 同样匿名化
  ③ 专名核账：块名单 grep（花名册姓名/别称 + 凡人专有名词），命中打回重写一次
  ④ 渲染 official_preview.html（核账报告内嵌）
输出：outputs/f8_p0/official/*.json + official_preview.html
"""
import argparse
import glob
import html
import json
import os
import re
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

NUM_PREFIX = re.compile(r"^(\d+)")
TRAIT_LABEL = {"altruism": "利他↔自私", "honor": "信义↔背信", "mercy": "仁慈↔狠辣",
               "resolve": "坚毅↔易摧", "decisiveness": "果决↔犹豫", "discipline": "自律↔放纵",
               "risk": "冒险↔稳健", "rationality": "理性↔冲动", "guile": "城府↔直率",
               "idealism": "理想↔务实", "warmth": "热忱↔冷漠", "dominance": "强势↔随和"}
GRADE_NAME = {5: "全书级/主角", 4: "小说级", 3: "卷级", 2: "篇章级（聚合）"}

# 凡人专有名词块名单（门派/地域/功法/法宝/界域/组织；修为境界=题材通用不在此列）
PROPER_NOUNS = [
    "七玄门", "彩霞山", "炼骨崖", "野狼帮", "越国", "黄枫谷", "掩月宗", "灵兽山",
    "化刀坞", "清虚门", "巨剑门", "天阙堡", "古剑门", "天南", "乱星海", "大晋",
    "慕兰", "灵界", "魔界", "地渊", "掌天瓶", "长春功", "罗烟步", "金光砖",
    "火弹术", "御风诀", "青元剑诀", "大衍诀", "噬金虫", "啼魂", "曲魂", "铁奴",
    "墨蛟", "六翼霜蚣", "落云宗", "掩月宗", "星宫", "鬼灵门", "合欢宗", "古魔",
    "血焰", "元刹", "银月", "玲珑", "雪玲", "珑梦", "韩立", "墨居仁", "厉飞雨",
    "南宫婉", "紫灵", "汪凝", "元瑶", "大衍神君", "蟹道人", "宝花", "青元子",
    "血光", "儒生", "天澜圣兽", "洞天鼠王", "冰凤", "朱果儿", "白果儿", "田琴儿",
    "莫简离", "敖啸", "灵王", "血魄", "冰魄", "向之礼", "魏无涯", "蛮胡子",
    "文思月", "万天明", "凌玉灵", "风希", "温天仁", "梅凝", "慕沛灵", "宋玉",
    "范静梅", "卓如婷", "乌丑", "萧诧", "玄骨", "极阴", "萧翠儿", "辛如音",
    "雷万鹤", "齐云霄", "董萱儿", "陈巧倩", "菡云芝", "王蝉", "李化元", "墨大夫",
    "墨夫人", "墨彩环", "墨玉珠", "墨凤舞", "孙二狗", "张铁", "三叔", "韩铸",
    "厉飞雨", "岳麓", "千竹教", "清虚", "太南谷", "太南小会", "坠魔谷", "昆吾山",
    "虚天殿", "小极宫", "元武国", "车骑恭", "呼老魔", "金青", "御灵宗", "妖族",
]

def get_gateway_key():
    db = os.path.expanduser(r"~\.ai_novel\data\novel_agent.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("SELECT value FROM app_configs WHERE key='llm.gateway_key'").fetchone()
    con.close()
    return json.loads(row[0]) if row[0].strip().startswith('"') else row[0]

def chat_stream(base_url, api_key, model, messages, temperature=0.2, max_tokens=8000, timeout=600):
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

# ---------- 匿名化 ----------

ANON_PROMPT = """你是模板脱敏器。输入一份角色模板 JSON（含 desc/voice/behavior_patterns/ranks 等）。任务：去掉一切小说专名，输出同构 JSON。

规则：
- 人名（含别称/称呼）→ 换成角色功能指代：「主角」「此人」「该修士」「同门」「长辈」等，禁止保留任何具体姓名。
- 门派/宗族/地名/界域/功法/法宝/丹药/妖兽等专名 → 换通用词：宗门、坊市、上界、下界、功法、法宝、丹药、妖兽等。
- 修为境界（炼气/筑基/结丹/元婴等）是题材通用词，保留。
- 行为与功能内容一字不许丢：脱敏只换指代，不许删条目、不许改行为逻辑。
- 全部字段与键名保持不变，输出严格 JSON（无围栏）。"""

def anonymize(tpl, name, api_key, args, blocklist):
    """重写 → grep 核账；命中重试一次；再命中打回（标记）。"""
    messages = [{"role": "system", "content": ANON_PROMPT},
                {"role": "user", "content": f"角色名（要去掉的）：{name}\n模板 JSON：\n"
                                            + json.dumps(tpl, ensure_ascii=False)}]
    for attempt in (1, 2):
        content, el = chat_stream(args.gateway, api_key, args.model, messages,
                                  max_tokens=8000)
        try:
            out = extract_json(content)
        except (ValueError, json.JSONDecodeError) as e:
            if attempt == 2:
                return tpl, [f"JSON 解析失败 {e}"]
            continue
        blob = json.dumps(out, ensure_ascii=False)
        hits = sorted({w for w in blocklist if w in blob})
        if not hits:
            return out, []
        if attempt == 2:
            return out, [f"专名残留: {hits[:8]}"]
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
                         f"核账命中这些专名：{hits[:15]}。逐个替换成通用表述后再输出完整 JSON。"})
    return tpl, ["未执行"]

# ---------- 档2 聚类 ----------

CLUSTER_PROMPT = """你是功能模板归纳器。输入一批「篇章级配角」的紧凑画像（已去名）。把它们聚类归纳成 8~12 个可复用的「篇章级功能模板」——同一模板 = 在不同书里都能成立的功能位（如：执事长老型、同门挚友型、一敌型引导者…），不是具体某个人。

每个模板输出：
{"slot":"功能位名（自拟，≤6字）","desc":"两三句功能定位","mode":"助力|阻碍|亦师亦敌|…",
 "ranks":"常见位阶范围","traits":{12维整数，-10~10，只准 0/±1/±4/±7/±10，取成员共性},
 "voice":{"记忆点":"从成员记忆点归纳的共性1~2个","语域":"…","攻防模式":"…","注意力偏向":"…","幽默类型":"…","sample_refs":[]},
 "behavior_patterns":["合并去重后 3~5 条，其中一条是立场演变模式"],
 "relation_patterns":[{"target":"上位者|同伴|敌人|亲人|陌生人","pattern":"…"}],
 "member_count":成员数}

纪律：输出严格 JSON {"templates":[...]}；模板之间功能边界清晰不重叠；禁止出现任何具体人名/书名/门派名；member_count 填归入该模板的成员个数。"""

def cluster_grade2(rows2, api_key, args, blocklist, out_dir):
    profiles = []
    for r in rows2:
        t = r["tpl"]
        top = sorted(t.get("traits", {}).items(), key=lambda x: -abs(x[1]))[:4]
        profiles.append(json.dumps({
            "slot": t.get("slot"), "mode": t.get("mode"), "desc": t.get("desc"),
            "ranks": t.get("ranks"),
            "traits_top": {TRAIT_LABEL.get(k, k): v for k, v in top},
            "patterns": (t.get("behavior_patterns") or [])[:3],
            "记忆点": t.get("voice", {}).get("记忆点", ""),
        }, ensure_ascii=False))
    half = (len(profiles) + 1) // 2
    all_t = []
    for part in (profiles[:half], profiles[half:]):
        content, _ = chat_stream(args.gateway, api_key, args.model,
                                 [{"role": "system", "content": CLUSTER_PROMPT},
                                  {"role": "user", "content": "\n".join(part)}], max_tokens=12000)
        data = extract_json(content)
        all_t += data.get("templates", [])
    # 二次归并
    content, _ = chat_stream(args.gateway, api_key, args.model,
                             [{"role": "system", "content": CLUSTER_PROMPT +
                               "\n\n这是两组已归纳的模板，请合并去重成最终 8~15 个，member_count 相加。"},
                              {"role": "user", "content": json.dumps(all_t, ensure_ascii=False)}],
                             max_tokens=12000)
    merged = extract_json(content).get("templates", [])
    out = []
    for i, t in enumerate(merged, 1):
        t.setdefault("slot", f"篇章级功能位{i}")
        t["slot_kind"] = "person"
        t2, issues = anonymize(t, t.get("slot", ""), api_key, args, blocklist)
        t2["_meta"] = {"source": "grade2_cluster", "member_count": t.get("member_count", 0),
                       "issues": issues}
        with open(os.path.join(out_dir, f"agg_{i:02d}.json"), "w", encoding="utf-8") as f:
            json.dump(t2, f, ensure_ascii=False, indent=1)
        out.append((f"聚合{i:02d}", t2, issues))
    return out

# ---------- 渲染 ----------

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

def card(title, t, issues, badge_label, badge_color):
    traits = t.get("traits", {})
    bars = "".join(trait_bar(k, traits.get(k, 0)) for k in TRAIT_LABEL if traits.get(k))
    basis = t.get("trait_basis", {})
    basis_html = "".join(f'<li><b>{TRAIT_LABEL.get(k, k)}</b>：{html.escape(str(v))}</li>' for k, v in basis.items())
    v = t.get("voice", {})
    pats = "".join(f'<li>{html.escape(str(p))}</li>' for p in t.get("behavior_patterns", []))
    rels = ""
    for h in t.get("relation_patterns", []):
        if isinstance(h, dict):
            rels += f'<li>{html.escape(str(h.get("target","")))}：{html.escape(str(h.get("pattern","")))}</li>'
        else:
            rels += f'<li>{html.escape(str(h))}</li>'
    iss = (f'<div style="background:#fee2e2;border-radius:6px;padding:4px 10px;font-size:12px;color:#b91c1c;'
           f'margin-top:6px">⚠ {html.escape("；".join(issues))}</div>') if issues else \
          ('<div style="color:#15803d;font-size:11px;margin-top:6px">✓ 专名核账 0 命中</div>')
    return f'''<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;margin:10px 0">
<div style="display:flex;align-items:center;gap:8px"><span style="font-size:16px;font-weight:700">{html.escape(title)}</span>
<span style="background:{badge_color};color:#fff;border-radius:999px;padding:1px 9px;font-size:11px">{badge_label}</span>
<span style="background:#dbeafe;color:#1d4ed8;border-radius:999px;padding:1px 9px;font-size:11px">{html.escape(str(t.get("slot","?")))}</span>
<span style="background:#dcfce7;color:#15803d;border-radius:999px;padding:1px 9px;font-size:11px">{html.escape(str(t.get("mode","?")))}</span>
{f'<span style="color:#6b7280;font-size:11px">成员{t.get("member_count","")}人</span>' if t.get("member_count") else ''}</div>
<p style="color:#374151;margin:8px 0 2px;font-size:13px">{html.escape(str(t.get("desc","")))}</p>
<p style="color:#6b7280;font-size:12px;margin:2px 0"><b>位阶：</b>{html.escape(str(t.get("ranks","")))}</p>
<div style="margin:6px 0">{bars}</div>
{"<ul style='color:#374151;font-size:12px;margin:4px 0'>" + basis_html + "</ul>" if basis_html else ""}
<div style="background:#f9fafb;border-radius:8px;padding:8px 12px;margin:8px 0;font-size:12px;color:#374151">
<b>记忆点：</b>{html.escape(str(v.get("记忆点","")))}<br><b>语域：</b>{html.escape(str(v.get("语域","")))}｜<b>攻防：</b>{html.escape(str(v.get("攻防模式","")))}</div>
<div style="display:flex;gap:14px"><div style="flex:1.2"><b style="font-size:12px">行为模式</b><ul style="font-size:12px;color:#374151;margin:3px 0;padding-left:16px">{pats}</ul></div>
<div style="flex:1"><b style="font-size:12px">关系模式</b><ul style="font-size:12px;color:#374151;margin:3px 0;padding-left:16px">{rels}</ul></div></div>{iss}</div>'''

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tpl-dir", default=r"E:\AI小说创作\outputs\f8_p0\templates")
    ap.add_argument("--aggregate", default=r"E:\AI小说创作\outputs\f8_p0\aggregate_final\aggregate.json")
    ap.add_argument("--out-dir", default=r"E:\AI小说创作\outputs\f8_p0\official")
    ap.add_argument("--gateway", default="http://127.0.0.1:9377/v1")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--render-only", action="store_true", help="跳过 LLM，从已落盘 JSON 直接渲染")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    agg = json.load(open(args.aggregate, encoding="utf-8"))
    meta = {r["name"]: r for r in agg["rows"]}

    if args.render_only:
        results, clusters = [], []
        for p in sorted(glob.glob(os.path.join(args.out_dir, "anon_*.json"))):
            t = json.load(open(p, encoding="utf-8"))
            m = t.pop("_meta", {})
            results.append((m.get("source_name", os.path.basename(p)), m.get("grade"), t,
                            m.get("issues", [])))
        for p in sorted(glob.glob(os.path.join(args.out_dir, "agg_*.json"))):
            t = json.load(open(p, encoding="utf-8"))
            m = t.pop("_meta", {})
            clusters.append((t.get("slot") or os.path.basename(p), t, m.get("issues", [])))
            results.append((t.get("slot") or os.path.basename(p), 2, t, m.get("issues", [])))
        _render(results, clusters, args.out_dir, meta)
        return

    api_key = get_gateway_key()

    agg = json.load(open(args.aggregate, encoding="utf-8"))
    meta = {r["name"]: r for r in agg["rows"]}
    # 块名单 = 花名册姓名+别称 + 专有名词
    blocklist = set(PROPER_NOUNS)
    for r in agg["rows"]:
        blocklist.add(r["name"])
        blocklist.update(r.get("aliases") or [])
    blocklist = {w for w in blocklist if 1 < len(w) <= 8}

    # ① 档5~3 匿名化
    jobs = []
    for p in glob.glob(os.path.join(args.tpl_dir, "tpl_*.json")):
        t = json.load(open(p, encoding="utf-8"))
        nm = os.path.basename(p)[4:-5]
        g = t.get("grade") or meta.get(nm, {}).get("final_grade")
        if g in (3, 4, 5):
            jobs.append((nm, t, g))
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(anonymize, t, nm, api_key, args, blocklist): (nm, t, g)
                for nm, t, g in jobs}
        for fu in as_completed(futs):
            nm, t, g = futs[fu]
            out, issues = fu.result()
            out.setdefault("slot_kind", "person")
            out["_meta"] = {"source_name": nm, "grade": g, "issues": issues}
            with open(os.path.join(args.out_dir, f"anon_{g}_{nm}.json"), "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            results.append((nm, g, out, issues))
            print(f"匿名化 {nm}（档{g}）{'⚠ ' + str(issues) if issues else '✓'}", flush=True)

    # ② 档2 聚合 + 匿名化
    rows2 = []
    for p in glob.glob(os.path.join(args.tpl_dir, "tpl_*.json")):
        t = json.load(open(p, encoding="utf-8"))
        nm = os.path.basename(p)[4:-5]
        g = t.get("grade") or meta.get(nm, {}).get("final_grade")
        if g == 2:
            rows2.append({"name": nm, "tpl": t})
    print(f"档2 聚合：{len(rows2)} 人", flush=True)
    clusters = cluster_grade2(rows2, api_key, args, blocklist, args.out_dir)
    for nm, t, issues in clusters:
        results.append((nm, 2, t, issues))
        print(f"{nm} ✓" if not issues else f"{nm} ⚠ {issues}", flush=True)

    # ③ 渲染
    _render(results, clusters, args.out_dir, meta)

def _render(results, clusters, out_dir, meta):
    by_g = {g: [] for g in (5, 4, 3, 2)}
    for nm, g, t, issues in results:
        by_g[g].append((nm, t, issues))
    sections = []
    for g in (5, 4, 3, 2):
        items = by_g[g]
        if not items:
            continue
        cards = "".join(card(nm if g != 2 else t.get("slot", nm), t, issues,
                             GRADE_NAME[g], "#7c3aed" if g == 2 else "#1d4ed8")
                        for nm, t, issues in items)
        n_issue = sum(1 for _, _, iss in items if iss)
        sections.append(f'<h2 style="font-size:16px;margin:16px 0 6px">{GRADE_NAME[g]}'
                        f'<span style="color:#6b7280;font-size:12px;font-weight:400">'
                        f'（{len(items)} 个｜核账通过 {len(items) - n_issue}｜待处理 {n_issue}）</span></h2>{cards}')
    n_all = len(results)
    n_ok = sum(1 for _, _, _, iss in results if not iss)
    page = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>F8 官方模板预览（已脱敏）</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f3f4f6;margin:0;padding:22px;max-width:940px;margin:0 auto">
<h1 style="font-size:20px">F8 官方模板预览 <span style="font-size:13px;color:#6b7280;font-weight:400">已去角色名/小说专有名｜此页拍板后才写库</span></h1>
<div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;padding:8px 14px;font-size:13px;color:#065f46;margin:8px 0">
共 {n_all} 个模板（档5~3 原样脱敏 {n_all - len(clusters)} + 档2 聚合 {len(clusters)}）｜专名核账 <b>{n_ok}/{n_all} 零命中</b></div>
{''.join(sections)}
<p style="color:#9ca3af;font-size:12px">生成于 {datetime.now():%m-%d %H:%M}｜档1 按设计不新建（匹配已有模板）｜core_conflict/contrast 待 P2 案例库</p>
</body></html>'''
    open(os.path.join(out_dir, "official_preview.html"), "w", encoding="utf-8").write(page)
    print("->", os.path.join(out_dir, "official_preview.html"))

if __name__ == "__main__":
    main()
