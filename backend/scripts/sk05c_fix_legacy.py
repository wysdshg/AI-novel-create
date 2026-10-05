# -*- coding: utf-8 -*-
"""[DEV-SK05C] 存量脏数据定点修（SK05B verify 浮出的 5 处，F2 时代遗留）。

    .venv\\Scripts\\python.exe backend/scripts/sk05c_fix_legacy.py --dry-run
    ... --apply / --verify

纪律：win 改前备份 *.bak-sk05c（存在即不覆盖）；定位 (book, cs, ce, seq) 必须唯一；
应用前逐条断言改前态（防并发改动）；零库写、零 API。台账落 outputs/sk05c/。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "outputs" / "_atomic_raw"
S05C = ROOT / "outputs" / "sk05c"
BAK = ".bak-sk05c"
NOFIT = "无合适原子"

# 五处定点：原值 → 新值（依据见 reason/evidence，逐条人工判读/原文核对）
FIXES = [
    {"book": "win_九星霸体诀.json", "cs": 90, "ce": 91, "seq": 22, "kind": "非法ID",
     "old": "G09", "new": "A09", "conf": "中",
     "reason": "读 c90~91 原文：主角独自猎杀二阶魔兽（金刚怒猿）以还森林之神债，拖尸赴神树备血祭；"
               "主轴=猎兽，A09『猎杀魔兽取其内丹晶核为己所用』最贴。备选 F06『秘术献祭』仅合拍铺垫"
               "（仪式在 c92 另条），不取。G09 系 F2 时代占位符（SK04 取证清单记为「G09 未命名原子」，"
               "77 行词表 G 域只到 G07/G08，无此 ID）。",
     "evidence": "决定独自猎杀还债"},
    {"book": "win_九星霸体诀.json", "cs": 1168, "ce": 1169, "seq": 3, "kind": "矛盾态除标",
     "old": ["D02", NOFIT], "new": "D02（tags 去「无合适原子」）", "conf": "—",
     "reason": "有 ID 却带「无合适原子」标（不变量：有 ID 不带头标）。原子判定 D02 中毒疗伤为主轴，"
               "除标即可，不改 ID（非判类范围）。",
     "evidence": "服用禁药导致天道抽取能量剧痛"},
    {"book": "win_九星霸体诀.json", "cs": 1743, "ce": 1746, "seq": 3, "kind": "矛盾态除标",
     "old": ["G02", NOFIT], "new": "G02（tags 去「无合适原子」）", "conf": "—",
     "reason": "同上：有 ID（G02 秘境夺宝）却带「无合适原子」标，按不变量除标。",
     "evidence": "潜入魔兽体内洗劫宝库"},
    {"book": "win_遮天.json", "cs": 1352, "ce": 1352, "seq": 5, "kind": "矛盾态除标",
     "old": ["G05", NOFIT], "new": "G05（tags 去「无合适原子」）", "conf": "—",
     "reason": "同上：有 ID（G05 寻访查探）却带「无合适原子」标，按不变量除标。",
     "evidence": "推测下方有古帝坟墓"},
    {"book": "win_遮天.json", "cs": 1366, "ce": 1367, "seq": 10, "kind": "矛盾态除标",
     "old": ["C05", NOFIT], "new": "C05（tags 去「无合适原子」）", "conf": "—",
     "reason": "同上：有 ID（C05 立威震慑）却带「无合适原子」标，按不变量除标。",
     "evidence": "人皇显化降临，威严震慑诸天"},
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def dump(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def locate(data: dict, f: dict) -> tuple[dict | None, str]:
    hit = [a for a in data["atoms"]
           if (a.get("chapter_start"), a.get("chapter_end"), a.get("seq")) == (f["cs"], f["ce"], f["seq"])]
    if len(hit) != 1:
        return None, f"定位命中 {len(hit)} 条（要求唯一）"
    return hit[0], ""


def check_pre(a: dict, f: dict) -> str:
    aid = a.get("atomic_id") or ""
    tags = a.get("tags") or []
    if f["kind"] == "非法ID":
        if aid != f["old"]:
            return f"改前态不符：atomic_id={aid!r} ≠ {f['old']!r}（疑似并发改动，拒写）"
    else:
        if aid != f["old"][0] or NOFIT not in tags:
            return f"改前态不符：atomic_id={aid!r} / tags={tags!r}（疑似并发改动，拒写）"
    return ""


def check_post(a: dict, f: dict) -> str:
    aid = a.get("atomic_id") or ""
    tags = a.get("tags") or []
    if f["kind"] == "非法ID":
        if aid != f["new"]:
            return f"改后态不符：atomic_id={aid!r} ≠ {f['new']!r}"
    else:
        if aid != f["old"][0] or NOFIT in tags:
            return f"改后态不符：atomic_id={aid!r} / tags={tags!r}"
    return ""


def ensure_backup(book: str) -> str:
    src = RAW / book
    dst = RAW / (book + BAK)
    if dst.exists():
        return f"{BAK} 已存在（不覆盖，{sha(dst)}）"
    shutil.copy2(src, dst)
    return f"{BAK} 新建（{sha(dst)}）"


def run(mode: str) -> int:
    books: dict[str, dict] = {}
    ledger, errs = [], []
    changed = 0
    for f in FIXES:
        book = f["book"]
        if book not in books:
            books[book] = load(RAW / book)
        a, e = locate(books[book], f)
        row = {"book": book, "chapter": f"c{f['cs']}~{f['ce']}", "seq": f["seq"],
               "kind": f["kind"], "原值": f["old"], "新值": f["new"],
               "依据": f["reason"], "原文/摘要引证": f["evidence"]}
        if a is None:
            errs.append(f"{book} c{f['cs']}~{f['ce']} seq{f['seq']}: {e}")
            ledger.append(row)
            continue
        if mode == "verify":
            e2 = check_post(a, f)
            row["verify"] = "PASS" if not e2 else f"FAIL {e2}"
            if e2:
                errs.append(f"{book} c{f['cs']}~{f['ce']} seq{f['seq']}: {e2}")
        else:
            e1 = check_pre(a, f)
            if e1:
                # 幂等：若已是目标态（前次 apply 已生效）→ 放行不算并发
                e2 = check_post(a, f)
                if not e2:
                    row["改前态"] = "already-applied"
                    ledger.append(row)
                    continue
                row["改前态"] = e1
                errs.append(f"{book} c{f['cs']}~{f['ce']} seq{f['seq']}: {e1}")
                ledger.append(row)
                continue
            row["改前态"] = "OK"
            if mode == "apply":
                row["备份"] = ensure_backup(book)
                if f["kind"] == "非法ID":
                    a["atomic_id"] = f["new"]
                else:
                    a["tags"] = [t for t in (a.get("tags") or []) if t != NOFIT]
                changed += 1
        ledger.append(row)
    if mode == "apply":
        for book, data in books.items():
            if any(r["book"] == book and r.get("改前态") == "OK" for r in ledger):
                dump(RAW / book, data)
    if errs:
        print("校验错误：")
        for e in errs:
            print("  !", e)
    S05C.mkdir(parents=True, exist_ok=True)
    (S05C / "定点修台账.json").write_text(
        json.dumps({"mode": mode, "fixes": ledger, "changed": changed, "errs": errs},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{mode}] 处置 {len(ledger)} 条 / 本次改写 {changed} 处 / 校验错误 {len(errs)}")
    for r in ledger:
        v = r.get("verify") or r.get("改前态") or ""
        print(f"  [{v.split()[0]}] {r['book']} {r['chapter']} seq{r['seq']} "
              f"{r['kind']}: {r['原值']} → {r['新值']}")
    return 0 if not errs else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    mode = "dry-run" if a.dry_run else ("apply" if a.apply else "verify")
    sys.exit(run(mode))


if __name__ == "__main__":
    main()
