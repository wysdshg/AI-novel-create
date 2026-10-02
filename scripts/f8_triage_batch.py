# -*- coding: utf-8 -*-
"""
F8 P0 ② Flash-Next 批次分诊脚本（机制文档 §4.3 第一层批内分诊）

输入：密度脚本产出的 JSON（含分批注入清单 batches）+ 章节原文
流程：每批 25 章 → 注入「本批在册角色+档位」清单 → Flash-Next（MyAPI 网关，流式）
      → 核对出场章 / 一句话行为摘要 / 档位建议 / 别称登记 / 新角色补漏
输出：每批一个 batch_NNNN.json（断点续跑：已存在即跳过，--force 重跑）

用法：
  python scripts/f8_triage_batch.py \
    --book-dir "E:/AI小说创作/小说/凡人修仙传" \
    --density outputs/f8_p0/density_fanren.json \
    --out-dir outputs/f8_p0/triage_fanren \
    --batches 1-4
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

GATEWAY_DEFAULT = "http://127.0.0.1:9377/v1"
NUM_PREFIX = re.compile(r"^(\d+)")

# ---------- 网关 ----------

def get_gateway_key():
    """从 app_configs 读 llm.gateway_key（与后端同源）。"""
    db = os.path.expanduser(r"~\.ai_novel\data\novel_agent.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("SELECT value FROM app_configs WHERE key='llm.gateway_key'").fetchone()
    con.close()
    if not row:
        sys.exit("app_configs 缺 llm.gateway_key")
    return json.loads(row[0]) if row[0].strip().startswith('"') else row[0]

def chat_stream(base_url, api_key, model, messages, temperature=0.2, max_tokens=8000,
                timeout=600, retries=5):
    """流式调用 OpenAI 兼容网关。返回 (content, usage, elapsed)。reasoning_content 不进正文。
    429/5xx 退避重试（30/60/120s），其余异常直接抛。"""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model, "messages": messages, "stream": True,
        "temperature": temperature, "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连，防系统代理劫持
    content, usage, t0 = [], None, time.time()
    for attempt in range(retries):
        try:
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
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        # thinking/reasoning 走独立字段，永不并入正文（E10 纪律）
                        if delta.get("content"):
                            content.append(delta["content"])
            return "".join(content), usage, time.time() - t0
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 30 * (2 ** attempt) + random.randint(0, 20)  # 抖动防并发重试扎堆
                print(f"[retry] HTTP {e.code}，{wait}s 后第 {attempt + 2} 次尝试", flush=True)
                time.sleep(wait)
                continue
            raise

def extract_json(text):
    """剥 markdown 围栏 + 截取最外层 JSON 对象。"""
    text = re.sub(r"```(?:json)?", "", text).strip()
    lo, hi = text.find("{"), text.rfind("}")
    if lo < 0 or hi <= lo:
        raise ValueError("输出中无 JSON 对象")
    return json.loads(text[lo:hi + 1])

# ---------- Prompt ----------

SYS_PROMPT = """你是网文角色观测助手。给你一部小说连续若干章的原文，以及一份「在册角色名单」（含密度脚本按出场量预判的档位）。你的任务：

1. 出场核对：逐个核对在册角色在本批哪些章真实在场（有对话、行动或被视角跟随）。只被他人转述提及不算在场。
2. 行为摘要：每个在场角色写一句话行为摘要——做了什么、怎么做，体现行为方式与说话腔调。
3. 档位建议：对密度预判档位给出 同意/上调/下调 及一句理由（剧情重要性、戏份密度、跨批连续性）。
4. 别称登记：本批新出现的称呼（外号/敬称/化名），挂到对应角色名下。
5. 新角色补漏：名单之外、本批有名字且戏份可观的角色，给名字+出场章+一句话描述+预估重要性（low/mid/high）。

输出严格 JSON（utf-8，无 markdown 围栏）：
{"characters":[{"name":"…","present_chapters":[章号…],"grade_agree":true,"grade_suggest":3,"reason":"…","behavior":"…","aliases":["…"]}],
 "new_characters":[{"name":"…","present_chapters":[…],"behavior":"…","importance":"low|mid|high"}]}

