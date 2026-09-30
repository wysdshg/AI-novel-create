# -*- coding: utf-8 -*-
"""七书跨书归并骨架入库（2026-09-19 用户拍板：今天的模板入库试效果）。

75 组 → plot_templates（status='active'）：
- structure 兼容旧前端契约：phases[].beats[].variants[]（variants = 该环的实证弧 = covered_by）；
  另存 structure.skeleton = {seq/repeats/cores/origin} 供程序用（不破坏旧字段）。
- 环 → phase：按位置均分「起/承/转/合」。
- name = 核心环中文名「·」连接（去重、≤80 字）；logline 自动生成。
- 向量索引走 plot_template_crud.index_template（旁路，硅基劣化时静默失败，服务恢复后重跑
  scripts/reindex_plot_templates.py 补齐）。

🔴 写库铁律（MEMORY.md #7）：StaticPool 单连接 + commit 后同连接 wal_checkpoint(PASSIVE) + 跨进程验证。
"""
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

SRC = ROOT / "outputs" / "_atomic_raw" / "merge_flat_七书_crossfirst.json"
PHASES = ["起", "承", "转", "合"]


def split_phases(n: int) -> list[str]:
    """环位均分到 起/承/转/合。"""
    if n <= 0:
        return []
    per = max(1, n // 4)
    out = []
    for i in range(n):
        idx = min(3, i // per) if n >= 4 else min(3, i * 4 // n)
        out.append(PHASES[idx])
    return out


def main() -> int:
    from sqlalchemy.pool import StaticPool
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.orm import PlotTemplateORM
    from app.services import plot_template_crud as crud
    from app.services.plot_template_crud import GLOBAL, SOURCE_TYPE, SOURCE_TYPE_CAST, SOURCE_TYPE_ARCHETYPE
    from app.services import vector_index
    from merge_arcs_crossbook import align_positions

    data = json.loads(SRC.read_text(encoding="utf-8"))
    db_path = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "uri": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine)
    db = Session()

    # 幂等：已入库（status=active 标记）先清掉 + 清其向量块（🔴 只 remove 不重建——
    # 硅基劣化时 index_template 的 embedding 调用会卡死；向量统一等恢复后 reindex）
    old = db.query(PlotTemplateORM).filter(
        PlotTemplateORM.status == "active").all()
    for t in old:
        vector_index.remove_source(db, GLOBAL, SOURCE_TYPE, t.id)
        vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_CAST, t.id)
        vector_index.remove_source(db, GLOBAL, SOURCE_TYPE_ARCHETYPE, t.id)
        db.delete(t)
    if old:
        db.commit()
        print(f"清掉上次入库 {len(old)} 条（幂等重跑）")

    n_ok = 0
    for g in data["groups"]:
        rings = g["rings"]
        n = len(rings)
        if n == 0:
            continue

        # 🔴 2026-09-19 v2：变体带**具体内容**——按环对齐取成员该环的起承转合概括 + 标签。
        #    成员 seq 是骨架子序列（SCS 性质），align_positions（贪心双指针）给出
        #    「成员第 k 个原子 → 骨架第 j 环」映射；summaries/tags 与成员 seq 一一对应。
        covers: list[list[dict]] = [[] for _ in range(n)]
        for m in g["members"]:
            pos = align_positions(m["seq"], g["seq"])
            sums = m.get("summaries") or []
            tgs = m.get("tags") or []
            for k, pl in enumerate(pos):
                for j in pl:
                    covers[j].append({
                        "src": m["book"],
                        "how": f"{m['arc_name']}（c{m['ch_lo']}~{m['ch_hi']}）",
                        "desc": (sums[k] if k < len(sums) else "") or "",
                        "tags": (tgs[k] if k < len(tgs) else []) or [],
                    })

        phases = split_phases(n)
        seen: dict[str, list] = {}
        for i, ph in enumerate(phases):
            seen.setdefault(ph, []).append(i)
        phases_struct = []
        for ph in PHASES:
            if ph not in seen:
                continue
            beats = []
            for i in seen[ph]:
                r = rings[i]
                beats.append({
                    "beat": f"{r['atomic_id']} {r['name']}" + (f"×{r['repeat']}" if r["repeat"] > 1 else ""),
                    "variants": covers[i],
                })
            phases_struct.append({"phase": ph, "beats": beats})

        cores = g.get("cores") or []
        core_names = []
        for r in rings:
            if r["atomic_id"] in cores and r["name"] not in core_names:
                core_names.append(r["name"])
        name = "·".join(core_names)[:80] or f"跨书骨架组#{g['gid']}"

        nb = len(g["books"])
        logline = (f"{nb} 本书 {g['n_members']} 条弧实证的通用情节骨架："
                   f"{' → '.join(r['name'] for r in rings)}。")

        structure = {
            "phases": phases_struct,
            "skeleton": {"seq": g["seq"], "repeats": g["repeats"], "cores": cores,
                         "origin": "atomic_merge_v1"},
        }
        t = PlotTemplateORM(
            id=uuid.uuid4().hex,
            name=name,
            scale="arc",
            genre_tags=["原子骨架", f"{nb}书跨书", "v3生成"],
            logline=logline,
            structure=structure,
            pitfalls=[],
            rhythm=None,
            source_stats={"books": nb, "book_names": g["books"],
                          "n_members": g["n_members"], "n_references": g["n_references"],
                          "origin": "atomic_merge_v1", "src_json": SRC.name},
            status="active",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.add(t)
        db.flush()
        # 🔴 向量索引暂不建（硅基劣化中 embedding 会卡死）——服务恢复后跑
        #    scripts/reindex_plot_templates.py 统一回填
        n_ok += 1

    db.commit()
    # 🔴 WAL checkpoint：同连接收尾，保证跨进程可见
    from sqlalchemy import text
    db.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
    db.close()
    print(f"✅ 入库 {n_ok} 条（status=active，向量待 reindex）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
