# -*- coding: utf-8 -*-
"""
F8 模板合成（按档分批，自上而下）：聚合观测材料 → Flash-Next 批量合成单书模板草稿。

输入：aggregate_final/aggregate.json（每角色行为摘要/别称/密度）+ density_merged.json（在场区间，抽台词）
输出：outputs/f8_p0/templates/tpl_<角色>.json（断点续跑：存在即跳过）

分批大小按档位配置（素材越厚批越小）：
  档5:1  档4:3  档3:4  档2:6  档1:10
跳过：档0、QA 误命中候选（待用户否决）、零行为摘要且出场<5 的角色（无材料可合成）。
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

NUM_PREFIX = re.compile(r"^(\d+)")

def get_gateway_key():
    db = os.path.expanduser(r"~\.ai_novel\data\novel_agent.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("SELECT value FROM app_configs WHERE key='llm.gateway_key'").fetchone()
    con.close()
    return json.loads(row[0]) if row[0].strip().startswith('"') else row[0]

def chat_stream(base_url, api_key, model, messages, temperature=0.2, max_tokens=16000, timeout=900):
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

SYS_PROMPT = """你是角色模板提取器。下面给出若干角色（用 ===== 分隔）及各自观测材料。对每个角色各产出一份模板，输出严格 JSON（无围栏）：
{"templates":[{"name":"角色名","slot":"主角位|核心配角位|功能配角位","desc":"两三句功能定位","mode":"助力|阻碍|亦师亦敌|…","ranks":"位阶/实力（材料没提写「未观测」）","traits":{12维},"trait_basis":{"维度":"一句话真书依据，最多6个"},"voice":{"语域":"…","幽默类型":"…","攻防模式":"…","注意力偏向":"…","记忆点":"1~2个口头禅或标志小动作","sample_refs":[{"chapter":章号,"scene":"2~3句场景概括"}]},"behavior_patterns":["行为模式，其中一条须是立场演变模式"],"relation_patterns":[{"target":"上位者|同伴|敌人|亲人|陌生人","pattern":"态度模式"}]}]}

12维Traits（-10~+10 双极）：altruism利他↔自私、honor信义↔背信、mercy仁慈↔狠辣、resolve坚毅↔易摧、decisiveness果决↔犹豫、discipline自律↔放纵、risk冒险↔稳健、rationality理性↔冲动、guile城府↔直率、idealism理想↔务实、warmth热忱↔冷漠、dominance强势↔随和。
只准用 0、±1、±4、±7、±10；±7/±10 必须给 trait_basis 真书依据。

