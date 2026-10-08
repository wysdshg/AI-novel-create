# 修复长平之战拍内多余旧 variant（SK05F 合并残留）：保留新走法 variant，删旧概要 variant → 重嵌
import sys, json, copy, sqlite3, hashlib
sys.path.insert(0, r'E:\AI小说创作\backend')
sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core import database as dbmod
from app.services.plot_template_crud import index_template
from app.models.orm import PlotTemplateORM

NAME = '大战征伐--诱敌歼灭·设伏合围-24'
apply = '--apply' in sys.argv

eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                    connect_args={"check_same_thread": False})
db = sessionmaker(bind=eng)()
o = db.query(PlotTemplateORM).filter(PlotTemplateORM.name == NAME).one()
st = copy.deepcopy(o.structure)

con = sqlite3.connect(r'C:\Users\w3013\.ai_novel\data\novel_agent.db')
row = con.execute("SELECT structure, source_stats, genre_tags, pitfalls FROM plot_templates WHERE name=?", (NAME,)).fetchone()
bp = r'E:\AI小说创作\outputs\sk05h\sk05m_长平修复备份.json'
with open(bp, 'w', encoding='utf-8') as f:
    f.write(json.dumps(
        {'name': NAME, 'structure': row[0], 'source_stats': row[1],
         'genre_tags': row[2], 'pitfalls': row[3]}, ensure_ascii=False, indent=1))
import hashlib as hl
print(f"[备份] 原始JSON文本 → 长平修复备份.json sha256={hl.sha256(open(bp,'rb').read()).hexdigest()[:16]}…")

fixed = 0
for p in st['phases']:
    for b in p['beats']:
        vs = b.get('variants', [])
        keep = [v for v in vs if v.get('desc', '').startswith('起：') and isinstance(v.get('tags'), list)]
        drop = [v for v in vs if v not in keep]
        if drop:
            for v in drop:
                print(f"  删旧 variant: beat={b['beat']} desc={str(v.get('desc'))[:40]}…")
            b['variants'] = keep
            fixed += 1
print(f'修复拍数: {fixed}')
assert fixed >= 1, '未发现需修复的拍'

if apply:
    o.structure = st
    index_template(db, o)
    db.commit()
    print('=== 长平 variant 修复完成（已重嵌） ===')
else:
    print('（dry-run 结束，加 --apply）')
