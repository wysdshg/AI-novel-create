# -*- coding: utf-8 -*-
"""把留档的 AI 完整描述（60~120 字）回填进 global_items/global_skills.full_desc。

来源：
- outputs/_e3_ab_result_A4_all.json  丹药/阵法批（got: {词: 描述}）
- outputs/_e3_others_desc_all.json   其他物品批（got: {词: 描述}）

纪律（铁律 7）：StaticPool 单连接 + commit 后同连接 wal_checkpoint(PASSIVE)，
跑完跨进程重读核账；full_desc 列不存在则幂等 ALTER。
"""
import json
import sqlite3
import sys

sys.path.insert(0, "E:/AI小说创作/backend")
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

DB = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
SRC = [
    "E:/AI小说创作/outputs/_e3_ab_result_A4_all.json",
    "E:/AI小说创作/outputs/_e3_others_desc_all.json",
]

# 1) 合并留档映射（后者不覆盖前者——丹药阵法批优先，词集本就不相交）
mapping = {}
for p in SRC:
    d = json.load(open(p, encoding="utf-8"))
    got = d.get("got") or {}
    for w, desc in got.items():
        mapping.setdefault(w, (desc or "").strip())
print(f"[src] 留档词数={len(mapping)}")

engine = create_engine(
    f"sqlite:///{DB}",
    poolclass=StaticPool,       # 铁律 7：工具内写库必须单连接
    connect_args={"check_same_thread": False},
)
Session = sessionmaker(bind=engine)
s = Session()

# 2) 幂等加列（init_db 也会做，这里双保险不依赖重启时序）
for tbl in ("global_items", "global_skills"):
    cols = {r[1] for r in s.execute(text(f"PRAGMA table_info({tbl})"))}
    if "full_desc" not in cols:
        s.execute(text(f"ALTER TABLE {tbl} ADD COLUMN full_desc TEXT"))
        print(f"[alter] {tbl} 已加 full_desc")
s.commit()

# 3) 逐词回填（库内重名组已核账为 0，按 name 全表唯一匹配）
hit_item = hit_skill = miss = 0
miss_words = []
for w, desc in mapping.items():
    if not desc:
        continue
    r1 = s.execute(text("UPDATE global_items SET full_desc=:d WHERE name=:n"),
                   {"d": desc, "n": w}).rowcount
    r2 = 0 if r1 else s.execute(
        text("UPDATE global_skills SET full_desc=:d WHERE name=:n"),
        {"d": desc, "n": w}).rowcount
    if r1:
        hit_item += 1
    elif r2:
        hit_skill += 1
    else:
        miss += 1
        miss_words.append(w)

s.commit()
# 4) 同连接 checkpoint 落盘（铁律 7）
ck = s.execute(text("PRAGMA wal_checkpoint(PASSIVE)").execution_options(
    synchronize_session=False)).fetchone()
print(f"[checkpoint] {ck}")
s.close()

# 5) 跨进程重读核账（sqlite3 直连，只读）
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
ni = c.execute("SELECT COUNT(*) FROM global_items WHERE full_desc IS NOT NULL").fetchone()[0]
ns = c.execute("SELECT COUNT(*) FROM global_skills WHERE full_desc IS NOT NULL").fetchone()[0]
tot_i = c.execute("SELECT COUNT(*) FROM global_items").fetchone()[0]
tot_s = c.execute("SELECT COUNT(*) FROM global_skills").fetchone()[0]
c.close()
print(f"[verify 跨进程] items full_desc 非空 {ni}/{tot_i}，skills {ns}/{tot_s}")
print(f"[回填统计] hit_item={hit_item} hit_skill={hit_skill} miss={miss}")
if miss_words:
    print(f"[miss 词] {miss_words}")
print("RESULT:", json.dumps({
    "src_words": len(mapping), "hit_item": hit_item, "hit_skill": hit_skill,
    "miss": miss, "verify": {"items_with_desc": ni, "total_items": tot_i,
                             "skills_with_desc": ns, "total_skills": tot_s}},
    ensure_ascii=False))
