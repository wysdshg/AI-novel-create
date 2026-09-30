# -*- coding: utf-8 -*-
"""退役 / 备份旧 `plot_templates`（默认 **dry-run，只导出备份不写库**）。

背景（2026-09-19 用户提问）：「库里那些模板要不要直接清空移到其他地方去，等确定新方案模板的效果后彻底删？」

🔴 **结论：不要清表，改用「导出备份 + 归档标记」。** 原因：
1. `plot_templates` 挂着 **1854 个 vector_chunks**（plot_template 842 / plot_cast 506 / char_archetype 506）——
   删表就产生 1854 个孤儿块，还得连向量一起清；
2. 它们是**唯一的对照基线**：新骨架方案好不好，得跟旧模板比才知道（docs/10 §9 明确要保留作回标素材）；
3. 清表不可逆；而「归档 + 检索排除」**一条 UPDATE 就能回滚**。

所以本脚本做两件事：
- **默认（dry-run）**：只**导出全量 JSON 备份**到 `outputs/_backup/`，并打印影响范围；
- `--apply`：把命中的模板 `status` 置为 `archived`（配合 `plot_template_crud.search` 的排除逻辑，检索即看不见）。

用法（在 backend/ 下执行）：
    ..\\.venv\\Scripts\\python.exe scripts/retire_plot_templates.py                  # 只备份 + 报影响范围
    ..\\.venv\\Scripts\\python.exe scripts/retire_plot_templates.py --apply          # 真归档（可回滚）
    ..\\.venv\\Scripts\\python.exe scripts/retire_plot_templates.py --unarchive      # 回滚：archived → draft
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import create_engine, text                        # noqa: E402
from sqlalchemy.orm import sessionmaker                           # noqa: E402
from sqlalchemy.pool import StaticPool                            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="旧模板退役/备份（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真把命中模板标 archived（默认只备份）")
    ap.add_argument("--unarchive", action="store_true", help="回滚：archived → draft")
    ap.add_argument("--status", default=None, help="只处理该状态（默认全部）")
    ap.add_argument("--outdir", default=None, help="备份目录（默认 项目/outputs/_backup）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    outdir = Path(args.outdir) if args.outdir else root / "outputs" / "_backup"
    outdir.mkdir(parents=True, exist_ok=True)

    import app.core.database as dbmod
    from app.models.orm import PlotTemplateORM, VectorChunkORM

    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    dbmod.init_db()

    from app.services import plot_template_crud as tpl
    TPL_TYPES = (tpl.SOURCE_TYPE, tpl.SOURCE_TYPE_CAST, tpl.SOURCE_TYPE_ARCHETYPE)

    rows = db.query(PlotTemplateORM).all()
    if args.status:
        rows = [r for r in rows if (r.status or "") == args.status]
    print(f"命中模板 {len(rows)} / {db.query(PlotTemplateORM).count()} 条")

    from collections import Counter
    dist = Counter((r.status or "?") for r in db.query(PlotTemplateORM).all())
    print(f"当前状态分布：{dict(dist)}")

    ids = {r.id for r in rows}
    n_vec = (db.query(VectorChunkORM)
             .filter(VectorChunkORM.source_type.in_(TPL_TYPES)).count())
    n_vec_hit = sum(1 for st, sid in db.query(VectorChunkORM.source_type,
                                              VectorChunkORM.source_id)
                    .filter(VectorChunkORM.source_type.in_(TPL_TYPES)).all()
                    if sid in ids)
    print(f"🔴 影响范围：这 {len(rows)} 条模板挂着 **{n_vec_hit} 个向量块**"
          f"（全库三路块共 {n_vec}）→ 删表会产生同样数量的孤儿块")

    # ---- 导出全量备份（永远做，只读） ----
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = outdir / f"plot_templates_backup_{len(rows)}_{stamp}.json"
    dump = []
    for r in rows:
        dump.append({
            "id": r.id, "name": r.name, "logline": getattr(r, "logline", None),
            "scale": getattr(r, "scale", None), "status": r.status,
            "structure": r.structure, "source_stats": r.source_stats,
            "genre_tags": getattr(r, "genre_tags", None),
            "created_at": str(getattr(r, "created_at", "")),
        })
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 已导出备份 → {out}（{out.stat().st_size / 1024:.0f} KB，{len(dump)} 条）")

    if args.unarchive:
        n = (db.query(PlotTemplateORM)
             .filter(PlotTemplateORM.status == "archived")
             .update({"status": "draft"}, synchronize_session=False))
        db.commit()
        db.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
        print(f"↩️ 已回滚 {n} 条：archived → draft")
        db.close()
        return 0

    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 才把上面这些模板标为 archived。")
        print("   · 归档后仍需 `plot_template_crud.search` 跳过 archived（已加该过滤）才等于「退出检索」")
        print("   · 回滚：--unarchive")
        db.close()
        return 0

    n = (db.query(PlotTemplateORM)
         .filter(PlotTemplateORM.id.in_(ids))
         .update({"status": "archived"}, synchronize_session=False))
    db.commit()
    db.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
    print(f"✅ 已归档 {n} 条（status=archived）。回滚用 --unarchive")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
