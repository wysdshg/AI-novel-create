# -*- coding: utf-8 -*-
"""[DEV-DATA01b] 已入库概括（chapter_summaries）污染抽验 —— 只读，一行不写。

背景：寒门 2137 章概括是旧管线从**未清洗**素材产的，素材里混着整段他书正文（见
`outputs/data01b/dry-run报告.md`）。本脚本把「素材层缺陷章集合」与库里的概括行对撞，
按三档如实分类，产出受影响章号清单 + 污染证据 + 处置建议；**清理与改写另行批准**（涉及库写）。

    用法: .venv/Scripts/python.exe backend/scripts/audit_summary_contamination.py
    输入: outputs/data01b/决策明细.json、缺陷登记.json（先跑 --dry-run 生成）
    输出: outputs/data01b/概括污染抽验报告.md、outputs/data01b/_verify/概括污染.json

库以 `mode=ro` 打开（打不开就报错，绝不退化成可写连接）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chapter_noise_patterns import INJECTED_BLOCKS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs" / "data01b"
VERIFY = OUT_DIR / "_verify"
DECISIONS = OUT_DIR / "决策明细.json"
LEDGER = OUT_DIR / "清洗台账_二批.json"
REGISTRY = OUT_DIR / "缺陷登记.json"
REPORT = OUT_DIR / "概括污染抽验报告.md"
DB_PATH = Path.home() / ".ai_novel" / "data" / "novel_agent.db"

BOOKS = ["寒门枭士", "斗破苍穹"]
SUMMARY_FIELDS = ("summary", "segment_summary", "arc_summary",
                  "summary_raw", "segment_summary_raw", "arc_summary_raw")

# 每本「他书/非正文」指纹词：素材层注入块（继承 chapter_noise_patterns）+ 已亲验的续写自述
def audit_markers(book: str) -> list[str]:
    base = [m for blk in INJECTED_BLOCKS.get(book, []) for m in blk["markers"]]
    extra = {
        "斗破苍穹": ["玄幻界", "天门界", "无心界", "界主", "神帝", "仙帝", "灵源",
                     "大主宰", "五帝破空", "第一次写小说", "穿越小说吧"],
    }.get(book, [])
    return sorted(set(base + extra), key=len, reverse=True)


def seq_of(file: str) -> int | None:
    m = re.match(r"^(\d+)_", file)
    return int(m.group(1)) if m else None


def load_defect_sets() -> dict[str, dict[str, set[int]]]:
    """素材层缺陷 → 章号集合。

    先读 `清洗台账_二批.json`（apply 后的事实），没有台账才退回 `决策明细.json`（dry-run 计划）。
    """
    out: dict[str, dict[str, set[int]]] = {}
    if LEDGER.exists():
        rows = json.loads(LEDGER.read_text(encoding="utf-8"))
        src = "清洗台账_二批.json（apply 后事实）"
    else:
        rows = json.loads(DECISIONS.read_text(encoding="utf-8")) if DECISIONS.exists() else []
        src = "决策明细.json（dry-run 计划）"
    reg = json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.exists() else {}
    for book in BOOKS:
        out[book] = {"whole_foreign": set(), "tail_block": set(), "ad_line": set(),
                     "truncated": set(), "renamed": set(), "非正文_续写感言": set()}
    for d in rows:
        ch = seq_of(Path(d["rel"]).name)
        if ch is None:
            continue
        book = Path(d["rel"]).parent.name
        if book not in out:
            continue
        if d["action"] == "B":
            out[book]["whole_foreign"].add(ch)
            if d.get("rule") == "fan_sequel_nonbody":
                out[book]["非正文_续写感言"].add(ch)
        elif d["action"] == "A":
            key = "ad_line" if d.get("rule") == "ad_line_only" else "tail_block"
            out[book][key].add(ch)
        elif d["action"] == "R":
            out[book]["renamed"].add(ch)
    for book, r in reg.items():
        if book in out:
            out[book]["truncated"] = set(r.get("句中截断_章号", []))
    print("缺陷集合来源：", src)
    return out


def snippet(text: str, markers: list[str], limit: int = 50) -> str:
    """取第一条含指纹词的分句作为证据（引用 ≤50 字）。"""
    for part in re.split(r"[。！？\n]", text or ""):
        if any(m in part for m in markers):
            return part.strip()[:limit]
    return (text or "").strip()[:limit]


def main() -> int:
    if not DB_PATH.exists():
        print(f"🔴 库文件不存在：{DB_PATH}")
        return 2
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    defects = load_defect_sets()

    payload: dict = {"生成UTC": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "库连接": "mode=ro（只读，未写任何一行）", "书": {}}
    for book in BOOKS:
        markers = audit_markers(book)
        rx = re.compile("|".join(re.escape(m) for m in markers))
        rows = [dict(r) for r in con.execute(
            f"select id, chapter_no, title, {', '.join(SUMMARY_FIELDS)} from chapter_summaries "
            f"where book_name = ? order by chapter_no", (book,))]
        d = defects[book]
        contaminated, suspect, clean = [], [], []
        field_hits: Counter[str] = Counter()
        for r in rows:
            matched = {}
            for f in SUMMARY_FIELDS:
                v = r.get(f) or ""
                if v and rx.search(v):
                    matched[f] = v
                    field_hits[f] += 1
            ch = r["chapter_no"]
            in_material = {
                "整章他书/非正文": ch in d["whole_foreign"],
                "章末注入块": ch in d["tail_block"],
                "仅广告行": ch in d["ad_line"],
                "句中截断": ch in d["truncated"],
            }
            rec = {"id": r["id"], "chapter": ch, "title": (r["title"] or "")[:36],
                   "fields": sorted(matched), "evidence": {f: snippet(v, markers)
                                                            for f, v in matched.items()},
                   "material": {k: v for k, v in in_material.items() if v}}
            if matched:
                contaminated.append(rec)
            elif any(in_material.values()):
                suspect.append(rec)
            else:
                clean.append(ch)
        payload["书"][book] = {
            "库行数": len(rows),
            "确认污染_行数": len(contaminated),
            "受影响可疑_行数": len(suspect),
            "未见污染_行数": len(clean),
            "按字段命中": dict(field_hits),
            "确认污染": contaminated,
            "受影响可疑": suspect,
            "素材缺陷集合规模": {k: len(v) for k, v in d.items()},
        }
        print(f"[{book}] 库行 {len(rows)}｜确认污染 {len(contaminated)}｜"
              f"受影响可疑 {len(suspect)}｜未见污染 {len(clean)}")

    VERIFY.mkdir(parents=True, exist_ok=True)
    (VERIFY / "概括污染.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    write_report(payload, defects)
    print(f"-> {REPORT}")
    con.close()
    return 0


def write_report(payload: dict, defects: dict) -> None:
    L: list[str] = []
    L.append("# [DEV-DATA01b] 已入库概括污染抽验报告（只报告，不改库）\n")
    L.append(f"- 生成（UTC）：{payload['生成UTC']}｜{payload['库连接']}")
    L.append("- 判据：素材层缺陷章集合（`决策明细.json`/`缺陷登记.json`）× `chapter_summaries` "
             "六个概括字段指纹对撞")
    L.append("- 三档口径：**确认污染**=概括文本里出现他书/续写指纹词；**受影响可疑**=该章素材有缺陷"
             "但概括未检出血缘（可能只概括了正文部分，也可能已把残缺当结局）；**未见污染**=两者都无\n")
    for book, p in payload["书"].items():
        L.append(f"## 一、{book}\n")
        bleed = [r["chapter"] for r in p["确认污染"] if not r["material"]]
        L.append(f"- 库行数 **{p['库行数']}**（`chapter_summaries` 按 book_name 取）")
        L.append(f"- 确认污染 **{p['确认污染_行数']}** 行｜受影响可疑 **{p['受影响可疑_行数']}** 行｜"
                 f"未见污染 {p['未见污染_行数']} 行")
        if bleed:
            L.append(f"- ⚠ 其中 **{len(bleed)}** 行的章本身素材是干净的（{bleed}）→ "
                     "说明旧管线概括会**跨章串扰**：邻章的他书内容/续写内容漏进了本章概括尾部。"
                     "重概括范围不能只圈缺陷章，要按「缺陷章 ± 相邻章」取")
        L.append(f"- 命中的字段分布：{p['按字段命中']}")
        L.append(f"- 素材缺陷规模（本单口径）：{p['素材缺陷集合规模']}\n")
        L.append(f"### 确认污染清单（{p['确认污染_行数']} 行，证据引用 ≤50 字）\n")
        L.append("| 章 | title | 命中字段 | 证据 |")
        L.append("|---|---|---|---|")
        for r in p["确认污染"][:120]:
            ev = "；".join(f"{k}：{v}" for k, v in r["evidence"].items())
            L.append(f"| c{r['chapter']} | {r['title']} | {', '.join(r['fields'])} | {ev[:90]} |")
        if p["确认污染_行数"] > 120:
            L.append(f"| …余 {p['确认污染_行数'] - 120} 行见 `_verify/概括污染.json` | | | |")
        L.append("")
        L.append(f"### 受影响可疑（{p['受影响可疑_行数']} 行，抽样 40）\n")
        L.append("| 章 | 素材缺陷 | title |")
        L.append("|---|---|---|")
        for r in p["受影响可疑"][:40]:
            L.append(f"| c{r['chapter']} | {', '.join(r['material'])} | {r['title']} |")
        L.append("")
    L.append("## 二、处置建议（本单不执行）\n")
    L.append("1. **确认污染行**：改写方向 = 只保留本书正文部分重新概括；缺原文的（整章他书/非正文）"
             "应**删除该行或标注非本书内容**，不是改写。")
    L.append("2. **整章他书/非正文对应行**（寒门 c1372/c1388；斗破 c1658~c1671）：素材已隔离，"
             "库里这些行概括的是他书/同人内容 → 建议删行或置 `book_name` 归属待定，需明确批准。")
    L.append("3. **章末注入块行**（寒门 141 章）：素材已切尾；概括若未含他书词，多为「只概括了正文」"
             "——可不重概括，但**弧层**（`arc_summary`）跨章的可能混入，建议随重灌单一起复核。")
    L.append("4. **句中截断行**（寒门 172 章）：原文尾部缺失无法补全，概括若写成完整结局即属推断，"
             "建议保留但在 `note` 标「素材截断」——需要库写权限，本单未做。")
    L.append("5. 三档清单可直接喂给「下游重灌单」：按 `id` 精确操作，不需重跑全书。")
    REPORT.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
