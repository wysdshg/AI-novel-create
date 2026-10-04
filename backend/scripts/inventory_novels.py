# -*- coding: utf-8 -*-
"""[DEV-DATA01] 第一步：素材目录盘点（只读，零 LLM 调用）。

产出 outputs/data_clean/盘点报告.md：每本书 / 章节文件数 / 命名规律 / 编码 / 大小分布。
本脚本不写任何素材文件。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOVEL_ROOT = ROOT / "小说"
OUT_DIR = ROOT / "outputs" / "data_clean"

# 章节文件名规律：NNNN_标题.txt
SEQ_PREFIX = re.compile(r"^(?P<seq>\d{3,5})_")
CHAPTER_TOKEN = re.compile(r"第[0-9一二三四五六七八九十百千零两]+章")
# 番外/感言类文件名常见词（只用于盘点计数，分类以 gate ② 为准）
NONBODY_HINT = re.compile(
    r"番外|特别篇|番外篇|完结|完本|感言|总结|后记|前言|序章?的话|作者的话|作者说|上架|通知|请假|单章|推书|新书|楔子|尾声|终章感言|抽奖|感谢|书评|章说"
)

ENCODINGS = ("utf-8", "utf-8-sig", "gb18030")


def decode(raw: bytes) -> str | None:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def detect_encoding(raw: bytes) -> str:
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    for enc in ("utf-8", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "unknown"


def bucket(size: int) -> str:
    if size < 1000:
        return "<1k"
    if size < 3000:
        return "1~3k"
    if size < 5000:
        return "3~5k"
    if size < 10000:
        return "5~10k"
    return ">=10k"


BUCKET_ORDER = ["<1k", "1~3k", "3~5k", "5~10k", ">=10k"]


def scan_dir(path: Path) -> list[dict]:
    rows = []
    for f in sorted(path.glob("*.txt")):
        if not f.is_file():
            continue
        size = f.stat().st_size
        raw = f.read_bytes()
        text = decode(raw)
        rows.append(
            {
                "name": f.name,
                "path": str(f.relative_to(ROOT)),
                "size": size,
                "enc": detect_encoding(raw),
                "seq": (SEQ_PREFIX.match(f.name).group("seq") if SEQ_PREFIX.match(f.name) else None),
                "has_chapter_token": bool(CHAPTER_TOKEN.search(f.stem)),
                "nonbody_hint": bool(NONBODY_HINT.search(f.stem)),
                "chars": len(text) if text is not None else None,
                "decode_fail": text is None,
            }
        )
    return rows


def stats(sizes: list[int]) -> dict:
    if not sizes:
        return {}
    s = sorted(sizes)
    return {
        "count": len(s),
        "min": s[0],
        "p10": s[max(0, int(len(s) * 0.10) - 1)],
        "p25": s[max(0, int(len(s) * 0.25) - 1)],
        "median": int(statistics.median(s)),
        "p75": s[min(len(s) - 1, int(len(s) * 0.75))],
        "p90": s[min(len(s) - 1, int(len(s) * 0.90))],
        "max": s[-1],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DIR / "盘点报告.md"))
    ap.add_argument("--json-out", default=str(OUT_DIR / "盘点明细.json"))
    args = ap.parse_args()

    books = []
    # `_` 前缀目录（_quarantine 隔离区、_残章）不在清洗/入库范围，盘点口径与之保持一致
    for d in sorted(p for p in NOVEL_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")):
        rows = scan_dir(d)
        subs = [s for s in d.iterdir() if s.is_dir()]
        books.append({"book": d.name, "dir": str(d.relative_to(ROOT)), "rows": rows,
                      "subdirs": {s.name: scan_dir(s) for s in subs}})

    lines: list[str] = []
    lines.append("# [DEV-DATA01] 素材目录盘点报告\n")
    lines.append(f"- 素材根：`{NOVEL_ROOT}`")
    lines.append("- 方式：本地文件系统遍历（只读，零 LLM 调用）")
    total_files = sum(len(b["rows"]) for b in books)
    lines.append(f"- 书目 **{len(books)}** 个，`.txt` 章节文件合计 **{total_files}**\n")

    # 全局编码 / 命名 / 异常
    enc_counter: dict[str, int] = {}
    seq_mismatch, decode_fail, hint_count = [], [], 0
    for b in books:
        for r in b["rows"]:
            enc_counter[r["enc"]] = enc_counter.get(r["enc"], 0) + 1
            if r["seq"] is None:
                seq_mismatch.append(r["path"])
            if r["decode_fail"]:
                decode_fail.append(r["path"])
            if r["nonbody_hint"]:
                hint_count += 1
    lines.append("## 全局特征\n")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    for enc, n in sorted(enc_counter.items(), key=lambda x: -x[1]):
        lines.append(f"| 编码 {enc} | {n} 个文件 |")
    lines.append(f"| 文件名不符合 `NNNN_标题.txt` | {len(seq_mismatch)} 个 |")
    lines.append(f"| 解码失败（三种编码都不通） | {len(decode_fail)} 个 |")
    lines.append(f"| 文件名含番外/感言/通知等词（仅计数，不作分类） | {hint_count} 个 |")
    lines.append("")
    if seq_mismatch:
        lines.append("<details><summary>不合命名规律的文件</summary>\n")
        for p in seq_mismatch:
            lines.append(f"- `{p}`")
        lines.append("\n</details>\n")
    if decode_fail:
        lines.append("🔴 解码失败文件清单：" + ", ".join(f"`{p}`" for p in decode_fail) + "\n")

    lines.append("## 每本书大小分布\n")
    lines.append("| 书 | .txt 数 | min | p10 | p25 | 中位 | p75 | p90 | max | <1k | 1~3k | 3~5k | 5~10k | ≥10k | 章号区间 | 重复章号文件数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in books:
        rows, st = b["rows"], stats([r["size"] for r in b["rows"]])
        buckets: dict[str, int] = {k: 0 for k in BUCKET_ORDER}
        for r in rows:
            buckets[bucket(r["size"])] += 1
        seqs = [int(r["seq"]) for r in rows if r["seq"]]
        dup = len(seqs) - len(set(seqs))
        tail = f"{min(seqs)}~{max(seqs)}" if seqs else "-"
        if st:
            lines.append(
                f"| {b['book']} | {st['count']} | {st['min']} | {st['p10']} | {st['p25']} | {st['median']} | "
                f"{st['p75']} | {st['p90']} | {st['max']} | "
                + " | ".join(str(buckets[k]) for k in BUCKET_ORDER) + f" | {tail} | {dup} |"
            )
        else:
            lines.append(f"| {b['book']} | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.append("")

    # 1~5k 簇重点：小于 5k 的占比
    lines.append("## 小文件簇（阈值候选依据）\n")
    lines.append("| 书 | <1k | 1~3k | 3~5k | 合计<5k | 占本书比例 | 无章号(<5k) | 文件名含非正文词(<5k) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in books:
        rows = b["rows"]
        small = [r for r in rows if r["size"] < 5000]
        no_tok = [r for r in small if not r["has_chapter_token"]]
        hints = [r for r in small if r["nonbody_hint"]]
        ratio = f"{len(small) / len(rows) * 100:.1f}%" if rows else "-"
        lines.append(
            f"| {b['book']} | {sum(1 for r in rows if r['size'] < 1000)} | "
            f"{sum(1 for r in rows if 1000 <= r['size'] < 3000)} | "
            f"{sum(1 for r in rows if 3000 <= r['size'] < 5000)} | {len(small)} | {ratio} | "
            f"{len(no_tok)} | {len(hints)} |"
        )
    lines.append("")

    lines.append("## 全库最小的 60 个 .txt（极端小文件，多为空白/通知/请假条）\n")
    all_rows = [r for b in books for r in b["rows"]]
    for r in sorted(all_rows, key=lambda x: x["size"])[:60]:
        lines.append(f"- {r['size']}B `{r['path']}`")
    lines.append("")

    lines.append("## 子目录（不属主章节序列，需单独定范围）\n")
    any_sub = False
    for b in books:
        for name, rows in b["subdirs"].items():
            any_sub = True
            st = stats([r["size"] for r in rows])
            lines.append(f"- `{b['dir']}/{name}`：{len(rows)} 个文件，中位 {st.get('median')}B，min {st.get('min')}B，max {st.get('max')}B")
    if not any_sub:
        lines.append("- 无")
    lines.append("")

    lines.append("## 非 .txt 文件\n")
    others = [str(p.relative_to(ROOT)) for p in NOVEL_ROOT.rglob("*") if p.is_file() and p.suffix.lower() != ".txt"]
    lines.append("\n".join(f"- `{p}`" for p in sorted(others)) if others else "- 无")
    lines.append("")

    lines.append("## 每本书最后 30 个章节文件名（番外集中区，gate ② 分类依据）\n")
    lines.append("（按文件名字符序排序后的末尾 30 个；括号 = 文件大小）\n")
    for b in books:
        rows = b["rows"]
        if not rows:
            continue
        tail_rows = rows[-30:]
        lines.append(f"### {b['book']}（共 {len(rows)} 章）\n")
        for r in tail_rows:
            flag = " ⚑非正文词" if r["nonbody_hint"] else ""
            lines.append(f"- `{r['name']}`（{r['size']}B）{flag}")
        lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(
        json.dumps(
            [{"book": b["book"], "dir": b["dir"],
              "stats": stats([r["size"] for r in b["rows"]]),
              "files": [{k: v for k, v in r.items() if k != "path"} for r in b["rows"]]}
             for b in books],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"[OK] 盘点报告 -> {out}")
    print(f"[OK] 盘点明细 -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
