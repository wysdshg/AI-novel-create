# -*- coding: utf-8 -*-
"""把三套设定模板 MD 里的 ASCII 波浪号 ~ 全部替换为全角 ～（防 Markdown 删除线误判），并同步入库。"""
import json
import sqlite3
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
NAMES = ["玄幻小说设定", "架空历史设定", "都市高武设定"]
DIR = "E:/AI小说创作/outputs/setting-templates"

engine = create_engine(f"sqlite:///{DB}", poolclass=StaticPool,
                       connect_args={"check_same_thread": False})
s = sessionmaker(bind=engine)()
now = datetime.utcnow().isoformat(sep=" ")

report = []
for name in NAMES:
    path = f"{DIR}/{name}.md"
    t = open(path, encoding="utf-8").read()
    n = t.count("~")
    if n:
        t = t.replace("~", "～")
        open(path, "w", encoding="utf-8", newline="").write(t)
    r = s.execute(text("UPDATE setting_templates SET content=:c, updated_at=:u WHERE name=:n"),
                  {"c": t, "u": now, "n": name})
    report.append({"name": name, "replaced": n, "db_rows": r.rowcount})
s.commit()
ck = s.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
s.close()
print(json.dumps({"checkpoint": list(ck), "report": report}, ensure_ascii=False))

# 核账：库与文件都不应再有 ASCII ~
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
for name in NAMES:
    ln, cnt = c.execute(
        "SELECT length(content), (length(content) - length(replace(content, '~', ''))) FROM setting_templates WHERE name=:n",
        {"n": name}).fetchone()
    print(f"[verify] {name} chars={ln} 残留ASCII~={cnt}")
c.close()
