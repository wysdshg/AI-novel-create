# [SK05M] 史书走法回填+入库契约对齐（hist 37 张）：同 SK05L，特判长平映射
# 输入：走法增补_史书_批1~4.json；映射：清单拍 1..n → 库拍，长平之战 {8:7}（SK05F 合并拍，清单拍7 走法弃用记 pitfalls）
# 用法: python sk05m_hist_backfill.py            (dry-run)
#       python sk05m_hist_backfill.py --apply    (正式执行，含备份+重嵌)
import json, sys, copy, re, hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from app.core import database as dbmod  # noqa: E402
from app.services.plot_template_crud import index_template  # noqa: E402
from app.models.orm import PlotTemplateORM  # noqa: E402

PKG = Path(r'E:\AI小说创作\outputs\史书模板任务包')
OUT = Path(r'E:\AI小说创作\outputs\sk05h')
BATCHES = [
    ('走法增补_史书_批1.json', '史书走法增补清单.json'),
    ('走法增补_史书_批2.json', '史书走法增补清单_第2批.json'),
    ('走法增补_史书_批3.json', '史书走法增补清单_第3批.json'),
    ('走法增补_史书_批4.json', '史书走法增补清单_第4批.json'),
]
# 拍序重映射：弧名 → {清单拍序: 库拍序}
REMAP = {'长平之战': {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 8: 7}}
DROP_NOTE = {'长平之战': '走法增补时清单拍7（赵括突围被射杀）因 SK05F 合并拍调整未入库，库末拍=原拍8（坑杀降卒）'}


def how_from(src_txt, arc, dynasty):
    m = re.search(r'《[^》]+》[^；;，,]*', src_txt or '')
    ref = m.group(0).strip() if m else (dynasty or '')
    return f'{arc}（{ref}）'


def main(apply):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    walk = {}
    for wf, lf in BATCHES:
        d = json.loads((PKG / '走法增补' / wf).read_text(encoding='utf-8-sig'))
        ld = json.loads((PKG / '走法增补' / lf).read_text(encoding='utf-8-sig'))['拍列表']
        lmap = {(r['弧名'], r['拍序']): r for r in ld}
        for r in d['走法列表']:
            k = (r['弧名'], r['拍序'])
            s = lmap[k]
            walk[k] = {'走法': r['走法'], '标签': r['标签'], '史源': r['史源'], '朝代': s['朝代']}
    print(f'[输入] 走法 {len(walk)} 拍')

    rows = db.query(PlotTemplateORM).filter(
        PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
    olds = [o for o in rows if (o.source_stats or {}).get('origin') == 'hist_v1']
    arc2tpl = {o.source_stats['member_arcs'][0].get('arc'): o for o in olds}
    print(f'[定位] hist 模板 {len(olds)} 张；走法覆盖弧 {len(set(k[0] for k in walk))}/37')

    if apply:
        bak = [{'id': o.id, 'name': o.name, 'structure': o.structure,
                'source_stats': o.source_stats, 'genre_tags': o.genre_tags,
                'pitfalls': o.pitfalls} for o in olds]
        bp = OUT / 'sk05m_回填前备份.json'
        bp.write_text(json.dumps(bak, ensure_ascii=False, default=str), encoding='utf-8')
        print(f"[备份] {len(bak)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    for arc, o in sorted(arc2tpl.items()):
        ks = sorted(k[1] for k in walk if k[0] == arc)
        if not ks:
            print(f'  [跳过] {o.name}（走法未到）')
            continue
        st = copy.deepcopy(o.structure or {})
        beats = [b for p in st.get('phases', []) for b in p.get('beats', [])]
        remap = REMAP.get(arc, {})
        # 清单拍序 → 库拍序对齐
        pairs = []
        for i, b in enumerate(beats, 1):
            lst_seq = next((l for l, t in remap.items() if t == i), i)
            k = (arc, lst_seq)
            if k not in walk:
                print(f'  [跳过] {o.name}（库拍{i} 无对应走法）')
                pairs = None
                break
            pairs.append((b, walk[k], lst_seq))
        if pairs is None:
            continue
        if len(ks) > len(beats):
            dropped = [l for l in ks if remap.get(l, l) > len(beats) and l not in remap.get(arc, {})]
            print(f'  [注意] {arc}: 清单 {len(ks)} 拍 > 库 {len(beats)} 拍，弃用清单拍 {dropped}')
        for b, w, lst_seq in pairs:
            v = b['variants'][0]
            v['desc'] = w['走法']
            v['tags'] = list(w['标签'])
            v['how'] = how_from(w['史源'], arc, w['朝代'])
        st['display_top'] = 5
        o.structure = st
        ss = copy.deepcopy(o.source_stats or {})
        ss.setdefault('low_conf_members', [])
        ss['form_tags'] = {'史实弧': 1}
        ss.setdefault('tag_notes', [])
        m = ss['member_arcs'][0]
        m.setdefault('no', 1)
        m.setdefault('ident', f"HIST-{o.id[:8]}")
        m.setdefault('block', '')
        m.setdefault('line_keys', '')
        m.setdefault('downgraded', '')
        m.setdefault('axis_src', '')
        o.source_stats = ss
        o.genre_tags = [t for t in (o.genre_tags or []) if t != '孤例'] + ['孤例']
        if arc in DROP_NOTE:
            pf = list(o.pitfalls or [])
            pf.append(DROP_NOTE[arc])
            o.pitfalls = pf
        print(f'  {o.name}: {len(beats)} 拍回填')
        if apply:
            index_template(db, o)

    if apply:
        db.commit()
        bad = []
        for o in db.query(PlotTemplateORM).filter(
                PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all():
            if (o.source_stats or {}).get('origin') != 'hist_v1':
                continue
            st = o.structure or {}
            vs = [v for p in st.get('phases', []) for b in p.get('beats', []) for v in b.get('variants', [])]
            if st.get('display_top') != 5:
                bad.append(f'{o.name}/display_top')
            for v in vs:
                if set(v.keys()) != {'src', 'how', 'desc', 'tags'}:
                    bad.append(f'{o.name}/variant键')
                    break
                if not isinstance(v.get('tags'), list) or not (2 <= len(v['tags']) <= 4):
                    bad.append(f'{o.name}/tags')
                    break
                if not (v.get('desc', '').startswith('起：') and '合：' in v.get('desc', '')):
                    bad.append(f'{o.name}/desc非四段')
                    break
                if '（' not in v.get('how', '') or '《' not in v.get('how', ''):
                    bad.append(f'{o.name}/how无史源')
                    break
            ss = o.source_stats or {}
            for k in ('low_conf_members', 'form_tags', 'tag_notes'):
                if k not in ss:
                    bad.append(f'{o.name}/ss缺{k}')
            if '孤例' not in (o.genre_tags or []):
                bad.append(f'{o.name}/无孤例')
        print(f'\n[verify] 契约不符: {len(bad)}（预期 0）{bad[:6]}')
        print('=== SK05M 完成 ===')
    else:
        print('（dry-run 结束，加 --apply 正式执行）')


if __name__ == '__main__':
    main('--apply' in sys.argv)
