# [SK05L] 走法回填 + 入库契约对齐（zx 42 张）：按 SK05K_回填规格 十项
# 输入：走法增补_zx_批1~4.json（可缺批，--partial 用已有批验证映射）
# 动作：variant{desc←走法, tags←标签, how←弧名（c起~止）} + display_top=5
#       + source_stats{low_conf_members/form_tags/tag_notes + member_arcs 补 6 键}
#       + genre_tags 补'孤例' + meta 括注拍记 pitfalls
# 用法: python sk05l_backfill.py            (dry-run)
#       python sk05l_backfill.py --apply    (正式执行，含备份+重嵌)
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

ZX = Path(r'E:\AI小说创作\outputs\zx300\走法增补')
OUT = Path(r'E:\AI小说创作\outputs\sk05h')
BATCHES = [  # (走法文件, 清单文件)
    ('走法增补_zx_批1.json', '赘婿_走法增补清单_v2.json'),
    ('走法增补_zx_批2.json', '赘婿_走法增补清单_c301-600_v2.json'),
    ('走法增补_zx_批3.json', '赘婿_走法增补清单_c601-900_v2.json'),
    ('走法增补_zx_批4.json', '赘婿_走法增补清单_c901-1262_v2.json'),
]
META_PAT = re.compile(r'（\d+章为作者')


