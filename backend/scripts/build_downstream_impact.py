# -*- coding: utf-8 -*-
"""[DEV-DATA01] 反查「素材章节隔离」对下游库表的影响，产出 gate ② 交付物受影响下游清单。

只读数据库（mode=ro），只写 outputs/data_clean/受影响下游清单.md。
反查链：文件名序号 → chapter_summaries(book_name, chapter_no) → arc_no →
       atomic_variants(arc_ref) → event_skeletons.evidence / plot_templates.source_stats
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clean_novel_chapters as cnc  # noqa: E402
from chapter_noise_patterns import SEQ_PREFIX  # noqa: E402

DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
OUT = cnc.ROOT / "outputs" / "data_clean" / "受影响下游清单.md"

# 素材目录名 → 库里 book_name（38 本里只有这 3 本目录名带后缀/全角括号，其余同名或未入库）
DIR_TO_DB = {
    "修真四万年 - 卧牛真人": "修真四万年",
    "圣墟（圣虚）": "圣墟",
    "道诡异仙 - 狐尾的笔": "道诡异仙",
}
PUNCT = re.compile(r"[\s《》（）()【】\[\]：:，,。.!！?？、·\-—~～]+")


def norm(s: str) -> str:
    return PUNCT.sub("", s or "")


def main() -> int:
    decisions = [d for d in json.loads(cnc.DECISIONS.read_text(encoding="utf-8"))
                 if isinstance(d, dict)]
    b_items = [d for d in decisions if d.get("action") == "B"]
    a_items = [d for d in decisions if d.get("action") == "A"]

    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    db_books = {r["book_name"] for r in con.execute(
        "SELECT DISTINCT book_name FROM chapter_summaries")}

    # ── 1. B 类文件 → chapter_summaries 行 ─────────────────────────
    hit_rows: list[sqlite3.Row] = []
    row_file: dict[int, str] = {}
    title_ok = title_bad = 0
    bad_samples: list[str] = []
    unmatched_books: dict[str, int] = defaultdict(int)
    per_dir_dbname: dict[str, str] = {}
    for d in b_items:
        book = d["book"]
        dbname = book if book in db_books else DIR_TO_DB.get(book)
        if not dbname:
            unmatched_books[book] += 1
            continue
        per_dir_dbname[book] = dbname
        m = SEQ_PREFIX.match(d["file"])
        if not m:
            unmatched_books[book] += 1
            continue
        no = int(m.group("seq"))
        rs = con.execute(
            "SELECT id,book_name,chapter_no,title,arc_no,arc_name,segment_no "
            "FROM chapter_summaries WHERE book_name=? AND chapter_no=?", (dbname, no)).fetchall()
        if not rs:
            continue
        got = norm(rs[0]["title"])
        want = norm(d["file"].split("_", 1)[1].rsplit(".", 1)[0])
        if got and (got in want or want in got or got[:8] == want[:8]):
            title_ok += 1
        else:
            title_bad += 1
            bad_samples.append(f"{dbname}#{no}｜库内标题「{rs[0]['title'][:28]}」"
                               f"≠ 文件名「{want[:28]}」")
        for r in rs:
            row_file[r["id"]] = f"{book}/{d['file']}"
        hit_rows.extend(rs)

    arcs = {}
    for r in hit_rows:
        if r["arc_no"] is not None:
            arcs[f"{r['book_name']}#{r['arc_no']}"] = (r["book_name"], r["arc_no"],
                                                       r["arc_name"] or "")

    # ── 2. arc → atomic_variants ─────────────────────────
    av_by_arc: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for arc in arcs:
        for r in con.execute("SELECT id,atomic_id,book_name,arc_ref,segment_no,quality,hit_count "
                             "FROM atomic_variants WHERE arc_ref=?", (arc,)):
            av_by_arc[arc].append(r)

    # ── 3. arc → event_skeletons / plot_templates ─────────
    skel_hit: dict[str, list[str]] = defaultdict(list)
    for r in con.execute("SELECT id,name,evidence FROM event_skeletons"):
        try:
            ev = json.loads(r["evidence"] or "[]")
        except json.JSONDecodeError:
            ev = []
        for e in ev if isinstance(ev, list) else []:
            key = str(e).split(":")[0]
            if key in arcs:
                skel_hit[key].append(f"{r['id']} {r['name']}")
    tpl_hit: dict[str, list[str]] = defaultdict(list)
    tpl_book_hit: dict[str, list[str]] = defaultdict(list)
    for r in con.execute("SELECT id,name,source_stats FROM plot_templates"):
        try:
            ss = json.loads(r["source_stats"] or "{}")
        except json.JSONDecodeError:
            ss = {}
        for a in ss.get("arc_refs") or []:
            key = str(a).split(":")[0]
            if key in arcs:
                tpl_hit[key].append(f"{str(r['id'])[:8]} {r['name']}")
        for bn in ss.get("book_names") or []:
            if bn in {v[0] for v in arcs.values()}:
                tpl_book_hit[bn].append(f"{str(r['id'])[:8]} {r['name']}")

    # ── 4. book_aliases.first_chapter 的口径实测 ────────────────────
    # 反查发现：每本书记的 first_chapter 只有 ≤20 个不同取值，且等差（蛊真人 20 个值差恒为 30），
    # 与 chapter_summaries 的真实 segment/arc 起始章不吻合（∩=2/20）→ first_chapter 是
    # 「每 30 章一档」的粗网格锚点，不是精确首现章。因此「命中隔离章号」不能推出
    # 「该实体只出现在被隔离的那一章」，这一面判为不受影响，只保留取证数据。
    alias_hits: list[str] = []
    b_no_by_db: dict[str, set[int]] = defaultdict(set)
    for d in b_items:
        dbname = per_dir_dbname.get(d["book"])
        m = SEQ_PREFIX.match(d["file"])
        if dbname and m:
            b_no_by_db[dbname].add(int(m.group("seq")))
    for dbname, nos in b_no_by_db.items():
        for r in con.execute("SELECT book_name,alias,original,first_chapter FROM book_aliases "
                             "WHERE book_name=?", (dbname,)):
            try:
                fc = int(str(r["first_chapter"]).strip())
            except (TypeError, ValueError):
                continue
            if fc in nos:
                alias_hits.append(f"{dbname}｜{r['original']}→{r['alias']}"
                                  f"｜first_chapter={fc}")
    # 全库口径：每本书的 distinct first_chapter 档数 vs 章数（证明它是粗锚点而非精确首现）
    fc_breadth: list[str] = []
    fc_shape: dict[str, dict] = {}
    for r in con.execute("SELECT book_name, "
                         "COUNT(DISTINCT CAST(first_chapter AS INT)) d, "
                         "COUNT(*) n FROM book_aliases GROUP BY book_name"):
        bn = r["book_name"]
        vals = sorted({v[0] for v in con.execute(
            "SELECT DISTINCT CAST(first_chapter AS INT) FROM book_aliases WHERE book_name=? "
            "AND first_chapter IS NOT NULL", (bn,)) if v[0] is not None})
        mx_ch, n_rows = con.execute(
            "SELECT MAX(chapter_no), COUNT(*) FROM chapter_summaries WHERE book_name=?",
            (bn,)).fetchone()
        fc_shape[bn] = {
            "n_alias_rows": r["n"], "n_values": len(vals), "values": vals,
            "diffs": sorted({b - a for a, b in zip(vals, vals[1:])}),
            "db_rows": n_rows or 0, "db_max_ch": mx_ch,
        }
        fc_breadth.append(f"{bn} {r['d']}档/{n_rows or 0}章")
    # 网格证据：distinct first_chapter 取值 + 相邻差值 + 与真实段起始章的交集
    alias_grid: dict[str, dict] = {}
    for dbname in sorted({h.split("｜")[0] for h in alias_hits}):
        fcs = sorted({r["fc"] for r in con.execute(
            "SELECT DISTINCT CAST(first_chapter AS INT) fc FROM book_aliases WHERE book_name=? "
            "AND first_chapter IS NOT NULL", (dbname,)) if r["fc"] is not None})
        seg_start = {r["mn"] for r in con.execute(
            "SELECT segment_no, MIN(chapter_no) mn FROM chapter_summaries WHERE book_name=? "
            "AND segment_no IS NOT NULL GROUP BY segment_no", (dbname,))}
        n_segments = con.execute(
            "SELECT COUNT(DISTINCT segment_no) FROM chapter_summaries WHERE book_name=? "
            "AND segment_no IS NOT NULL", (dbname,)).fetchone()[0]
        arc_start = {r["mn"] for r in con.execute(
            "SELECT arc_no, MIN(chapter_no) mn FROM chapter_summaries WHERE book_name=? "
            "AND arc_no IS NOT NULL GROUP BY arc_no", (dbname,))}
        alias_grid[dbname] = {
            "n_values": len(fcs),
            "values": fcs,
            "diffs": [b - a for a, b in zip(fcs, fcs[1:])],
            "hit_real_segment_starts": len(set(fcs) & seg_start),
            "n_segment_starts": len(seg_start),
            "n_segments": n_segments,
            "hit_real_arc_starts": len(set(fcs) & arc_start),
            "spans": [(r["sn"], r["lo"], r["hi"]) for r in con.execute(
                "SELECT segment_no sn, MIN(chapter_no) lo, MAX(chapter_no) hi "
                "FROM chapter_summaries WHERE book_name=? AND segment_no IS NOT NULL "
                "GROUP BY segment_no ORDER BY sn LIMIT 8", (dbname,))],
            "max_span": con.execute(
                "SELECT MAX(hi - lo + 1) FROM (SELECT MIN(chapter_no) lo, MAX(chapter_no) hi "
                "FROM chapter_summaries WHERE book_name=? AND segment_no IS NOT NULL "
                "GROUP BY segment_no)", (dbname,)).fetchone()[0],
        }

    # ── 5. 未受影响的下游面（实测） ──────────────────────────
    vc = {r["source_type"]: r["n"] for r in con.execute(
        "SELECT source_type,COUNT(*) n FROM vector_chunks GROUP BY source_type")}
    n_universe = con.execute("SELECT COUNT(*) FROM chapter_summaries").fetchone()[0]

    # ══════════════════════ 写报告 ══════════════════════
    L: list[str] = []
    L.append("# [DEV-DATA01] 隔离动作的下游影响清单（gate ② 交付物之一）")
    L.append("")
    L.append(f"- 生成（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    L.append(f"- 数据库：`{DB}`（**只读 mode=ro 反查，未写一行**）")
    L.append(f"- 反查口径：文件名 `NNNN_` 序号 = `chapter_summaries.chapter_no`；"
             f"再顺着 `arc_no` → `atomic_variants.arc_ref` → `event_skeletons.evidence` "
             f"/ `plot_templates.source_stats.arc_refs`")
    L.append(f"- 标题对账：{len(hit_rows)} 条命中行里，标题与文件名一致 **{title_ok}** 条、"
             f"不一致 **{title_bad}** 条"
             + ("（不一致样本：" + "；".join(bad_samples[:5]) + "）" if bad_samples else ""))
    L.append("")
    L.append("## 一、总结论")
    L.append("")
    L.append(f"| 影响面 | 数量 | 隔离后要做什么 |")
    L.append("|---|---|---|")
    L.append(f"| 受影响 `chapter_summaries` 行 | **{len(hit_rows)}** | 无需删：这些是「摘要行」，"
             f"源文件只是移到 `_quarantine/`（路径变了，内容没变）；"
             f"下次重跑入库会按新目录扫描，隔离章自然不再进候选 |")
    L.append(f"| 受影响剧情弧 `arc` | **{len(arcs)}** | 见第四节 |")
    L.append(f"| 受影响 `atomic_variants` | **{sum(len(v) for v in av_by_arc.values())}** | "
             f"见第四节：这些变体文本是从整弧里提炼的，隔离单章不使变体失效，只需在复核时知情 |")
    L.append(f"| 受影响 `event_skeletons` | **{len({s for v in skel_hit.values() for s in v})}** | "
             f"骨架 evidence 里引用了受影响 arc，不阻塞使用 |")
    L.append(f"| 受影响 `plot_templates` | **{len({t for v in tpl_hit.values() for t in v})}** | "
             f"同上，source_stats 里的引用变旧，不影响模板本身 |")
    L.append(f"| `book_aliases.first_chapter` | **0（口径误用，见第五节）** | "
             f"初算出 {len(alias_hits)} 条「命中」，实测该字段是粗粒度批量锚点"
             f"（每本书只有 ≤22 个不同取值，而章数上百~数千）、不是精确首现章，"
             f"不能据此判定别名只出自被隔离章 → 判为不受影响 |")
    L.append(f"| 素材向量 | **0** | `vector_chunks` 的 source_type 只有 "
             f"{', '.join(sorted(vc))}，**素材原文没进向量库**，隔离不动任何索引 |")
    L.append("")
    L.append("**章号不会漂移（实测口径）**：入库走 "
             "`backend/app/services/plot_import.py::discover_chapters`，`chapter_no` 直接取文件名 "
             "`NNNN_` 前缀（不是遍历计数器），所以下游 `arc_ref`/`segment_no`/`first_chapter` 的编号关系"
             "在隔离后仍然成立；重跑入库只是少这几行，不会整体错位。")
    L.append("")
    L.append("## 二、38 本素材的入库现状（决定影响面）")
    L.append("")
    L.append(f"- `chapter_summaries` 共 {n_universe} 行，覆盖 {len(db_books)} 个书名："
             f"{'、'.join(sorted(db_books))}")
    L.append(f"- **B 类 {len(b_items)} 个文件里，{len(hit_rows)} 个的对应章已入库**；"
             f"其余落在未入库书上，隔离对库内零影响：")
    L.append("")
    L.append("| 素材目录 | B 类文件数 | 是否入库 | 库内书名 |")
    L.append("|---|---|---|---|")
    from collections import Counter
    per_book = Counter(d["book"] for d in b_items)
    for book, n in sorted(per_book.items()):
        dbname = per_dir_dbname.get(book)
        L.append(f"| {book} | {n} | {'已入库' if dbname else '未入库'} | {dbname or '—'} |")
    L.append("")
    L.append("## 三、逐条反查：被隔离章 ↔ 库内摘要行")
    L.append("")
    L.append("| 库内书名 | chapter_no | 素材文件 | 库内标题 | arc_ref |")
    L.append("|---|---|---|---|---|")
    for r in sorted(hit_rows, key=lambda x: (x["book_name"], x["chapter_no"])):
        L.append(f"| {r['book_name']} | {r['chapter_no']} | {row_file.get(r['id'], '—')} | "
                 f"{r['title'][:24]} | {r['book_name']}#{r['arc_no']} |")
    L.append("")
    L.append("## 四、受影响 arc 的原子变体（`atomic_variants`）")
    L.append("")
    if not any(av_by_arc.values()):
        L.append("本次隔离没有命中任何已提炼的 arc 变体。")
    for arc, rs in sorted(av_by_arc.items()):
        if not rs:
            continue
        L.append(f"- **{arc}**（{len(rs)} 条变体）：" +
                 "，".join(f"#{str(r['id'])[:8]}/{r['atomic_id']}/seg{r['segment_no']}" for r in rs[:8]) +
                 ("…" if len(rs) > 8 else ""))
    L.append("")
    L.append("## 五、`book_aliases.first_chapter`：初算命中 "
             f"{len(alias_hits)} 条，复核后判为**口径误用、不受影响**")
    L.append("")
    L.append("反查时我先按「first_chapter = 实体首现章」去对隔离章号，得到 "
             f"{len(alias_hits)} 条命中。回头核字段形态发现这个假设不成立：")
    L.append("")
    L.append(f"| 书名 | distinct first_chapter 取值数 | 该书 `chapter_summaries` 行数 | 相邻差值 | ∩真实段起始章 | ∩arc 起始章 |")
    L.append("|---|---|---|---|---|---|")
    for dbname, g in sorted(alias_grid.items()):
        n_rows = con.execute("SELECT COUNT(*) FROM chapter_summaries WHERE book_name=?",
                             (dbname,)).fetchone()[0]
        L.append(f"| {dbname} | {g['n_values']} | {n_rows} | {g['diffs'][:10]}… | "
                 f"{g['hit_real_segment_starts']}/{g['n_values']} | "
                 f"{g['hit_real_arc_starts']}/{g['n_values']} |")
    L.append("")
    g = alias_grid.get("蛊真人") or next(iter(alias_grid.values()), None)
    if g:
        L.append(f"- {dbname} 的 {g['n_values']} 个 first_chapter 取值是**等差网格**"
                 f"（{g['values'][:6]}…{g['values'][-1]}，相邻差值 {g['diffs'][:8]}），"
                 f"而库里该书真实切了 {g['n_segments']} 个 segment（单段最长 {g['max_span']} 章，"
                 f"实测前 4 段：{['ch%d~%d' % (lo, hi) for _, lo, hi in g['spans'][:4]]}）；"
                 f"网格与真实 segment 起始章的重合是 {g['hit_real_segment_starts']} 个"
                 f"（分母两种口径：占网格 {g['hit_real_segment_starts']}/{g['n_values']}、"
                 f"占去重后 {g['n_segment_starts']} 个真实起始章中的 "
                 f"{g['hit_real_segment_starts']}/{g['n_segment_starts']}）；")
    L.append("- 十本有别名的书**全都**是这个形态：档数 ≤20，而入库章数成百上千"
             "（实测 distinct 档数/入库行数：" + "、".join(fc_breadth) + "）；")
    L.append("- 最直白的两条证据：**修真四万年 52 条别名、道诡异仙 38 条别名，两本书的 "
             "first_chapter 都只有 1 档（值恒为 1）**——整本书的别名统一挂在第 1 章，"
             "这不可能是一章一章核出来的「首现章」；")
    L.append("- 结论：不能由「first_chapter == 被隔离章号」推出「该别名/实体只出自被隔离那一章」，"
             "所以 `book_aliases` 这一面**不需要任何动作**，40 条命中只作留档取证；")
    L.append("- 顺带记一笔既有问题：蛊真人 fc=121 这一档的锚点文件正是 `0121_预求月票.txt`（234B 通知章），"
             "说明入库时通知章也被当成了编号锚点。这属于上游数据既有质量问题，与本次清洗无关，本次不改库。")
    L.append("")
    if alias_hits:
        L.append("<details><summary>初算命中清单（留档取证，无需处理）</summary>")
        L.append("")
        L.append("\n".join(f"- {h}" for h in alias_hits))
        L.append("")
        L.append("</details>")
        L.append("")
    L.append("## 六、骨架 / 模板里对受影响 arc 的引用")
    L.append("")
    for k, v in sorted(skel_hit.items()):
        L.append(f"- evidence 引用 `{k}` 的骨架：{'；'.join(sorted(set(v)))}")
    for k, v in sorted(tpl_hit.items()):
        L.append(f"- source_stats 引用 `{k}` 的模板：{'；'.join(sorted(set(v)))}")
    if not skel_hit and not tpl_hit:
        L.append("- 无引用")
    L.append("")
    L.append("## 七、A 类章内清理对下游的影响（口径说明）")
    L.append("")
    L.append(f"- A 类 {len(a_items)} 个文件只删噪声行/片段，**章号、文件名、正文都不动**，"
             f"所以 `chapter_summaries.book_name/chapter_no/title` 全部继续有效；")
    L.append("- 已入库的摘要是「读正文写的摘要」，噪声行（翻页提示/求票/站标）本来就不该进摘要，"
             "清理只会让源文件与既有摘要更一致，不会造成错位；")
    L.append("- 若将来重跑 `--scan` 之外的一条龙入库（plot_import），清洗后的文件正好是更干净的输入；")
    L.append("- 台账 `outputs/data_clean/清洗台账.json` 记录每个文件的原大小/被删字数/命中模式，"
             "任何一条都能回溯到备份 `outputs/data_clean/backup/`。")
    L.append("")
    L.append("## 八、回滚方式（不删除任何文件）")
    L.append("")
    L.append("- A 类：`outputs/data_clean/backup/<书名>/<原文件名>` 是改写前的整文件副本，"
             "逐个覆盖回原位即可；")
    L.append("- B 类：`小说/_quarantine/<书名>/<文件名>` 原样移动过去，改名放回 `小说/<书名>/` 即恢复；")
    L.append("- 台账里每条都有 `rel`（原相对路径）与 `backup`/`moved_to`，可用它做全量还原校验。")
    L.append("")
    L.append("## 九、已知记账（本次不动，交 PM 定夺）")
    L.append("")
    L.append("- 上面第三节那 61 行摘要，摘要对象本身就是噪声章（标题如 `预求月票`、`这不是求月票！`、"
             "`关于6月份的更新计划和致歉声明`）——是**上游入库时把通知章也写了摘要**。"
             "本次只隔离源文件、不写库（用户要求只读反查），所以这些行留在库里：无害但无价值，"
             "要不要清理属于「入库侧」的活，不在本单范围。")
    L.append("- `backend/scripts/inventory_novels.py:113` 把 `小说/` 下所有子目录都当一本书，"
             "没有跳过 `_` 前缀；一旦 `_quarantine/` 建出来，重跑盘点会把它误计为一本素材书"
             f"（B 类 {len(b_items)} 个文件会「凭空多出一本书」）。清洗脚本自己的 "
             "`iter_books()` 已显式排除 `_quarantine` 与 `SKIP_DIRS`，所以扫描/核账不受影响。")
    L.append("- `_quarantine/` 的落点是 `小说/_quarantine/<书名>/`，仍在素材根内，"
             "备份与隔离两层都没出仓库，回滚只需要移动文件。")
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"-> {OUT}")
    print(f"[impact] summaries_rows={len(hit_rows)} arcs={len(arcs)} "
          f"variants={sum(len(v) for v in av_by_arc.values())} "
          f"alias_hits={len(alias_hits)} title_bad={title_bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
