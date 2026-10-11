# 补索引：cr_v1 49 张重新 index_template（幂等）
import sys
sys.path.insert(0, r'E:\AI小说创作\backend')
sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core import database as dbmod
from app.services.plot_template_crud import index_template
from app.models.orm import PlotTemplateORM

apply = '--apply' in sys.argv
eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                    connect_args={"check_same_thread": False})
db = sessionmaker(bind=eng)()
rows = db.query(PlotTemplateORM).filter(
    PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
targets = [o for o in rows if (o.source_stats or {}).get('origin') == 'cr_v1']
print(f'待补索引: {len(targets)} 张')
ok = 0
if apply:
    for o in targets:
        n = index_template(db, o)
        print(f'  {o.name}: {n} 块')
        if n > 0:
            ok += 1
    db.commit()
    miss = [o.name for o in targets if db.execute(
        text("SELECT COUNT(*) FROM vector_chunks WHERE source_type='plot_template' AND source_id=:sid"),
        {'sid': o.id}).scalar() == 0]
    print(f'\n[verify] 补索引成功 {ok}/{len(targets)}；仍缺: {miss or "无"}')
    print('=== 补索引完成 ===')
else:
    print('（加 --apply 执行）')
