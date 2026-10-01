# -*- coding: utf-8 -*-
"""
F8 P0 ① 出场密度统计脚本（机制文档 §4.2 间隔容忍游程合并，纯代码零 LLM）

输入：全书章节 txt + 花名册（主名<TAB>别称1,别称2 可选）
流程：逐章判定出场（主名或任一别称的子串命中）→ 0/1 章序列
      → gap_inner 缝合段内断点 → 段长 >= min_run 为有效段
      → 相邻有效段间隔 > gap_back 判跨篇 → 按 §4.1 六档阶梯分档
输出：JSON（全量数据 + 每角色在场章区间 + 分批注入清单）+ Markdown 报告

分批注入清单（--batch-size 25）：每批列出「本批在册角色及其档位/出场量」，
供分诊 LLM prompt 注入——AI 只做核对+补漏+行为摘要，不从零判断人物重要性。

用法：
  python scripts/f8_density_stats.py \
    --book-dir "E:/AI小说创作/小说/凡人修仙传" \
    --names outputs/f8_p0/names_fanren_zhihu.txt \
    --out outputs/f8_p0/density_fanren.json --batch-size 25
阈值全部可配置（初值 = 机制文档 §4.1/§4.2，跑两三本书看分布再调）。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

# ---------- 章节文件扫描 ----------

NUM_PREFIX = re.compile(r"^(\d+)")

def list_chapters(book_dir):
    """自然排序：取文件名前导数字；无数字前缀的文件（如 书籍信息.txt）跳过。
    返回 [(章号, 文件名)]，章号=文件名前导数字（可能与实际章数有跳号，仅作展示）。"""
    entries, skipped = [], []
    for fn in sorted(os.listdir(book_dir)):
        if not fn.lower().endswith(".txt"):
            continue
        m = NUM_PREFIX.match(fn)
        if not m:
            skipped.append(fn)
            continue
        entries.append((int(m.group(1)), fn))
    entries.sort()
    if skipped:
        print(f"[warn] 跳过 {len(skipped)} 个无编号文件: {skipped[:3]}")
    return entries

def read_text(path):
    for enc in ("utf-8", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

# ---------- §4.2 游程合并 ----------

def raw_runs(mask):
    """未缝合的连续 1 段（在场章区间，闭区间 0 起）。"""
    runs, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs

def stitch(mask, gap_inner):
    """把被 <=gap_inner 个 0 隔开的 1 段缝合，返回 [(start, end)] 闭区间。"""
    if not any(mask):
        return []
    segs = []
    i, n = 0, len(mask)
    while i < n and not mask[i]:
        i += 1
    start = i
    zero_run = 0
    for j in range(i, n):
        if mask[j]:
            if zero_run > gap_inner:
                segs.append((start, j - zero_run - 1))
                start = j
            zero_run = 0
        else:
            zero_run += 1
    segs.append((start, n - 1 - zero_run))
    return segs

def cluster_segs(segs, gap_back):
    """相邻有效段间隔（中间 0 的个数）> gap_back → 不同篇。返回 [[(s,e),...], ...]"""
    if not segs:
        return []
    clusters = [[segs[0]]]
    for s, e in segs[1:]:
        ps, pe = clusters[-1][-1]
        if s - pe - 1 > gap_back:
            clusters.append([(s, e)])
        else:
            clusters[-1].append((s, e))
    return clusters

# ---------- §4.1 六档分档 ----------

def grade(total_on, n_chapters, eff_segs, clusters, p):
    """返回 (档位, 命中规则说明)。全部阈值来自 p（CLI 参数）。"""
    if total_on == 0:
        return None, "不出场"
    if total_on < p.t0:
        return 0, f"出场{total_on} < t0={p.t0}"
    coverage = total_on / n_chapters
    if coverage >= p.t5_ratio:
        return 5, f"覆盖率{coverage:.0%} >= t5_ratio={p.t5_ratio:.0%}（贯穿全文）"
    if total_on >= p.t4:
        return 4, f"累计{total_on} >= t4={p.t4}"
    max_seg = max((e - s + 1 for s, e in eff_segs), default=0)
    if not eff_segs:
        return 1, "无有效段（段长均 < min_run）"
    if len(eff_segs) >= 3 or (clusters >= 2 and total_on >= p.t3):
        return 3, f"有效段{len(eff_segs)}个/跨{clusters}篇/累计{total_on}"
    if clusters <= 2 and max_seg >= p.t2_run:
        return 2, f"有效段{max_seg}章集中{clusters}篇"
    if total_on >= p.t3:
        return 3, f"累计{total_on} >= t3={p.t3}"
    return 1, f"断续出场{total_on}章、最长段{max_seg}"

# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", required=True)
    ap.add_argument("--names", required=True, help="花名册文件：每行 主名 或 主名<TAB>别称1,别称2")
    ap.add_argument("--out", required=True, help="JSON 输出路径")
    ap.add_argument("--report", help="Markdown 报告路径（缺省= out 换后缀 .md）")
    ap.add_argument("--batch-size", type=int, default=0, help=">0 时生成分批注入清单")
    # §4.2 密度参数
    ap.add_argument("--gap-inner", type=int, default=3)
    ap.add_argument("--min-run", type=int, default=8)
    ap.add_argument("--gap-back", type=int, default=5)
    # §4.1 分档阈值
    ap.add_argument("--t0", type=int, default=5)
    ap.add_argument("--t2-run", type=int, default=10)
    ap.add_argument("--t3", type=int, default=40)
    ap.add_argument("--t4", type=int, default=100)
    ap.add_argument("--t5-ratio", type=float, default=0.4)
    ap.add_argument("--top", type=int, default=90, help="报告明细表显示前 N 名")
    ap.add_argument("--spot", default="", help="需打印段明细的抽检角色，逗号分隔")
    args = ap.parse_args()
    args.report = args.report or os.path.splitext(args.out)[0] + ".md"

    # 花名册
    roster = []  # [(main, [aliases])]
    with open(args.names, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            main = parts[0].strip()
            aliases = [a.strip() for a in parts[1].split(",") if a.strip()] if len(parts) > 1 else []
            if main:
                roster.append((main, aliases))

    chapters = list_chapters(args.book_dir)  # [(章号, 文件名)]
    n = len(chapters)
    if n == 0:
        sys.exit("无章节文件")

    # 逐章扫描
    per_char = {main: {"mask": [0] * n, "occur": 0, "main_only": 0, "alias_only": 0,
                       "alias": aliases} for main, aliases in roster}
    for idx, (chno, fn) in enumerate(chapters):
        text = read_text(os.path.join(args.book_dir, fn))
        for main, aliases in roster:
            rec = per_char[main]
            hit_main = main in text
            hit_alias = any(a in text for a in aliases)
            if hit_main or hit_alias:
                rec["mask"][idx] = 1
                rec["occur"] += text.count(main) + sum(text.count(a) for a in aliases)
                if hit_main and not hit_alias:
                    rec["main_only"] += 1
                elif hit_alias and not hit_main:
                    rec["alias_only"] += 1

    # 游程合并 + 分档
    grade_of = {}
    results = []
    for main, rec in per_char.items():
        runs = raw_runs(rec["mask"])
        raw_segs = stitch(rec["mask"], args.gap_inner)
        eff_segs = [(s, e) for s, e in raw_segs if e - s + 1 >= args.min_run]
        clus = cluster_segs(eff_segs, args.gap_back)
        total_on = sum(rec["mask"])
        g, rule = grade(total_on, n, eff_segs, len(clus), args)
        grade_of[main] = g
        results.append({
            "name": main, "aliases": rec["alias"], "total_on": total_on,
            "occur": rec["occur"], "main_only": rec["main_only"], "alias_only": rec["alias_only"],
            "eff_seg_count": len(eff_segs),
            "eff_segs": [{"start": s + 1, "end": e + 1, "len": e - s + 1} for s, e in eff_segs],
            "raw_seg_count": len(raw_segs),
            "gaps": [eff_segs[i + 1][0] - eff_segs[i][1] - 1 for i in range(len(eff_segs) - 1)],
            "clusters": len(clus),
            "cluster_spans": [{"start": c[0][0] + 1, "end": c[-1][1] + 1,
                               "segs": len(c)} for c in clus],
            "presence_runs": [[s + 1, e + 1] for s, e in runs],
            "coverage": round(total_on / n, 4),
            "grade": g, "rule": rule,
        })
    results.sort(key=lambda r: -r["total_on"])

    # 分批注入清单
    batches = []
    if args.batch_size > 0:
        bs = args.batch_size
        by_name = {r["name"]: r for r in results}
        masks = {main: rec["mask"] for main, rec in per_char.items()}
        for b0 in range(0, n, bs):
            b1 = min(b0 + bs, n)
            present = []
            for main in by_name:
                if any(masks[main][b0:b1]):
                    r = by_name[main]
                    present.append({"name": main, "grade": r["grade"],
                                    "total_on": r["total_on"], "aliases": r["aliases"]})
            present.sort(key=lambda x: -(x["total_on"] or 0))
            batches.append({
                "batch": b0 // bs + 1,
                "idx_range": [b0 + 1, b1],
                "chapters": [chapters[b0][0], chapters[b1 - 1][0]],
                "files": [chapters[b0][1], chapters[b1 - 1][1]],
                "chars": present,
            })

    # 落盘
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    payload = {
        "book_dir": args.book_dir, "n_chapters": n,
        "chapter_nums": [c for c, _ in chapters],
        "roster_source": args.names, "generated_at": datetime.now().isoformat(),
        "params": {"gap_inner": args.gap_inner, "min_run": args.min_run, "gap_back": args.gap_back,
                    "t0": args.t0, "t2_run": args.t2_run, "t3": args.t3, "t4": args.t4,
                    "t5_ratio": args.t5_ratio, "batch_size": args.batch_size},
        "results": results,
        "batches": batches,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # Markdown 报告
    L = [f"# F8 P0 出场密度报告 — {os.path.basename(args.book_dir)}", "",
         f"- 章数 N = **{n}**｜花名册 {len(roster)} 人｜生成于 {datetime.now():%m-%d %H:%M}",
         f"- 参数：gap_inner={args.gap_inner} min_run={args.min_run} gap_back={args.gap_back}"
         f" t0={args.t0} t2_run={args.t2_run} t3={args.t3} t4={args.t4} t5_ratio={args.t5_ratio}",
         f"- 分批注入：{'每批 ' + str(args.batch_size) + ' 章，共 ' + str(len(batches)) + ' 批' if batches else '未生成'}", ""]
    dist = {}
    for r in results:
        if r["grade"] is not None:
            dist[r["grade"]] = dist.get(r["grade"], 0) + 1
    L += ["## 分档分布", "", "| 档 | 人数 |", "|---|---|"]
    for g in range(6):
        L.append(f"| {g} | {dist.get(g, 0)} |")
    L.append("")
    L += ["## 明细（按出场章数降序）", "",
          "| 角色 | 档 | 出场章 | 覆盖率 | 有效段 | 最长段 | 篇簇 | 别称章 | 规则 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in results[: args.top]:
        max_seg = max((s["len"] for s in r["eff_segs"]), default=0)
        L.append(f"| {r['name']} | {r['grade'] if r['grade'] is not None else '—'}"
                 f" | {r['total_on']} | {r['coverage']:.1%} | {r['eff_seg_count']} | {max_seg}"
                 f" | {r['clusters']} | {r['alias_only']} | {r['rule']} |")
    spot = [s.strip() for s in args.spot.split(",") if s.strip()]
    if spot:
        L += ["", "## 抽检角色段明细"]
        for nm in spot:
            r = next((x for x in results if x["name"] == nm), None)
            L.append("")
            if not r:
                L.append(f"### {nm}：未在花名册中")
                continue
            L.append(f"### {nm}（档 {r['grade']}，出场 {r['total_on']} 章，{r['rule']}）")
            if r["eff_segs"]:
                seg_strs = [f"{s['start']}~{s['end']}({s['len']})" for s in r["eff_segs"][:20]]
                more = f" …共{len(r['eff_segs'])}段" if len(r["eff_segs"]) > 20 else ""
                L.append("- 有效段: " + "、".join(seg_strs) + more)
                if r["gaps"]:
                    L.append(f"- 段间 gap: {r['gaps'][:20]}")
            else:
                L.append("- 无有效段")
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print(f"N={n} 花名册={len(roster)} 批次={len(batches)} -> {args.out}")
    print("分布:", {g: dist.get(g, 0) for g in range(6)})

if __name__ == "__main__":
    main()
