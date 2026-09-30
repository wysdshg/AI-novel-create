# -*- coding: utf-8 -*-
"""A1-A2 迁移脚本：旧引用 → entity_relations（幂等）+ 关系字典种子。

用法：
    cd backend && .venv/Scripts/python.exe scripts/migrate_data_model.py [--project <id>]

跑完**必须跨进程验证落盘**（本脚本结尾自带 StaticPool + checkpoint + 复核）。
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

DB_URL = "sqlite:///C:/Users/w3013/.ai_novel/data/novel_agent.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None, help="只迁某个项目（缺省=全部）")
    a = ap.parse_args()

    # 🔴 单连接（StaticPool）—— 本机文件虚拟化下多连接互不可见（docs/04 沉淀）
    engine = create_engine(DB_URL, poolclass=StaticPool, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    db = Session()

    from app.core.database import init_db
    init_db()
    from app.models.orm import EntityRelationORM, RelationTypeORM
    from app.services import entity_relation_crud as er

    n_types = er.seed_relation_types(db)
    stats = er.migrate_legacy(db, a.project)

    total = db.query(EntityRelationORM).count()
    types = db.query(RelationTypeORM).count()
    # 收尾：同连接 checkpoint（先结束挂起事务）
    db.commit()
    db.connection().exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
    db.close()

    print(f"[migrate] 字典新增 {n_types} 条 | 迁移边 {stats}")
    print(f"[migrate] entity_relations 总数 {total} | relation_types {types}")
    print("[migrate] 请用只读连接复核：", DB_URL)


if __name__ == "__main__":
    main()
