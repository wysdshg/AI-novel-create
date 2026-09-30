# -*- coding: utf-8 -*-
"""文风指标（C4）：语气词密度 / 连串重复 / 段均字数 —— 按项目与时间分组统计。

背景（docs/08-C4）：AI 正文「语气词密度 2.1 vs 真实 9.0」，但样本仅 37 章且混了 v1~v6
多版 prompt → 要算**同版本均值**必须先按来源分组。本脚本就是那个分组统计器：

- `--project 我超原神`：统计该项目全部章节（同一时期的产物 = 同一 prompt 版本）；
- `--book-dir 小说/斗破苍穹 --sample 20`：统计源书抽样若干章，得到「真人基线」；
- 输出每章明细 + 均值/中位，便于回答「v6 到底把语气词提上来了没有」。

指标定义（可对齐历史口径）：
- **语气词密度**：`啊呀呢吧吗嘛哦唉哼嘿咦诶唔嗯哈呵哟喔啦呐` 的出现次数 / 每千字；
- **连串**：章内出现 ≥3 次的 4 字片段个数（复读机倾向，越低越好）；
- **段均字数**：按换行分段后的平均段长（v6 目标 ~26）。
"""
import argparse
import collections
import os
import re
import sqlite3
import sys
import time

DB = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"
TONE = "啊呀呢吧吗嘛哦唉哼嘿咦诶唔嗯哈呵哟喔啦呐"


def metrics(text: str) -> dict:
    text = text or ""
    n = len(text)
    if n == 0:
        return {"chars": 0, "tone_per1k": 0.0, "runs": 0, "para_avg": 0.0}
    tone = sum(text.count(ch) for ch in TONE)
    grams = collections.Counter(text[i:i + 4] for i in range(max(0, n - 3)))
    runs = sum(1 for g, c in grams.items() if c >= 3)
    paras = [p for p in re.split(r"\n+", text) if p.strip()]
    para_avg = round(sum(len(p) for p in paras) / len(paras), 1) if paras else 0.0
    return {"chars": n, "tone_per1k": round(tone / n * 1000, 2), "runs": runs,
            "para_avg": para_avg}


def by_project(name: str):
    con = sqlite3.connect(DB, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT chapter_no, title, content, word_count, created_at FROM chapters "
        "WHERE project_id=(SELECT id FROM projects WHERE name=?) ORDER BY chapter_no",
        (name,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def by_book_dir(path: str, sample: int):
    files = sorted(f for f in os.listdir(path) if f.endswith((".txt", ".md")))
    step = max(1, len(files) // sample)
    out = []
    for f in files[::step][:sample]:
        p = os.path.join(path, f)
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        out.append({"chapter_no": f, "title": f, "content": txt, "word_count": len(txt),
                    "created_at": ""})
    return out


def report(rows: list[dict], label: str):
    if not rows:
        print(f"{label}: 无样本")
        return
    ms = [metrics(r["content"]) for r in rows]
    print(f"\n===== {label}（{len(rows)} 章）=====")
    print(f"{'章':>14} | {'字数':>6} | {'语气词/千字':>10} | {'连串':>5} | {'段均':>6}")
    for r, m in zip(rows, ms):
        lab = str(r["chapter_no"])[-14:]
        print(f"{lab:>14} | {m['chars']:>6} | {m['tone_per1k']:>10} | "
              f"{m['runs']:>5} | {m['para_avg']:>6}")
    def avg(k):
        vals = [m[k] for m in ms]
        return round(sum(vals) / len(vals), 2)
    print(f"{'均值':>14} | {avg('chars'):>6} | {avg('tone_per1k'):>10} | "
          f"{avg('runs'):>5} | {avg('para_avg'):>6}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="项目名（统计其全部章节）")
    ap.add_argument("--book-dir", help="源书目录（抽样算真人基线）")
    ap.add_argument("--sample", type=int, default=20, help="源书抽样章数")
    a = ap.parse_args()
    if not a.project and not a.book_dir:
        ap.error("至少给 --project 或 --book-dir")
    if a.project:
        report(by_project(a.project), f"项目《{a.project}》")
    if a.book_dir:
        report(by_book_dir(a.book_dir, a.sample), f"源书 {os.path.basename(a.book_dir)}（抽样 {a.sample}）")
