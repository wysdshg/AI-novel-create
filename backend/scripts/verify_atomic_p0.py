# -*- coding: utf-8 -*-
"""P0 跨进程验收：用**只读连接**核对 4 张原子表是否真落盘（docs/10 §8 P0）。

要点（本项目硬事实）：
- 一律 `file:...?mode=ro` URI 打开，**不碰 -wal / -shm**；
- 独立进程运行，验证「writer 进程写完 + checkpoint 后，另一个进程能读到」；
- 同时打印 plot_templates 行数，作为「没动旧模板」的证据。

用法：
    ..\\.venv\\Scripts\\python.exe scripts/verify_atomic_p0.py
"""
import sqlite3
import sys

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
URI = f"file:{DB}?mode=ro"

EXPECT = {
    "atomic_categories": 8,
    # 64 = 60 active + 4 candidate（含 2026-09-18 P1 抽查后：G07 转正 active、新增 B08 candidate）
    "atomic_events": 64,
    "event_skeletons": 6,
}
# P1 会往 atomic_variants 写标注结果（P0 阶段本应为 0）→ 只报告不判定，
# 否则跑完 P1 再回来跑 P0 验收会误报失败。
INFO_TABLES = ["atomic_variants"]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    con = sqlite3.connect(URI, uri=True)
    try:
        exists = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        print("===== 1. sqlite_master 表存在性（只读连接）=====")
        ok_all = True
        for t in list(EXPECT) + INFO_TABLES:
            if t in exists:
                print(f"  ✅ {t}")
            else:
                print(f"  ❌ {t}  —— 不存在")
                ok_all = False

        print("\n===== 2. 行数对账 =====")
        for t, exp in EXPECT.items():
            if t not in exists:
                continue
            got = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            mark = "✅" if got == exp else "❌"
            if got != exp:
                ok_all = False
            print(f"  {mark} {t:<20} 实测 {got:>3}｜预期 {exp:>3}")

        for t in INFO_TABLES:
            if t not in exists:
                print(f"  ℹ️ {t} 不存在")
                continue
            got = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            tag = "P0 阶段应为 0" if got == 0 else "P1 已写入，属正常"
            print(f"  ℹ️ {t:<20} 实测 {got:>3}（{tag}）")

        print("\n===== 3. atomic_events 状态分布 =====")
        if "atomic_events" in exists:
            for st, n in con.execute(
                    "SELECT status, COUNT(*) FROM atomic_events GROUP BY status ORDER BY status"):
                print(f"  {st:<12} {n}")
            n_core = con.execute(
                "SELECT COUNT(*) FROM atomic_events WHERE is_core_capable=1").fetchone()[0]
            n_dom = con.execute(
                "SELECT COUNT(*) FROM atomic_events WHERE domain='general'").fetchone()[0]
            print(f"  is_core_capable=1: {n_core}｜domain=general: {n_dom}")

        print("\n===== 4. 分类计数（A~H）=====")
        if {"atomic_categories", "atomic_events"} <= exists:
            rows = con.execute("""
                SELECT c.id, c.name, c.sort_order, COUNT(e.id),
                       SUM(CASE WHEN e.status='active' THEN 1 ELSE 0 END)
                FROM atomic_categories c LEFT JOIN atomic_events e ON e.category_id=c.id
                GROUP BY c.id ORDER BY c.sort_order""").fetchall()
            tot = 0
            for cid, cname, so, cnt, act in rows:
                tot += cnt
                print(f"  {cid} {cname:<4} sort={so:<3} 原子 {cnt:>2}（active {act}）")
            print(f"  合计 {tot}")

        print("\n===== 5. 骨架 =====")
        if "event_skeletons" in exists:
            import json
            for sid, name, core, steps in con.execute(
                    "SELECT id, name, core_atomic, steps FROM event_skeletons ORDER BY id"):
                try:
                    st = json.loads(steps)
                    req = sum(1 for s in st if s.get("required"))
                    print(f"  {sid} {name:<8} 核心={core} 环节={len(st)}（必需 {req}）")
                except Exception as e:  # noqa: BLE001
                    print(f"  {sid} {name}: steps 解析失败 {e}")

        print("\n===== 6. 未触碰的证据 =====")
        if "plot_templates" in exists:
            n = con.execute("SELECT COUNT(*) FROM plot_templates").fetchone()[0]
            print(f"  plot_templates 行数: {n}（docs/10 §9：保留不动，作回标素材）")
        else:
            print("  plot_templates 不存在")

        print("\n===== 结论 =====")
        print("✅ P0 验收通过" if ok_all else "❌ P0 验收未通过（见上方 ❌ 项）")
        return 0 if ok_all else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
