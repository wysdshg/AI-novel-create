# -*- coding: utf-8 -*-
"""P1 跨进程验收（docs/10 §8 P1）：只读连接核对标注结果。

检查项：
1. atomic_variants 行数 == 本批标注段数；
2. 每条 variant 的 atomic_id 都在 atomic_events 内（越表 = 0）；
3. 走法文本长度分布（要求 100~160 字）；
4. 🔴 **专名泄漏检测**：用 `book_aliases` 里该书的**真实原名表**（len≥2）逐条子串匹配
   —— 比正则靠谱得多：模型只要把「项峰」「西夏宫」「熔星草」写进走法就会被抓到；
5. 每个 POC 弧的段覆盖情况（无遗漏、无多余）。

用法：
    ..\\.venv\\Scripts\\python.exe scripts/verify_atomic_p1.py
"""
import sqlite3
import sys

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
URI = f"file:{DB}?mode=ro"

POC_ARCS = [("太荒吞天诀#22", 4), ("九星霸体诀#5", 4), ("北派盗墓笔记#25", 3)]
TEXT_MIN, TEXT_MAX = 100, 160


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    con = sqlite3.connect(URI, uri=True)
    ok_all = True
    try:
        n_var = con.execute("SELECT COUNT(*) FROM atomic_variants").fetchone()[0]
        n_seg = sum(n for _, n in POC_ARCS)
        mark = "✅" if n_var == n_seg else "❌"
        if n_var != n_seg:
            ok_all = False
        print("===== 1. 行数对账 =====")
        print(f"  {mark} atomic_variants = {n_var}｜POC 标注段数 = {n_seg}")

        print("\n===== 2. 逐弧段覆盖 =====")
        for arc_ref, exp_n in POC_ARCS:
            rows = con.execute(
                "SELECT segment_no FROM atomic_variants WHERE arc_ref=? ORDER BY segment_no",
                (arc_ref,)).fetchall()
            nos = [r[0] for r in rows]
            m = "✅" if len(nos) == exp_n else "❌"
            if len(nos) != exp_n:
                ok_all = False
            print(f"  {m} {arc_ref:<18} 段 {nos}｜预期 {exp_n} 段")

        print("\n===== 3. atomic_id 合法性 =====")
        bad = con.execute("""
            SELECT v.arc_ref, v.segment_no, v.atomic_id FROM atomic_variants v
            LEFT JOIN atomic_events e ON e.id = v.atomic_id
            WHERE e.id IS NULL""").fetchall()
        if bad:
            ok_all = False
            print(f"  ❌ {len(bad)} 条越表: {bad}")
        else:
            print("  ✅ 全部 atomic_id 均存在于 atomic_events（越表 0 条）")

        print("\n===== 4. 走法长度分布 =====")
        rows = con.execute(
            "SELECT arc_ref, segment_no, LENGTH(text) FROM atomic_variants "
            "ORDER BY arc_ref, segment_no").fetchall()
        lens = [r[2] for r in rows]
        out = [(r[0], r[1], r[2]) for r in rows if not (TEXT_MIN <= r[2] <= TEXT_MAX)]
        print(f"  n={len(lens)}｜min={min(lens)}｜max={max(lens)}｜"
              f"均值={sum(lens) / len(lens):.1f}")
        if out:
            print(f"  ⚠️ {len(out)} 条越界（{TEXT_MIN}~{TEXT_MAX}）:")
            for a, s, n in out:
                print(f"     - {a} 段{s}: {n} 字（超 {n - TEXT_MAX} 字）")
        else:
            print(f"  ✅ 全部落在 {TEXT_MIN}~{TEXT_MAX} 字")

        print("\n===== 5. 专名泄漏检测（依据 book_aliases 真实原名表）=====")
        total_leak = 0
        for arc_ref, _ in POC_ARCS:
            book = arc_ref.split("#")[0]
            originals = [r[0] for r in con.execute(
                "SELECT DISTINCT original FROM book_aliases "
                "WHERE book_name=? AND LENGTH(original)>=2", (book,)).fetchall()]
            rows = con.execute(
                "SELECT segment_no, text, tags FROM atomic_variants WHERE arc_ref=?",
                (arc_ref,)).fetchall()
            hits = []
            for seg, txt, tags in rows:
                blob = f"{txt}{tags or ''}"
                found = sorted({o for o in originals if o in blob}, key=len, reverse=True)
                if found:
                    hits.append((seg, found))
            total_leak += len(hits)
            print(f"  {arc_ref}（原名表 {len(originals)} 条）："
                  f"{'✅ 无泄漏' if not hits else f'❌ {len(hits)} 段有泄漏'}")
            for seg, found in hits:
                print(f"     - 段{seg} 命中: {found}")
        if total_leak:
            ok_all = False

        print("\n===== 6. 归属一览 =====")
        for arc_ref, _ in POC_ARCS:
            rows = con.execute("""
                SELECT v.segment_no, v.atomic_id, e.name, e.category_id
                FROM atomic_variants v LEFT JOIN atomic_events e ON e.id = v.atomic_id
                WHERE v.arc_ref=? ORDER BY v.segment_no""", (arc_ref,)).fetchall()
            print(f"  {arc_ref}: " + " → ".join(f"段{s}({a} {n})" for s, a, n, _ in rows))

        print("\n===== 结论 =====")
        print("✅ P1 验收通过" if ok_all else "❌ P1 验收未通过（见上方 ❌ / ⚠️ 项）")
        return 0 if ok_all else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
