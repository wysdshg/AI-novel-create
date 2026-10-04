# -*- coding: utf-8 -*-
"""[DEV-SK01] 从备份 JSON + 两份清单生成报告用表格（Markdown），避免手抄出错。

    .venv\\Scripts\\python.exe backend/scripts/_sk01_report_data.py
"""
from __future__ import annotations

import io
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sk01_purge_summaries import (  # noqa: E402
    BACKUP, DELETE_LIST, NOISE_DECISIONS, NOISE_LIST, DEL_LINE, NOISE_HEAD,
)

OUT = Path(__file__).resolve().parents[2] / "outputs" / "skel_v4" / "_SK01_report_tables.md"


def parse(path, rx, kind):
    rows, cur = [], None
    for raw in io.open(path, encoding="utf-8"):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith(("SK01", "格式:", "疑似")):
            continue
        m = rx.match(line)
        if m:
            if kind == "del":
                rows.append({"book": m["book"], "no": int(m["no"]), "title": m["title"], "file": m["file"]})
            else:
                cur = {"book": m["book"], "no": int(m["no"]), "title": m["title"], "preview": ""}
                rows.append(cur)
        elif kind == "noise" and cur is not None and raw.startswith("  "):
            cur["preview"] = raw.strip()
    return rows


def main() -> None:
    snap = json.load(io.open(BACKUP, encoding="utf-8"))
    dels = parse(DELETE_LIST, DEL_LINE, "del")
    noises = parse(NOISE_LIST, NOISE_HEAD, "noise")
    del_keys = {(d["book"], d["no"]) for d in dels}
    by_id = {r["id"]: r for r in snap["deleted"]}
    upd = snap["updated"][0]
    out = io.open(OUT, "w", encoding="utf-8")

    print("## A. 65 行待删清单执行情况（按书）\n", file=out)
    print("| 书名 | 清单行数 | 实际删除 | 一致 |", file=out)
    print("|---|---|---|---|", file=out)
    cnt_list = Counter(d["book"] for d in dels)
    src_del = [r for r in snap["deleted"] if r["_source"].startswith("待删清单")]
    cnt_done = Counter(r["book_name"] for r in src_del)
    for b in sorted(cnt_list, key=lambda x: -cnt_list[x]):
        ok = "✅" if cnt_list[b] == cnt_done.get(b, 0) else "❌"
        print(f"| {b} | {cnt_list[b]} | {cnt_done.get(b, 0)} | {ok} |", file=out)
    print(f"| **合计** | **{sum(cnt_list.values())}** | **{sum(cnt_done.values())}** | "
          f"{'✅' if sum(cnt_list.values()) == sum(cnt_done.values()) else '❌'} |", file=out)

    noise_src = [r for r in snap["deleted"] if r["_source"] == "噪声清单"]
    print(f"\n## B. 噪声 21 行逐条处置表\n\n"
          f"> 重叠 {len(del_keys & {(n['book'], n['no']) for n in noises})} 行按「以删行为准」随 A 段删除。\n", file=out)
    print("| # | 书名 | 章 | 标题 | 命中词 | 处置 | 依据 |", file=out)
    print("|---|---|---|---|---|---|---|", file=out)
    for i, n in enumerate(noises, 1):
        key = (n["book"], n["no"])
        words = [w for w in ("翻页", "本章未完", "本章已完成", "求票", "求月票", "求推荐票",
                             "求收藏", "点击收藏", "未完待续", "请点击下一页", "ps:")
                 if w in (n["preview"] or "")]
        if key in del_keys:
            act, why = "删（随 A 段）", "整章已是隔离的通知/感言章 → 以删行为准"
        elif key in {(u["book_name"], u["chapter_no"]) for u in snap["updated"]}:
            act = "改写（掐尾）"
            why = NOISE_DECISIONS[key][1]
        elif key in {(r["book_name"], r["chapter_no"]) for r in noise_src}:
            act = "删"
            why = NOISE_DECISIONS[key][1]
        else:
            act = "待定（不动）"
            why = NOISE_DECISIONS[key][1]
        title = n["title"] if len(n["title"]) <= 24 else n["title"][:23] + "…"
        print(f"| {i} | {n['book']} | c{n['no']} | {title} | {'/'.join(words) or '（预览截断，DB 命中）'} "
              f"| {act} | {why} |", file=out)

    print("\n## C. 改写行前后对照（≤60 字窗口）\n", file=out)
    print(f"- 行：`{upd['book_name']} c{upd['chapter_no']}` id=`{upd['id']}` 标题《{upd['title']}》", file=out)
    print(f"- 掐掉碎片：`{upd['_fragment_removed']}`", file=out)
    print(f"- before（尾 60 字）：…{upd['summary'][-60:]}", file=out)
    print(f"- after （尾 60 字）：…{upd['_new_summary'][-60:]}", file=out)
    print(f"- summary_raw 同步改写：{'是' if upd['_new_summary_raw'] else '否'}；"
          f"长度 {len(upd['summary'])} → {len(upd['_new_summary'])}", file=out)

    print("\n## D. 噪声独有 5 行删除明细\n", file=out)
    for r in noise_src:
        print(f"- {r['book_name']} c{r['chapter_no']}《{r['title']}》 概括 {len(r['summary'] or '')} 字 "
              f"| id={r['id']} | {r['_reason']}", file=out)

    print(f"\n## E. 备份与统计\n", file=out)
    print(f"- 备份文件：`outputs/skel_v4/SK01_删除备份.json`（ts_utc={snap['ts_utc']}，columns={len(snap['columns'])} 列全字段）", file=out)
    print(f"- deleted={len(snap['deleted'])}，updated={len(snap['updated'])}，"
          f"备份行数={len(snap['deleted']) + len(snap['updated'])}，counts={json.dumps(snap['counts'], ensure_ascii=False)}", file=out)
    print(f"- 库表 chapter_summaries：16758 → 16688（-70）", file=out)
    print(f"- 剩余噪声命中：summary 1 行（遮天 c225 待定），summary_raw 0 行", file=out)
    out.close()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