def main(apply):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    # 1) 汇总走法（缺批容错）
    walk, missing = {}, []
    for wf, lf in BATCHES:
        if not (ZX / wf).exists():
            missing.append(wf)
            continue
        d = json.loads((ZX / wf).read_text(encoding='utf-8-sig'))
        ld = json.loads((ZX / lf).read_text(encoding='utf-8-sig'))['拍列表']
        lmap = {(r['弧名'], r['拍序']): r for r in ld}
        for r in d['走法列表']:
            k = (r['弧名'], r['拍序'])
            s = lmap[k]
            walk[k] = {'走法': r['走法'], '标签': r['标签'],
                       '章起': s['章起'], '章止': s['章止']}
    print(f'[输入] 走法 {len(walk)} 拍；缺批: {missing or "无"}')

    # 2) 定位模板（弧名=member_arcs[0].arc）
    rows = db.query(PlotTemplateORM).filter(
        PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
    olds = [o for o in rows if (o.source_stats or {}).get('origin') == 'zx_v1']
    arc2tpl = {}
    for o in olds:
        arc = (o.source_stats['member_arcs'][0]).get('arc')
        arc2tpl[arc] = o
    covered = set(k[0] for k in walk)
    print(f'[定位] zx 模板 {len(olds)} 张；走法覆盖弧 {len(covered)}/42')

    if apply:
        bak = [{'id': o.id, 'name': o.name, 'structure': o.structure,
                'source_stats': o.source_stats, 'genre_tags': o.genre_tags,
                'pitfalls': o.pitfalls} for o in olds]
        bp = OUT / 'sk05l_回填前备份.json'
        bp.write_text(json.dumps(bak, ensure_ascii=False, default=str), encoding='utf-8')
        print(f"[备份] {len(bak)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    changed = 0
    for arc, o in sorted(arc2tpl.items()):
        ks = sorted(k for k in walk if k[0] == arc)
        if not ks:
            print(f'  [跳过] {o.name}（走法未到）')
            continue
        st = copy.deepcopy(o.structure or {})
        beats = [b for p in st.get('phases', []) for b in p.get('beats', [])]
        if len(beats) > len(ks):
            print(f'  [跳过] {o.name}（模板拍数 {len(beats)} > 走法 {len(ks)}，人工核查）')
            continue
        if len(beats) < len(ks):
            # 当年入库排除的拍（如铁拳陨落污染段）不入库，只回填前 len(beats) 拍
            dropped = '、'.join(f"拍{k[1]}（c{walk[k]['章起']}~{walk[k]['章止']}）" for k in ks[len(beats):])
            print(f'  [注意] {o.name}: 模板 {len(beats)} 拍 < 清单 {len(ks)} 拍，{dropped} 不入库（当年排除拍）')
        ks = ks[:len(beats)]
        dropped_note = (f'走法增补时排除当年未入库拍：{dropped}' if len(beats) < len(ks) else None)
        meta_hits = []
        for i, (b, k) in enumerate(zip(beats, ks), 1):
            w = walk[k]
            v = b['variants'][0]
            v['desc'] = w['走法']
            v['tags'] = list(w['标签'])
            v['how'] = f"{arc}（c{w['章起']}~{w['章止']}）"
            # src 保持原值（'赘婿'）
            if META_PAT.search(w['走法']):
                meta_hits.append(f"拍{i}（c{w['章起']}~{w['章止']}）")
        st['display_top'] = 5
        o.structure = st
        # source_stats 十项对齐
        ss = copy.deepcopy(o.source_stats or {})
        ss.setdefault('low_conf_members', [])
        ss['form_tags'] = {'切原子弧': 1}
        ss.setdefault('tag_notes', [])
        m = ss['member_arcs'][0]
        m.setdefault('no', 1)
        m.setdefault('ident', f"ZX-{o.id[:8]}")
        m.setdefault('block', '')
        m.setdefault('line_keys', '')
        m.setdefault('downgraded', '')
        m.setdefault('axis_src', '')
        o.source_stats = ss
        # genre_tags 补孤例（序：大类, 来源标, 1源, 孤例）
        tags = [t for t in (o.genre_tags or []) if t != '孤例']
        o.genre_tags = tags + ['孤例']
        # meta 括注记 pitfalls
        if meta_hits:
            pf = list(o.pitfalls or [])
            pf.append(f'走法含 meta 章括注（作者感言/随笔章无正文剧情）：{"、".join(meta_hits)}')
            if dropped_note:
                pf.append(dropped_note)
            o.pitfalls = pf
        elif dropped_note:
            pf = list(o.pitfalls or [])
            pf.append(dropped_note)
            o.pitfalls = pf
        n_meta = f'｜meta {len(meta_hits)} 拍' if meta_hits else ''
        print(f'  {o.name}: {len(beats)} 拍回填{n_meta}')
        if apply:
            index_template(db, o)
            changed += 1

    if apply:
        db.commit()
        # verify：契约逐项
        import sqlite3
        bad = []
        for o in db.query(PlotTemplateORM).filter(
                PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all():
            if (o.source_stats or {}).get('origin') != 'zx_v1':
                continue
            st = o.structure or {}
            vs = [v for p in st.get('phases', []) for b in p.get('beats', []) for v in b.get('variants', [])]
            if st.get('display_top') != 5:
                bad.append(f'{o.name}/display_top')
            for v in vs:
                if set(v.keys()) != {'src', 'how', 'desc', 'tags'}:
                    bad.append(f'{o.name}/variant键{sorted(v.keys())}')
                    break
                if not isinstance(v.get('tags'), list) or not (2 <= len(v['tags']) <= 4):
                    bad.append(f'{o.name}/tags')
                    break
                if not (v.get('desc', '').startswith('起：') and '合：' in v.get('desc', '')):
                    bad.append(f'{o.name}/desc非四段')
                    break
                if '（c' not in v.get('how', ''):
                    bad.append(f'{o.name}/how')
                    break
            ss = o.source_stats or {}
            for k in ('low_conf_members', 'form_tags', 'tag_notes'):
                if k not in ss:
                    bad.append(f'{o.name}/ss缺{k}')
            if '孤例' not in (o.genre_tags or []):
                bad.append(f'{o.name}/无孤例')
        print(f'\n[verify] 契约不符: {len(bad)}（预期 0）{bad[:6]}')
        print('=== SK05L 完成 ===')
    else:
        print('（dry-run 结束，加 --apply 正式执行；当前仅批1~3 时不 apply，等批4 齐）')


if __name__ == '__main__':
    main('--apply' in sys.argv)
