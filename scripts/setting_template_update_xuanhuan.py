# -*- coding: utf-8 -*-
"""更新玄幻小说设定模板 content（境界顺序统一 + 经济体系重构）。幂等：按 name 覆盖。"""
import json
import sqlite3
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
NAME = "玄幻小说设定"
SRC = "E:/AI小说创作/outputs/setting-templates/玄幻小说设定.md"

content = open(SRC, encoding="utf-8").read()
engine = create_engine(f"sqlite:///{DB}", poolclass=StaticPool,
                       connect_args={"check_same_thread": False})
s = sessionmaker(bind=engine)()
now = datetime.utcnow().isoformat(sep=" ")
r = s.execute(text("UPDATE setting_templates SET content=:c, updated_at=:u WHERE name=:n"),
              {"c": content, "u": now, "n": NAME})
s.commit()
ck = s.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
s.close()
print(f"updated rows={r.rowcount} checkpoint={ck}")

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
n, ln = c.execute("SELECT name, length(content) FROM setting_templates WHERE name=:n",
                  {"n": NAME}).fetchone()
c.close()
print(f"[verify 跨进程] {n} content_chars={ln}")
