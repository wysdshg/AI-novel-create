# -*- coding: utf-8 -*-
"""新增可复用清理脚本：按**质量**清理（cast=0 = 错误模板），默认 dry-run。

教训（2026-09-18）：此前按"同 arc_refs 保留最早"删，判据错了——
正确判据是**模板本身是否合格**（cast 槽位为 0 = 缺角色功能位 = 错误模板），
且重名时保留**最晚**生成的（新 prompt 更好）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, r"E:\AI小说创作\backend")
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def bad_reasons(ss: dict, st: dict) -> list[str]:
    why = []
    cast = st.get("cast") or []
    if not cast:
        why.append("cast=0（缺角色功能位）")
    phases = st.get("phases") or []
    if not phases:
        why.append("无 phases")
    return why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=None, help="只处理某本书（如 太荒吞天诀）")
    ap.add_argument("--status", default="draft")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run 只列出）")
    a = ap.parse_args()

    engine = create_engine("sqlite:///C:/Users/w3013/.ai_novel/data/novel_agent.db",
                           poolclass=StaticPool, connect_args={"check_same_thread": False})
    db = sessionmaker(bind=engine)()
    from app.core.database import init_db
    init_db()
    from app.models.orm import PlotTemplateORM
    from app.services import plot_template_crud as tpl

    rows = db.query(PlotTemplateORM).all()
    hits = []
    for r in rows:
        if a.status and (r.status or "") != a.status:
            continue
        try:
            ss = json.loads(r.source_stats) if isinstance(r.source_stats, str) else (r.source_stats or {})
            st_ = json.loads(r.structure) if isinstance(r.structure, str) else (r.structure or {})
        except Exception:
            continue
        if a.book and a.book not in (ss.get("book_names") or []):
            continue
        why = bad_reasons(ss, st_)
        if why:
            hits.append((r, why))

    print(f"不合格模板 {len(hits)} 条" + ("（dry-run，加 --apply 才真删）" if not a.apply else "（执行删除）"))
    for r, why in hits[:15]:
        print(f"   - {r.name} | {'; '.join(why)} | {str(r.created_at)[:19]}")
    if a.apply and hits:
        n = 0
        for r, _ in hits:
            if tpl.delete(db, r.id):   # 连向量一起清
                n += 1
        db.commit()
        db.connection().exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
        print(f"已删除 {n} 条；剩余模板 {db.query(PlotTemplateORM).count()}")
    db.close()


if __name__ == "__main__":
    main()
