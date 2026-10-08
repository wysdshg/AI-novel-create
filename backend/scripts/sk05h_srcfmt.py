# [SK05H] 来源模板命名/格式规范化（58 张整改）+ 史书第三批入库（新格式直入）
# 用法: python sk05h_srcfmt.py            (dry-run)
#       python sk05h_srcfmt.py --apply    (正式执行)
import json, sys, hashlib, re, copy
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
from app.services.plot_template_crud import index_template  # noqa: E402
from app.models.orm import PlotTemplateORM  # noqa: E402

OUT = Path(r'E:\AI小说创作\outputs\sk05h')
OUT.mkdir(parents=True, exist_ok=True)
PKG = Path(r'E:\AI小说创作\outputs\史书模板任务包')


def ring_chain_full(beats):
    return ' → '.join(b['beat'] for b in beats)


def build_from_arc(arc, src_kind, src_label, origin, dynasty=None):
    """按图二规范从弧条目构建模板 data（史书/赘婿通用）"""
    beats = []
    for b in arc['拍链']:
        beats.append({'beat': b['环草判'], 'variants': [{
            'src': src_label, 'how': b['拍概要'],
            'desc': f"{b['拍概要']}｜依据：{b['原文依据']}" if b.get('原文依据') else b['拍概要'],
        }]})
    sizes, i = [], 0
    base, rem = divmod(len(beats), 4)
    sizes = [base + (1 if k < rem else 0) for k in range(4)]
    phases, i = [], 0
    for pi, cnt in enumerate(sizes):
        phases.append({'phase': ['起', '承', '转', '合'][pi], 'beats': beats[i:i + cnt]})
        i += cnt
    cls, sub = ('史实战役', arc['弧名']) if src_kind == 'hist' else ('赘婿剧情', arc['弧名'])
    chain = ring_chain_full(beats)
    tags = ['史书模板' if src_kind == 'hist' else '赘婿模板', cls]
    if dynasty:
        tags.append(dynasty)
    tags.append('1源')
    ss = {
        'origin': origin, 'books': 1, 'book_names': [src_label],
        'n_members': 1, 'single_arc': True, 'class': cls, 'sub_event': sub,
        'member_arcs': [{'book': src_label, 'arc': arc['弧名'], 'src_ref': arc['史源'],
                         'confidence': '高', 'form_tag': '史实弧' if src_kind == 'hist' else '切原子弧',
                         'judge_source': '契约草判+PM复核', 'origin_gids': [],
                         'ch_lo': arc.get('章起', 0) or 0, 'ch_hi': arc.get('章止', 0) or 0}],
    }
    return {
        'name': f'{cls}--{sub}',
        'scale': 'arc', 'genre_tags': tags,
        'logline': f"（{cls}--{sub}）1 条弧、1 源实证的情节骨架：{chain}。",
        'structure': {'phases': phases, 'skeleton': arc},
        'pitfalls': [], 'source_stats': ss, 'status': 'active',
    }


def main(apply):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    log = []

    def w(m):
        print(m); log.append(m)

    # ── 1. 存量 58 张整改（16 hist + 42 zx）──
    # 🔴 E24：genre_tags 是 \u 转义 JSON，LIKE 中文恒 0——必须全取后 Python 侧解析
    olds = [o for o in db.query(PlotTemplateORM).filter(
        PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
        if any(t in ('史书模板', '赘婿模板') for t in (o.genre_tags or []))]
    w(f'[整改] 存量来源模板: {len(olds)} 张（预期 58）')
    if apply:
        trows = [{c.name: getattr(o, c.name) for c in PlotTemplateORM.__table__.columns}
                 for o in db.query(PlotTemplateORM).all()]
        bp = OUT / 'sk05h_备份.json'
        bp.write_text(json.dumps({'templates': trows}, ensure_ascii=False, default=str), encoding='utf-8')
        w(f"[备份] {len(trows)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    for o in olds:
        kind = 'hist' if '史书模板' in (o.genre_tags or []) else 'zx'
        cls = '史实战役' if kind == 'hist' else '赘婿剧情'
        sub = o.name.split('--')[-1]  # 旧名为裸弧名
        dynasty = next((t for t in (o.genre_tags or []) if t in
                        {'春秋（东周）', '战国', '秦末', '汉', '楚汉', '东汉', '东汉末', '新莽末', '唐', '东晋', '南宋'}), None)
        # structure 已是规范四段（环 beat + variants），只改 name/logline/tags/class
        chain = ' → '.join(b['beat'] for p in (o.structure or {}).get('phases', []) for b in p.get('beats', []))
        tags = ['史书模板' if kind == 'hist' else '赘婿模板', cls] + ([dynasty] if dynasty else [])
        if apply:
            o.name = f'{cls}--{sub}'
            o.logline = f'（{cls}--{sub}）1 条弧、1 源实证的情节骨架：{chain}。'
            o.genre_tags = tags
            ss = dict(o.source_stats or {})
            ss['class'], ss['sub_event'] = cls, sub
            o.source_stats = ss
            index_template(db, o.id and o)
        w(f"  {sub[:20]} → {f'{cls}--{sub}'}")

    # ── 2. 史书第三批入库（新格式直入）──
    p3 = PKG / '史书弧库_第3批.json'
    if p3.exists():
        arcs3 = json.loads(p3.read_text(encoding='utf-8-sig'))['弧列表']
        have = {r[0] for r in db.execute(text("SELECT name FROM plot_templates")).fetchall()}
        todo = [a for a in arcs3 if f'史实战役--{a["弧名"]}' not in have]
        w(f'\n[第三批] 弧 {len(arcs3)}｜待入库 {len(todo)}')
        for a in todo:
            dyn = a.get('朝代')
            data = build_from_arc(a, 'hist', dyn or '?', 'hist_v1', dyn)
            if apply:
                o = plot_template_crud.create(db, data)
                w(f"  [apply] {data['name']} → {o.id[:8]}…")
            else:
                w(f"  [dry] {data['name']}（拍{len(a['拍链'])}）")
    else:
        w('\n[第三批] 文件不存在，跳过')

    if apply:
        n_arc = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'")).scalar()
        w(f'\n[verify] arc active: {n_arc}（预期 994+16+42+新增）')
        # 命名规范核查：来源模板必须带 -- 键结构
        bad = db.execute(text("""
            SELECT COUNT(*) FROM plot_templates pt, json_each(pt.genre_tags) je
            WHERE pt.scale='arc' AND pt.status='active'
              AND je.value IN ('史书模板','赘婿模板') AND pt.name NOT LIKE '%--%'""")).scalar()
        w(f'[verify] 来源模板无键结构残留: {bad}（预期 0）')
        old_ll = db.execute(text("""
            SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'
              AND logline LIKE '%拍切原子骨架%'""")).scalar()
        w(f'[verify] 旧句式 logline 残留: {old_ll}（预期 0）')
        w('=== SK05H 完成 ===')
    else:
        w('（dry-run 结束，加 --apply 正式执行）')
    (OUT / '执行日志.txt').write_text('\n'.join(log), encoding='utf-8')


if __name__ == '__main__':
    main('--apply' in sys.argv)
