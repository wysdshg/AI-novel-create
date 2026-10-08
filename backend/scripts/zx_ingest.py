# [zx_ingest] 赘婿 42 弧 → plot_templates（origin=zx_v1，meta 剥离，污染原子排除）
# 用法: python zx_ingest.py            (dry-run)
#       python zx_ingest.py --apply    (正式入库，含备份)
import json, sys, hashlib, re
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

OUT = Path(r'E:\AI小说创作\outputs\zx300')
BATCHES = [('赘婿_原子.json', '赘婿_弧库.json'),
           ('赘婿_原子_c301-600.json', '赘婿_弧库_c301-600.json'),
           ('赘婿_原子_c601-900.json', '赘婿_弧库_c601-900.json'),
           ('赘婿_原子_c901-1262.json', '赘婿_弧库_c901-1262.json')]
EXCLUDE_MIN_CH = 1258  # c1258~1262 源概括他书污染，两原子不入库（弧尾收束 c1257）
META_RE = re.compile(r'[；;，,]?\d{3,4}章?为作者[^；;。]*')
PHASES4 = ['起', '承', '转', '合']


def split4(n):
    base, rem = divmod(n, 4)
    return [base + (1 if i < rem else 0) for i in range(4)]


def strip_meta(s):
    s2 = META_RE.sub('', s)
    return s2.rstrip('；;，, ').strip() or s.strip()  # 剥完为空则保留原句（防误剥）


def main(apply):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    arcs, atoms_by_batch = [], []
    for af, xf in BATCHES:
        atoms_by_batch.append(json.loads((OUT / af).read_text(encoding='utf-8-sig'))['原子列表'])
        arcs.extend(json.loads((OUT / xf).read_text(encoding='utf-8-sig'))['弧列表'])

    existing = {r[0] for r in db.execute(text("SELECT name FROM plot_templates")).fetchall()}
    todo = [a for a in arcs if a['弧名'] not in existing]
    print(f'弧 {len(arcs)}｜已存在跳过 {len(arcs) - len(todo)}｜待入库 {len(todo)}')

    if apply:
        rows = [{c.name: getattr(o, c.name) for c in PlotTemplateORM.__table__.columns}
                for o in db.query(PlotTemplateORM).all()]
        bp = OUT.parent / 'histsrc' / 'plot_templates_备份_zx入库前.json'
        bp.write_text(json.dumps({'table': 'plot_templates', 'row_count': len(rows), 'rows': rows},
                                 ensure_ascii=False, default=str), encoding='utf-8')
        print(f"[备份] {len(rows)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    created, skipped_arcs = [], []
    for a in todo:
        batch_atoms = None
        for (af, xf), atoms in zip(BATCHES, atoms_by_batch):
            refs = a['原子号']
            if refs and all(any(x['原子号'] == n for x in atoms) for n in refs if n <= 9999):
                batch_atoms = atoms
                break
        beats = []
        for i, no in enumerate(a['原子号']):
            atom = next((x for x in batch_atoms if x['原子号'] == no), None)
            if atom is None or atom['章起'] >= EXCLUDE_MIN_CH:
                continue  # 污染原子排除
            ring = a['拍链环草判'][i] if i < len(a['拍链环草判']) else '待定'
            txt = strip_meta(atom['概要'])
            beats.append({'beat': ring, 'variants': [{
                'src': '赘婿', 'how': txt,
                'desc': f"{txt}｜（c{atom['章起']}~{atom['章止']}）",
            }]})
        if len(beats) < 3:
            skipped_arcs.append((a['弧名'], f'有效拍 {len(beats)} < 3'))
            continue
        sizes = split4(len(beats))
        phases, i = [], 0
        for pi, cnt in enumerate(sizes):
            phases.append({'phase': PHASES4[pi], 'beats': beats[i:i + cnt]})
            i += cnt
        ring_chain = ' → '.join(b['beat'].split(' ', 1)[0] for b in beats)
        lo = min(b['variants'][0]['desc'] for b in beats)  # 占位防错——真实值下面重算
        chs = [int(m) for b in beats for m in [re.search(r'c(\d+)', b['variants'][0]['desc']).group(1)]]
        data = {
            'name': a['弧名'],
            'scale': 'arc',
            'genre_tags': ['赘婿模板', 'zx_v1'],
            'logline': f"（赘婿·历史商战）{len(beats)} 拍切原子骨架：{ring_chain}。",
            'structure': {'phases': phases, 'skeleton': a},
            'pitfalls': [],
            'source_stats': {
                'origin': 'zx_v1', 'books': 1, 'book_names': ['赘婿'],
                'n_members': 1, 'single_arc': True,
                'member_arcs': [{'book': '赘婿', 'arc': a['弧名'],
                                 'src_ref': 'Qoder 概括切原子四批',
                                 'confidence': '高', 'form_tag': '切原子弧',
                                 'judge_source': 'Qoder草判+PM复核', 'origin_gids': [],
                                 'ch_lo': min(chs), 'ch_hi': max(chs)}],
                'class': '', 'sub_event': '',
            },
            'status': 'active',
        }
        if apply:
            o = plot_template_crud.create(db, data)
            chunks = db.execute(
                text("SELECT COUNT(*) FROM vector_chunks WHERE source_type='plot_template' AND source_id=:sid"),
                {'sid': o.id}).scalar()
            created.append((a['弧名'], chunks))
            print(f"[apply] {a['弧名']} → {o.id[:8]}… 拍{len(beats)} 向量块 {chunks}")
        else:
            print(f"[dry] {a['弧名']}（拍{len(beats)}）")

    if apply:
        n_arc = db.execute(
            text("SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'")).scalar()
        print(f'\n[verify] arc active: 994 → {n_arc}（预期 1036）')
        assert n_arc == 1036, 'arc active 数量异常'
        bad_meta = db.execute(text("""
            SELECT COUNT(*) FROM plot_templates pt, json_tree(pt.structure) jt
            WHERE pt.genre_tags LIKE '%赘婿模板%' AND jt.type='text'
              AND (jt.value LIKE '%为作者%' OR jt.value LIKE '%无正文剧情%')""")).scalar()
        assert bad_meta == 0, f'meta 注记残留 {bad_meta}'
        miss = [nm for nm, c in created if c == 0]
        assert not miss, f'缺向量块: {miss}'
        if skipped_arcs:
            print(f'[注] 跳过弧: {skipped_arcs}')
        print(f'=== zx 入库完成：+{len(created)} 张（含 meta 剥离与污染排除）===')
    else:
        if skipped_arcs:
            print(f'[dry] 将跳过弧: {skipped_arcs}')
        print('（dry-run 结束，加 --apply 正式入库）')


if __name__ == '__main__':
    main('--apply' in sys.argv)
