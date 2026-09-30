# -*- coding: utf-8 -*-
"""把三套题材设定模板写入 setting_templates 表（幂等：name 命中即跳过）。"""
import json
import sqlite3
import uuid
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
DOCS = [
    ("玄幻小说设定", "玄幻",
     "修真世界基准：炼气~大乘九大境界、丹药法宝妖兽九品映射、灵石货币与物价、灵气感知规则与写作禁忌、品级铁律。"),
    ("架空历史设定", "架空历史",
     "宋代框架基准：中央/路/府州军监/县四级官僚体系、军事编制、科举授官、法律刑罚、赋税土地、货币物价与百姓收支。"),
    ("都市高武设定", "高武",
     "武道九阶（明劲~武神）+ 凡间武道五档 + 实力克制规则；骨架版，预留异能/资源/势力分节。"),
]
SRC = "E:/AI小说创作/outputs/setting-templates"

engine = create_engine(f"sqlite:///{DB}", poolclass=StaticPool,
                       connect_args={"check_same_thread": False})
s = sessionmaker(bind=engine)()

s.execute(text("""CREATE TABLE IF NOT EXISTS setting_templates (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    genre VARCHAR(40) DEFAULT '通用',
    summary VARCHAR(500),
    content TEXT NOT NULL,
    tags JSON DEFAULT '[]',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)"""))
s.commit()

now = datetime.utcnow().isoformat(sep=" ")
inserted, skipped = [], []
for name, genre, summary in DOCS:
    exists = s.execute(text("SELECT id FROM setting_templates WHERE name=:n"),
                       {"n": name}).fetchone()
    if exists:
        skipped.append(name)
        continue
    content = open(f"{SRC}/{name}.md", encoding="utf-8").read()
    s.execute(text("""INSERT INTO setting_templates
        (id, name, genre, summary, content, tags, created_at, updated_at)
        VALUES (:id, :name, :genre, :summary, :content, :tags, :ca, :ua)"""),
        {"id": uuid.uuid4().hex, "name": name, "genre": genre,
         "summary": summary, "content": content,
         "tags": json.dumps([genre, "设定模板"], ensure_ascii=False),
         "ca": now, "ua": now})
    inserted.append(name)
s.commit()
ck = s.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
s.close()
print(f"[checkpoint] {ck} inserted={inserted} skipped={skipped}")

# 跨进程核账
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = c.execute("SELECT name, genre, length(content) FROM setting_templates ORDER BY name").fetchall()
c.close()
print("[verify 跨进程]", json.dumps(
    [{"name": n, "genre": g, "content_chars": ln} for n, g, ln in rows], ensure_ascii=False))
