# -*- coding: utf-8 -*-
"""[INTERN-SK05E] 第一步：确定性提取——按三组关键词扫 8 个 win 文件的原子 summary。

零 AI、零 HTTP，纯 `关键词 in 文本`，可复跑幂等（同输入同输出，产物不含时间戳）。

    .venv\\Scripts\\python.exe backend/scripts/sk05e_extract.py

产物：outputs/sk05e/候选清单.json —— 每条 (原子, 命中族) 一行，含书名、win 文件内
原子序号、命中关键词、原文 summary 全文。原子命中多族时各族都记（任务单 §第一步）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "outputs" / "_atomic_raw"
OUTDIR = ROOT / "outputs" / "sk05e"
OUTFILE = OUTDIR / "候选清单.json"

BOOKS = ["凡人修仙传", "斗破苍穹", "太荒吞天诀", "九星霸体诀", "遮天", "蛊真人", "圣墟", "寒门枭士"]

# 任务单 §背景 给的三组关键词，逐字照抄（唯一提取口径，改动须回 PM）
FAMILIES: dict[str, list[str]] = {
    "匿藏": ["藏匿", "掩藏", "窝藏", "藏进", "藏入", "藏好", "藏于", "藏起", "收起", "匿", "贴身藏"],
    "盘查": ["蒙混", "瞒过", "瞒住", "瞒天", "盘查", "搜查", "查验", "搜身", "检查", "盘问",
             "审问", "关卡", "拦下", "放行", "混出", "混入"],
    "贵人": ["相助", "援手", "搭救", "解围", "贵人", "出手相救", "救下", "出手救", "伸出援手"],
}


def hits(text: str, kws: list[str]) -> list[str]:
    """命中关键词按给出顺序去重返回（确定性）。"""
    seen: list[str] = []
    for kw in kws:
        if kw in text and kw not in seen:
            seen.append(kw)
    return seen


def main() -> int:
    records: list[dict] = []
    missing: list[str] = []
    per_book: dict[str, int] = {}

    for book in BOOKS:
        path = RAW / f"win_{book}.json"
        if not path.exists():
            missing.append(str(path))
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        atoms = data["atoms"]
        per_book[book] = len(atoms)
        # 原子序号 = win 文件 atoms 数组的 1 -based 位置（书内唯一且稳定）。
        # 注意：atom["seq"] 是**窗内**序号，各窗都从 1 起，书内不唯一，不能当标识用。
        for idx, atom in enumerate(atoms, start=1):
            summary = (atom.get("summary") or "").strip()
            if not summary:
                continue
            for fam, kws in FAMILIES.items():
                matched = hits(summary, kws)
                if not matched:
                    continue
                records.append({
                    "book": book,
                    "atom_index": idx,
                    "atom_seq": atom.get("seq"),
                    "family": fam,
                    "matched": matched,
                    "label": atom.get("atomic_id"),
                    "chapter_start": atom.get("chapter_start"),
                    "chapter_end": atom.get("chapter_end"),
                    "arc_ref": f"{book}#win原子{idx}",
                    "summary": summary,
                    "summary_len": len(summary),
                })

    records.sort(key=lambda r: (r["book"], r["atom_index"], r["family"]))
    uniq: dict[str, dict] = {}
    for r in records:
        key = f"{r['book']}#{r['atom_index']}"
        u = uniq.setdefault(key, {"book": r["book"], "atom_index": r["atom_index"],
                                  "families_hit": []})
        if r["family"] not in u["families_hit"]:
            u["families_hit"].append(r["family"])
    fam_counts = {fam: sum(1 for r in records if r["family"] == fam) for fam in FAMILIES}
    payload = {
        "task": "INTERN-SK05E",
        "step": "1-确定性提取",
        "spec": "关键词逐字照抄任务单 §背景；原子序号=win 文件 atoms 数组 1-based 位置；"
                "arc_ref=书名#win原子NNN（2026-10-07 PM 裁决键口径，替掉任务单原「书名#原子序号」）",
        "keywords": FAMILIES,
        "books": {b: per_book.get(b, 0) for b in BOOKS},
        "total_atoms_scanned": sum(per_book.values()),
        "candidate_records": len(records),
        "unique_atoms": len(uniq),
        "family_counts": fam_counts,
        "candidates": records,
        "sha256": "",
    }
    body = json.dumps({k: v for k, v in payload.items() if k != "sha256"},
                      ensure_ascii=False, sort_keys=True)
    payload["sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    text = json.dumps(payload, ensure_ascii=False, indent=1)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    if OUTFILE.exists() and OUTFILE.read_text(encoding="utf-8") == text:
        print(f"[extract] 幂等：内容未变，不重写 {OUTFILE.name}")
    # newline='\n' 防 Windows 文本模式 LF→CRLF（坑：Windows 写文件）
    OUTFILE.write_text(text, encoding="utf-8", newline="\n")
    print(f"[extract] 扫描 {sum(per_book.values())} 条原子 / 8 书 → 候选记录 {len(records)} 条"
          f"（唯一原子 {len(uniq)}）")
    for fam, n in fam_counts.items():
        print(f"   族 {fam}: {n}")
    print(f"[extract] sha256={payload['sha256'][:16]} → {OUTFILE}")
    if missing:
        print("[extract] 缺失 win 文件: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
