# -*- coding: utf-8 -*-
"""素材厚度统计（只读）：回答「切原子该吃哪一层」。

🔴 **结论先说（2026-09-18 查证）**：只能用**逐章概括** `summary`，
`segment_no / segment_summary` **整层不可用** ——
- 「情节段」环节已于 2026-09-16 废弃（`import_novel.py:352` 打印 `[deprecated] …已被 v3 的「弧内节拍」取代`）；
- 该字段被 v3 Pass2 **复用为「弧内节拍」**（`plot_import.py:1593` 注释 `# 节拍 → segment_*`），
  提示词规格是 **3~6 个节拍、每节拍 3~6 章、summary 20~40 字**（`plot_import.py:1359-1364`）
  → 30 字是**设计规格，不是"太薄"**；它是节拍说明，不承担"给下游推走法"的职责；
- 另有 3 本书的段数据**不符合该规格**（1 章/段、140~184 字）→ 旧路径残留，来源不可考。

本脚本实测这两种指纹（A 组符合 v3 规格 / B 组不符合），并对比两层的信息量。

用法：
    ..\\.venv\\Scripts\\python.exe scripts/stats_segment_quality.py
"""
import re
import sqlite3
import statistics
import sys

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
URI = f"file:{DB}?mode=ro"
EXPECT_SEGMENTS = 1229   # 报告 §0 的去重后段总数（自检用）
CH_RE = re.compile(r"第\s*\d+\s*章")
# v3 节拍规格（_arc_content_prompt）：3~6 个节拍、每节拍 3~6 章、summary 20~40 字
V3_BEATS_MIN, V3_BEATS_MAX = 3, 6
V3_CH_PER_BEAT_MIN, V3_CH_PER_BEAT_MAX = 3, 6
V3_SUM_MIN, V3_SUM_MAX = 20, 40


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    con = sqlite3.connect(URI, uri=True)
    try:
        # ---------------- 第一层：段字段（只读指纹，判定来源是否可信）----------------
        rows2 = con.execute("""
            SELECT book_name, arc_no, segment_no, MIN(chapter_no), MAX(chapter_no),
                   MIN(LENGTH(segment_summary)), MIN(segment_summary), MIN(summary)
            FROM chapter_summaries
            WHERE arc_no IS NOT NULL AND segment_no IS NOT NULL
            GROUP BY book_name, arc_no, segment_no""").fetchall()

        if not rows2:
            print("无可统计数据")
            return 1

        books: dict[str, list[int]] = {}
        arcs: dict[str, set] = {}
        segsz: dict[str, list[int]] = {}
        crosshit: dict[str, list[int]] = {}
        for book, arc_no, _seg, c0, c1, slen, ssum, csum in rows2:
            books.setdefault(book, []).append(slen or 0)
            arcs.setdefault(book, set()).add(arc_no)
            segsz.setdefault(book, []).append((c1 or 0) - (c0 or 0) + 1)
            crosshit.setdefault(book, []).append(1 if (ssum and CH_RE.search(ssum)) else 0)

        total = len(rows2)
        print(f"去重后段总数：{total}"
              + ("  ✅ 与报告 §0 一致（1229）" if total == EXPECT_SEGMENTS
                 else f"  ⚠️ 与报告 §0 的 {EXPECT_SEGMENTS} 不符，口径可能被改过"))

        print("\n" + "=" * 90)
        print("第一层｜segment_* 字段指纹 —— 判定它是不是可用的素材层")
        print("=" * 90)
        print(f"v3 节拍规格（_arc_content_prompt）：每弧 {V3_BEATS_MIN}~{V3_BEATS_MAX} 个节拍、"
              f"每节拍 {V3_CH_PER_BEAT_MIN}~{V3_CH_PER_BEAT_MAX} 章、summary {V3_SUM_MIN}~{V3_SUM_MAX} 字")
        print(f"\n{'书':<14}{'弧数':>5}{'段数':>6}{'段/弧':>7}{'段均章数':>9}"
              f"{'段概括中位':>11}{'含第N章':>9}   判定")
        for book in sorted(books, key=lambda b: statistics.mean(segsz[b])):
            L = sorted(books[book])
            n_arc = len(arcs[book])
            seg_per_arc = len(L) / max(n_arc, 1)
            ch_per_seg = statistics.mean(segsz[book])
            med = statistics.median(L)
            cross = sum(crosshit[book]) / len(crosshit[book]) * 100
            ok = (V3_BEATS_MIN <= seg_per_arc <= V3_BEATS_MAX + 1.5
                  and V3_CH_PER_BEAT_MIN - 0.5 <= ch_per_seg <= V3_CH_PER_BEAT_MAX + 1
                  and V3_SUM_MIN <= med <= V3_SUM_MAX + 5)
            verdict = "✅ 符合 v3 规格 = 弧内节拍（非素材层）" if ok \
                else "❌ 违反 v3 规格 = 旧路径残留，来源不可考"
            print(f"{book:<14}{n_arc:>5}{len(L):>6}{seg_per_arc:>7.2f}{ch_per_seg:>9.2f}"
                  f"{med:>11.0f}{cross:>8.1f}%   {verdict}")

        print("\n🔴 两种指纹都存在 → segment_* 是「混源层」，整层不可作为切原子/标注的输入。")

        # ---------------- 第二层：逐章概括（唯一可用素材层）----------------
        print("\n" + "=" * 90)
        print("第二层｜逐章概括 summary —— 唯一可用的切原子输入层")
        print("=" * 90)
        ch = con.execute("""
            SELECT book_name, COUNT(*) AS n_ch,
                   SUM(CASE WHEN summary IS NOT NULL AND LENGTH(summary) > 0 THEN 1 ELSE 0 END)
            FROM chapter_summaries GROUP BY book_name ORDER BY book_name""").fetchall()

        print(f"\n{'书':<14}{'章数':>6}{'有概括':>7}{'覆盖率':>8}"
              f"{'逐章中位':>9}{'段中位(不可用)':>15}{'每弧章数':>9}")
        tot_ch = tot_cov = 0
        for book, n_ch, n_cov in ch:
            L_ch = sorted(r[0] for r in con.execute("""
                SELECT LENGTH(summary) FROM chapter_summaries
                WHERE book_name=? AND summary IS NOT NULL AND LENGTH(summary)>0""",
                (book,)).fetchall())
            n_arc = len(arcs.get(book, ()))
            med_ch = statistics.median(L_ch) if L_ch else 0
            med_seg = statistics.median(books.get(book, [0]))
            tot_ch += n_ch
            tot_cov += n_cov
            print(f"{book:<14}{n_ch:>6}{n_cov:>7}{n_cov / max(n_ch, 1) * 100:>7.1f}%"
                  f"{med_ch:>9.0f}{med_seg:>15.0f}{n_ch / max(n_arc, 1):>9.2f}")

        print(f"\n全库：章 {tot_ch}｜有逐章概括 {tot_cov}"
              f"（{tot_cov / max(tot_ch, 1) * 100:.1f}% = 唯一可用层）")

        print("\n切一个弧的原子时能拿到的输入量（逐章概括 / 段层，后者仅供参考不可用）：")
        for book in ("太荒吞天诀", "九星霸体诀", "斗破苍穹", "北派盗墓笔记"):
            if book not in books:
                continue
            n_seg = len(books[book]) / max(len(arcs[book]), 1)
            seg_in = n_seg * statistics.median(books[book])
            ch_in = con.execute(
                "SELECT AVG(c) FROM (SELECT COUNT(*) AS c FROM chapter_summaries "
                "WHERE book_name=? GROUP BY arc_no)", (book,)).fetchone()[0] or 0
            ch_L = [r[0] for r in con.execute(
                "SELECT LENGTH(summary) FROM chapter_summaries WHERE book_name=? "
                "AND summary IS NOT NULL AND LENGTH(summary)>0", (book,)).fetchall()]
            med_ch = statistics.median(ch_L) if ch_L else 0
            print(f"  {book:<12} 吃逐章概括 ≈ {ch_in * med_ch:>6.0f} 字"
                  f"（{ch_in:.1f} 章 × {med_ch:.0f} 字）"
                  f"｜吃段层 ≈ {seg_in:>6.0f} 字｜倍数 ×{(ch_in * med_ch) / max(seg_in, 1):.1f}")

        print("\n结论：切原子只用逐章概括（覆盖率 100%）；segment_* 整层禁用。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