纪律：
- present_chapters 用每章开头标注的章号数字。
- 在册名单里本批未出场的角色直接不输出，不要硬凑。
- 行为摘要必须基于本批原文，禁止脑补本批未发生的行为。
- 档位数字 0~5：0 无关配角 / 1 断续配角 / 2 篇章级 / 3 卷级 / 4 小说级 / 5 全书级。"""

def build_user_prompt(batch, texts):
    lines = ["【在册角色（密度脚本预估本批在场，供核对）】"]
    for c in batch["chars"]:
        alias = f"（别称：{'、'.join(c['aliases'])}）" if c["aliases"] else ""
        lines.append(f"- {c['name']}{alias}｜密度预判档{c['grade']}｜全书累计出场{c['total_on']}章")
    if len(batch["chars"]) < 5:
        lines.append("（本批在册角色较少，留意新角色补漏）")
    lines.append("")
    lines.append("【章节原文】")
    for chno, fn, text in texts:
        lines.append(f"==== 第{chno}章 {fn} ====")
        lines.append(text.strip())
    return "\n".join(lines)

# ---------- 单批处理 ----------

def run_batch(batch, args, api_key, chapter_map):
    out_path = os.path.join(args.out_dir, f"batch_{batch['batch']:04d}.json")
    if os.path.exists(out_path) and not args.force:
        try:
            prev = json.load(open(out_path, encoding="utf-8"))
            if not prev.get("error"):  # parse_fail/error 批不算完成，断点重跑
                return batch["batch"], "skip", None, 0.0
        except Exception:  # noqa: BLE001
            return batch["batch"], "skip", None, 0.0
    texts = []
    for chno, fn in chapter_map[batch["idx_range"][0] - 1: batch["idx_range"][1]]:
        with open(os.path.join(args.book_dir, fn), encoding="utf-8", errors="ignore") as f:
            texts.append((chno, fn, f.read()))
    messages = [{"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": build_user_prompt(batch, texts)}]
    try:
        content, usage, elapsed = chat_stream(
            args.gateway, api_key, args.model, messages,
            temperature=args.temperature, max_tokens=args.max_tokens)
    except Exception as e:  # noqa: BLE001 —— 单批网络/网关异常不炸全场，留待断点重跑
        data = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
        data["_meta"] = {
            "batch": batch["batch"], "idx_range": batch["idx_range"], "chapters": batch["chapters"],
            "model": args.model, "elapsed_s": 0.0, "usage": None,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return batch["batch"], "error", data, 0.0
    try:
        data = extract_json(content)
        status = "ok"
    except (ValueError, json.JSONDecodeError) as e:
        data = {"error": str(e)}
        status = "parse_fail"
        with open(out_path.replace(".json", ".raw.txt"), "w", encoding="utf-8") as f:
            f.write(content)
    data["_meta"] = {
        "batch": batch["batch"], "idx_range": batch["idx_range"], "chapters": batch["chapters"],
        "model": args.model, "elapsed_s": round(elapsed, 1),
        "usage": usage, "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return batch["batch"], status, data, elapsed

# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", required=True)
    ap.add_argument("--density", required=True, help="密度脚本 JSON（含 batches）")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--batches", default="", help="批号，如 1-4 或 1,3,5；空=全部")
    ap.add_argument("--gateway", default=GATEWAY_DEFAULT)
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--api-key", default="", help="直连供应商时显式传 key（缺省读 app_configs llm.gateway_key）")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    with open(args.density, encoding="utf-8") as f:
        density = json.load(f)
    batches = density["batches"]
    if args.batches:
        sel = set()
        for part in args.batches.split(","):
            if "-" in part:
                a, b = part.split("-")
                sel.update(range(int(a), int(b) + 1))
            else:
                sel.add(int(part))
        batches = [b for b in batches if b["batch"] in sel]
    if not batches:
        sys.exit("无匹配批次")

    api_key = args.api_key or get_gateway_key()
    # 章号→文件映射（与密度脚本同一自然排序）
    chapter_map = []
    for fn in sorted(os.listdir(args.book_dir)):
        m = NUM_PREFIX.match(fn)
        if fn.lower().endswith(".txt") and m:
            chapter_map.append((int(m.group(1)), fn))

    print(f"批次 {len(batches)} 个｜模型 {args.model}｜并发 {args.concurrency}", flush=True)
    t0 = time.time()
    stats = {"ok": 0, "parse_fail": 0, "skip": 0, "error": 0}
    if args.concurrency <= 1:
        for b in batches:
            no, status, data, el = run_batch(b, args, api_key, chapter_map)
            stats[status] += 1
            n_chr = len(data.get("characters", [])) if isinstance(data, dict) else 0
            n_new = len(data.get("new_characters", [])) if isinstance(data, dict) else 0
            print(f"批{no:3d} {status} {el:6.1f}s 在册{len(b['chars'])}人 输出:{n_chr}+新{n_new}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(run_batch, b, args, api_key, chapter_map): b["batch"] for b in batches}
            for fu in as_completed(futs):
                no, status, data, el = fu.result()
                stats[status] += 1
                print(f"批{no:3d} {status} {el:6.1f}s", flush=True)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    with open(os.path.join(args.out_dir, "_run_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"args": vars(args), "stats": stats}, f, ensure_ascii=False, indent=1)
    print("汇总:", stats)

if __name__ == "__main__":
    main()
