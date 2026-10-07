# [hist_ingest] 史书弧库 → plot_templates 入库（origin=hist_v1）
# 用法: python hist_ingest.py            (dry-run)
#       python hist_ingest.py --apply    (正式入库，含备份)
# 幂等：按 name 已存在则跳过。向量由 plot_template_crud.create→index_template 自动建。
import json, sys, hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from app.core import database as dbmod  # noqa: E402
from app.services import plot_template_crud  # noqa: E402
from app.models.orm import PlotTemplateORM  # noqa: E402

PKG = Path(r'E:\AI小说创作\outputs\史书模板任务包')
OUT = Path(r'E:\AI小说创作\outputs\histsrc')
OUT.mkdir(parents=True, exist_ok=True)
FILES = ['史书弧库.json', '史书弧库_第2批.json']

# 已知争议/说明 → pitfalls（防后人把存疑数字当定论）
PITFALLS = {
    '官渡之战': ['歼敌数两史两存：七万余级（裴注《献帝起居注》）vs 八万（张斐《汉纪》）'],
    '长平之战': ['坑杀四十五万系《史记》记载，朱熹/胡三省至现代学者有质疑；相持时长有三年说/半年说'],
    '赤壁之战': ['战场地点与火攻细节历来有争议'],
    '采石之战': ['战果规模《宋史》与《中兴遗史》七大疑点有争议，现代学者有"遭遇战"说'],
}

PHASES4 = ['起', '承', '转', '合']


def split4(n):
    """n 拍按四分均分到起承转合，余数给前面的段"""
    base, rem = divmod(n, 4)
    return [base + (1 if i < rem else 0) for i in range(4)]


def main(apply: bool):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    arcs = []
    for fn in FILES:
        data = json.loads((PKG / fn).read_text(encoding='utf-8-sig'))
        arcs.extend(data['弧列表'])
    print(f'待入库弧: {len(arcs)}')

    # 幂等：已存在的名字跳过
    existing = {r[0] for r in db.query(PlotTemplateORM.name).all()}
    todo = [a for a in arcs if a['弧名'] not in existing]
    skipped = [a['弧名'] for a in arcs if a['弧名'] in existing]
    if skipped:
        print(f'已存在跳过: {skipped}')
    print(f'实际入库: {len(todo)}')

    if apply:
        rows = [{c.name: getattr(o, c.name) for c in PlotTemplateORM.__table__.columns}
                for o in db.query(PlotTemplateORM).all()]
        bak = {"table": "plot_templates", "row_count": len(rows), "rows": rows,
               "backup_time": "hist_ingest 写前备份"}
        bp = OUT / 'plot_templates_备份_hist入库前.json'
        bp.write_text(json.dumps(bak, ensure_ascii=False, default=str), encoding='utf-8')
        print(f'[备份] {len(rows)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…')

    created = []
    for a in todo:
        beats = a['拍链']
        n = len(beats)
        sizes = split4(n)
        phases, i = [], 0
        for pi, cnt in enumerate(sizes):
            group = []
            for b in beats[i:i + cnt]:
                ring = b['环草判'] if b['环草判'] != '待定' else '待定'
                group.append({'beat': ring, 'variants': [{
                    'src': f"{a['朝代']}·{a['弧名']}",
                    'how': b['拍概要'],
                    'desc': f"{b['拍概要']}｜依据：{b['原文依据']}",
                }]})
            i += cnt
            phases.append({'phase': PHASES4[pi], 'beats': group})
        ring_chain = ' → '.join(b['环草判'].split(' ', 1)[0] for b in beats)
        data = {
            'name': a['弧名'],
            'scale': 'arc',
            'genre_tags': ['史书模板', 'hist_v1', a['朝代'], a.get('弧类型', '战役')],
            'logline': f"（史书·{a['朝代']}·{a.get('弧类型','战役')}）{n} 拍史实骨架：{ring_chain}。",
            'structure': {'phases': phases, 'skeleton': a},
            'pitfalls': PITFALLS.get(a['弧名'], []),
            'source_stats': {
                'origin': 'hist_v1', 'books': 1, 'book_names': [a['朝代']],
                'n_members': 1, 'single_arc': True,
                'member_arcs': [{'book': f"史书·{a['朝代']}", 'arc': a['弧名'],
                                 'src_ref': a['史源'], 'confidence': '高',
                                 'form_tag': '史实弧', 'judge_source': '契约草判+PM验收',
                                 'origin_gids': [], 'ch_lo': 0, 'ch_hi': 0}],
                'class': '', 'sub_event': '',
            },
            'status': 'active',
        }
        if apply:
            o = plot_template_crud.create(db, data)
            chunks = db.execute(
                text("SELECT COUNT(*) FROM vector_chunks WHERE source_type='plot_template' AND source_id=:sid"),
                {'sid': o.id}).scalar()
            created.append((a['弧名'], o.id, chunks))
            print(f"[apply] {a['弧名']} → {o.id[:8]}… 向量块 {chunks}")
        else:
            print(f"[dry] {a['弧名']}（{n} 拍，待定 {sum(1 for b in beats if b['环草判']=='待定')}）")

    if apply:
        n_arc = db.execute(
            text("SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'")).scalar()
        print(f'\n[verify] arc active: 978 → {n_arc}（预期 994）')
        assert n_arc == 994, 'arc active 数量异常'
        miss = [nm for nm, tid, c in created if c == 0]
        assert not miss, f'缺向量块: {miss}'
        print('[verify] 16 模板向量块全部就位 ✓')
    print('完成' if apply else '（dry-run 结束，加 --apply 正式入库）')


if __name__ == '__main__':
    main('--apply' in sys.argv)
