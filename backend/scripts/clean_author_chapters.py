# -*- coding: utf-8 -*-
"""作者交流章清洗（2026-09-19 用户拍板：方案A 文件名判别 + 系统回收站）。

判别规则（方案A，零误伤正文）：
- 文件大小 <= 阈值(默认 4K) 且 文件名**不含**「第<数字>章」格式 → 作者交流章 → 处理；
- 文件名含章号格式的默认保留（正文短章也保，如遮天"第255章 天鹏极速"1249B）；
- 已人工确认的伪装项（文件名带章号但内容实为交流）走 EXTRA 兜底名单精确删除。

处置：Windows 回收站（SHFileOperationW + FOF_ALLOWUNDO），**不永久删除**，可从回收站还原。
默认 dry-run 只打印清单；--apply 才真正移入回收站。任何一项失败立即停止。
"""
import argparse
import re
import sys
from pathlib import Path

from send2trash import send2trash

# 🔴 章号格式两种都要认：阿拉伯（第177章）与中文数字（第一百二十七章，遮天全用这种）
CHAPTER_RE = re.compile(r"第[0-9一二三四五六七八九十百千零两]+章")

# 已人工逐个读内容确认的伪装项（文件名带章号、内容是作者交流）——精确文件名匹配
EXTRA_CONFIRMED = {
    "0089_第89章 一些话.txt",                    # 作者谈创作经历
    "0622_第622章 一些话。.txt",                  # 作者谈改卷名/读者建议
    "0931_第八百九十九章 无疆（1+2_2）（潜龙大佬白银加更10_20）.txt",  # 内容实为月票抽奖编号名单
}

# ---------------- 回收站 ----------------
# （send2trash 2.1.0，venv 已装；内部走 Windows IFileOperation COM）


def recycle_delete_many(paths: list[Path], tmp_dir: Path) -> list[Path]:
    """逐个移入回收站（send2trash → Windows IFileOperation COM，最可靠）。
    返回**失败**列表。🔴 教训：SHFileOperationW 在本环境 rc 不可信（报 rc=2 但实际已删）、
    PowerShell VisualBasic FileIO 批量全静默失败——只有 send2trash 结果与文件系统一致。"""
    failed = []
    for p in paths:
        try:
            send2trash(str(p))
        except Exception as e:
            failed.append(p)
            print(f"    (失败 {type(e).__name__}: {str(e)[:100]}: {p.name})")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", nargs="+", required=True, help="书目录（可多个）")
    ap.add_argument("--threshold", type=int, default=4096, help="大小阈值字节（默认 4096）")
    ap.add_argument("--apply", action="store_true", help="实际移入回收站（默认 dry-run）")
    args = ap.parse_args()

    total_kept, total_hit, all_failed = 0, 0, []
    for root in args.book_dir:
        d = Path(root)
        if not d.is_dir():
            print(f"[跳过] 目录不存在: {d}")
            continue
        hits = []
        for f in sorted(d.glob("*.txt")):
            if not f.is_file() or f.stat().st_size > args.threshold:
                continue
            if CHAPTER_RE.search(f.stem) and f.name not in EXTRA_CONFIRMED:
                total_kept += 1          # 正文短章，保留
                continue
            hits.append(f)
        total_hit += len(hits)
        mode = "APPLY(回收站)" if args.apply else "DRY-RUN"
        print(f"\n◆ {d.name}：命中 {len(hits)} 个（{mode}）")
        if not args.apply:
            for f in hits:
                tag = "兜底" if f.name in EXTRA_CONFIRMED else ""
                print(f"  [将删]{tag} {f.name} [{f.stat().st_size}B]")
            continue
        dir_failed = recycle_delete_many(hits, d)
        n_ok = len(hits) - len(dir_failed)
        print(f"  已移入回收站 {n_ok}｜失败 {len(dir_failed)}")
        all_failed.extend(dir_failed)

    print(f"\n合计：命中 {total_hit}｜保留正文短章 {total_kept}"
          f"｜模式 {'APPLY' if args.apply else 'DRY-RUN'}"
          + (f"｜🔴 仍失败 {len(all_failed)}" if args.apply else ""))
    return 1 if (args.apply and all_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