纪律：
- 全部依据观测材料，禁止编造；材料薄的低档角色模板从简（traits 给最显著的 2~4 个维度其余 0，patterns 2~3 条，没材料就写「未观测」）。
- 台词参考只用于提炼腔调与记忆点，输出禁止整句照抄原文（只留章号指针与模式描述）。
- relation_patterns 对类不对人，禁止绑定具体人名。
- templates 数组必须覆盖输入里的每一个角色，name 原样照抄。"""

def build_user_prompt(batch_rows, dmap, chapter_map, book_dir, quote_grades=(4, 5)):
    parts = []
    for r in batch_rows:
        nm = r["name"]
        d = dmap.get(nm, {})
        L = [f"===== 角色：{nm}（密度预判档{r['final_grade']}）====="]
        L.append(f"【密度】出场{r['total_on']}章/覆盖率{r['coverage']:.1%}，有效段 "
                 + "、".join(f"{s['start']}~{s['end']}" for s in (r.get("eff_segs") or [])[:8]))
        if r.get("aliases"):
            L.append(f"【别称】{'、'.join(r['aliases'][:8])}")
        L.append("【行为摘要（分批观测，按时间序）】")
        bhs = r.get("behaviors") or []
        L += [f"- {b['behavior']}" for b in bhs] or ["- （无分诊摘要，仅有密度命中）"]
        # 台词参考：仅高档角色配 1~2 条（控制输入体积）
        if r["final_grade"] in quote_grades:
            runs = d.get("presence_runs", [])
            names = [nm] + list(r.get("aliases") or [])
            quotes = []
            for a, b in runs:
                chno = (a + b) // 2
                fn = next((f for c, f in chapter_map if c == chno), None)
                if not fn:
                    continue
                text = open(os.path.join(book_dir, fn), encoding="utf-8", errors="ignore").read()
                for para in text.split("\n"):
                    para = para.strip()
                    if len(para) >= 10 and ("“" in para or "「" in para or '"' in para) \
                            and any(x in para for x in names):
                        quotes.append(f"第{chno}章: {para[:110]}")
                        break
                if len(quotes) >= 2:
                    break
            if quotes:
                L.append("【台词参考（仅供提炼腔调，禁止照抄）】")
                L += [f"- {q}" for q in quotes]
        parts.append("\n".join(L))
    return "\n\n".join(parts)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aggregate", default=r"E:\AI小说创作\outputs\f8_p0\aggregate_final\aggregate.json")
    ap.add_argument("--density", default=r"E:\AI小说创作\outputs\f8_p0\aggregate_final\density_merged.json")
    ap.add_argument("--book-dir", default=r"E:\AI小说创作\小说\凡人修仙传")
    ap.add_argument("--out-dir", default=r"E:\AI小说创作\outputs\f8_p0\templates")
    ap.add_argument("--gateway", default="http://127.0.0.1:9377/v1")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--batch-sizes", default="5:1,4:3,3:4,2:6,1:10")
    ap.add_argument("--grades", default="5,4,3,2,1", help="要跑的档位（自上而下）")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    agg = json.load(open(args.aggregate, encoding="utf-8"))
    dmap = {r["name"]: r for r in json.load(open(args.density, encoding="utf-8"))["results"]}
    rows = agg["rows"]
    skip_qa = {r["name"] for r in rows if r.get("qa")}
    bs_map = {int(k): int(v) for k, v in (p.split(":") for p in args.batch_sizes.split(","))}
    grades = [int(g) for g in args.grades.split(",")]

    chapter_map = []
    for fn in sorted(os.listdir(args.book_dir)):
        m = NUM_PREFIX.match(fn)
        if fn.lower().endswith(".txt") and m:
            chapter_map.append((int(m.group(1)), fn))

    # 组批（自上而下）
    batches, stats = [], {"todo": 0, "skip_qa": 0, "skip_nomat": 0, "done": 0}
    for g in grades:
        pool = [r for r in rows if r["final_grade"] == g]
        size = bs_map.get(g, 6)
        cur = []
        for r in pool:
            if r["name"] in skip_qa:
                stats["skip_qa"] += 1
                continue
            if not (r.get("behaviors") or []) and r["total_on"] < 5:
                stats["skip_nomat"] += 1
                continue
            if not args.force and os.path.exists(os.path.join(args.out_dir, f"tpl_{r['name']}.json")):
                stats["done"] += 1
                continue
            cur.append(r)
            if len(cur) >= size:
                batches.append((g, cur))
                cur = []
        if cur:
            batches.append((g, cur))

    print(f"待跑批次 {len(batches)}｜已完成 {stats['done']}｜跳过误命中 {stats['skip_qa']}｜"
          f"跳过无材料 {stats['skip_nomat']}", flush=True)
    api_key = get_gateway_key()

    def run(batch):
        g, rows_b = batch
        prompt = build_user_prompt(rows_b, dmap, chapter_map, args.book_dir)
        content, el = chat_stream(args.gateway, api_key, args.model,
                                  [{"role": "system", "content": SYS_PROMPT},
                                   {"role": "user", "content": prompt}],
                                  max_tokens=args.max_tokens)
        try:
            data = extract_json(content)
        except (ValueError, json.JSONDecodeError) as e:
            raw = os.path.join(args.out_dir, f"fail_g{g}_{int(time.time())}.raw.txt")
            open(raw, "w", encoding="utf-8").write(content)
            return g, [f"解析失败: {e} -> {os.path.basename(raw)}"], el
        got = data.get("templates", [])
        for t in got:
            nm = t.get("name", "").strip()
            if not nm:
                continue
            t["grade"] = g
            t["_meta"] = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                          "model": args.model, "batch_chars": len(rows_b)}
            with open(os.path.join(args.out_dir, f"tpl_{nm}.json"), "w", encoding="utf-8") as f:
                json.dump(t, f, ensure_ascii=False, indent=1)
        want = {r["name"] for r in rows_b}
        miss = want - {t.get("name", "").strip() for t in got}
        return g, [f"ok {len(got)} 个" + (f"，缺 {sorted(miss)}" if miss else "")], el

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(run, b): b for b in batches}
        for i, fu in enumerate(as_completed(futs), 1):
            g, msg, el = fu.result()
            print(f"[{i}/{len(batches)}] 档{g} {msg} {el:.0f}s", flush=True)
    print(f"总耗时 {time.time() - t0:.0f}s", flush=True)

if __name__ == "__main__":
    main()
