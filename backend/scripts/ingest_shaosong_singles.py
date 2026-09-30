# -*- coding: utf-8 -*-
"""绍宋单书骨架入库（2026-09-21 用户拍板方案 A）。

把绍宋的 25 条 v3 弧（+324 原子变体）入库 plot_templates，作为**单书组**模板。

与 `ingest_merge_skeletons.py` 的关系：
- 复用同一套 structure 契约（phases[].beats[].variants[] + skeleton），前端零改动可见
- 🔴 **只 append**：不动现有 75 条跨书组；幂等清理时只删 `source_stats.origin == "shaosong_singles_v1"` 的条
- 向量索引不在此建（跑 scripts/reindex_plot_templates.py 统一回填）

🔴 写库铁律（MEMORY.md #7）：StaticPool 单连接 + commit 后同连接 wal_checkpoint(PASSIVE) + 跨进程验证。
"""
import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

BOOK = "绍宋"
ORIGIN = "shaosong_singles_v1"
PHASES = ["起", "承", "转", "合"]


def split_phases(n: int) -> list[str]:
    if n <= 0:
        return []
    per = max(1, n // 4)
    out = []
    for i in range(n):
        idx = min(3, i // per) if n >= 4 else min(3, i * 4 // n)
        out.append(PHASES[idx])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印将要入库的 25 条，不写库")
    args = ap.parse_args()

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.models.orm import PlotTemplateORM
    from app.core import database as dbmod

    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    # 取绍宋全部弧的原子变体（按弧号、段号排序）
    rows = db.execute(text(
        "SELECT arc_ref, segment_no, atomic_id, text, tags FROM atomic_variants "
        "WHERE book_name = :b ORDER BY arc_ref, segment_no"), {"b": BOOK}).fetchall()
    if not rows:
        print("❌ 库内无绍宋原子变体（先跑 label_atomic_variants.py --book 绍宋）")
        return 2

    # 弧名 + 章范围（从 chapter_summaries 取）
    arc_info = {}
    for arc_no, arc_name, lo, hi in db.execute(text(
            "SELECT arc_no, MAX(arc_name), MIN(chapter_no), MAX(chapter_no) "
            "FROM chapter_summaries WHERE book_name = :b AND arc_no IS NOT NULL "
            "GROUP BY arc_no ORDER BY arc_no"), {"b": BOOK}).fetchall():
        arc_info[int(arc_no)] = {"name": str(arc_name or f"弧{arc_no}"),
                                 "ch_lo": int(lo), "ch_hi": int(hi)}

    by_arc: dict[int, list] = {}
    for arc_ref, seg, atomic_id, txt, tags in rows:
        try:
            no = int(str(arc_ref).split("#")[1])
        except (IndexError, ValueError):
            continue
        by_arc.setdefault(no, []).append({
            "seq": int(seg), "atomic_id": str(atomic_id or ""),
            "text": str(txt or ""), "tags": list(tags or []),
        })

    print(f"[load] 绍宋 {len(by_arc)} 条弧 / {len(rows)} 个原子变体")

    if not args.dry_run:
        # 幂等：只清本脚本上次入库的（不动 75 条跨书组）
        old = [t for t in db.query(PlotTemplateORM).filter_by(status="active").all()
               if (t.source_stats or {}).get("origin") == ORIGIN]
        for t in old:
            db.delete(t)
        if old:
            db.commit()
            print(f"[idem] 清掉上次入库 {len(old)} 条（幂等重跑）")

    n_ok = 0
    for arc_no in sorted(by_arc):
        vs = sorted(by_arc[arc_no], key=lambda x: x["seq"])
        n = len(vs)
        if n == 0:
            continue
        info = arc_info.get(arc_no, {"name": f"弧{arc_no}", "ch_lo": 0, "ch_hi": 0})
        phases = split_phases(n)
        seen: dict[str, list] = {}
        for i, ph in enumerate(phases):
            seen.setdefault(ph, []).append(i)

        def beat_label(v):
            return v["atomic_id"] or "?"

        phases_struct = []
        for ph in PHASES:
            if ph not in seen:
                continue
            beats = []
            for i in seen[ph]:
                v = vs[i]
                beats.append({
                    "beat": beat_label(v),
                    "variants": [{
                        "src": BOOK,
                        "how": f"{info['name']}（c{info['ch_lo']}~{info['ch_hi']}）",
                        "desc": v["text"],
                        "tags": v["tags"],
                    }],
                })
            phases_struct.append({"phase": ph, "beats": beats})

        # 核心 = 序列里出现 ≥2 次的原子；没有则取首个非过场类（E/G）
        from collections import Counter
        cnt = Counter(v["atomic_id"] for v in vs if v["atomic_id"])
        cores = [a for a, c in cnt.most_common() if c >= 2]
        if not cores:
            cores = [vs[0]["atomic_id"]] if vs[0]["atomic_id"] else []

        name = info["name"][:80]
        logline = (f"《{BOOK}》弧「{info['name']}」的原子序列（{info['ch_lo']}~{info['ch_hi']} 共 "
                   f"{info['ch_hi'] - info['ch_lo'] + 1} 章 / {n} 个原子）："
                   f"{' → '.join(beat_label(v) for v in vs)}。")
        structure = {
            "phases": phases_struct,
            "skeleton": {"seq": [v["seq"] for v in vs],
                         "repeats": [1] * n, "cores": cores, "origin": ORIGIN},
        }
        if args.dry_run:
            print(f"  · 弧{arc_no:>2} {name}｜{n} 原子｜core={cores[:3]}｜"
                  f"{' → '.join(beat_label(v) for v in vs)[:90]}")
            n_ok += 1
            continue

        t = PlotTemplateORM(
            id=uuid.uuid4().hex,
            name=name,
            scale="arc",
            genre_tags=["原子骨架", "1书单书", "历史穿越", "v3生成"],
            logline=logline,
            structure=structure,
            pitfalls=[],
            rhythm=None,
            source_stats={"books": 1, "book_names": [BOOK], "n_members": 1,
                          "n_atomic_variants": n, "origin": ORIGIN,
                          "arc_no": arc_no, "ch_lo": info["ch_lo"], "ch_hi": info["ch_hi"]},
            status="active",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.add(t)
        db.flush()
        n_ok += 1

    if args.dry_run:
        print(f"\n[dry-run] 将入库 {n_ok} 条（未写库）")
        return 0

    db.commit()
    # 🔴 写库铁律：同连接 checkpoint，确保跨进程可见
    db.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
    db.commit()
    tot = db.query(PlotTemplateORM).filter_by(status="active").count()
    print(f"\n[done] 绍宋单书入库 {n_ok} 条；plot_templates active 合计 {tot} 条")
    print("[next] 跑 scripts/reindex_plot_templates.py 回填向量（前端语义检索需要）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
