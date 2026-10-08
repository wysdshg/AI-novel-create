# [SK05K] 来源模板阶段重排（79 张 zx+hist）：对齐小说库切段惯例 + 向量重嵌
# 惯例（skel_v4_2 实测）：n≤4 每拍一段（起/起转/起承转/起承转合）；5~7 拍 [1,1,1,n-3]；8+ 拍 [2,2,2,n-6]
# 用法: python sk05k_phase_reorg.py            (dry-run)
#       python sk05k_phase_reorg.py --apply    (正式执行)
import json, sys, copy, hashlib
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

OUT = Path(r'E:\AI小说创作\outputs\sk05h')


def seg_sizes(n):
    if n <= 4:
        return [1] * n
    if n <= 7:
        return [1, 1, 1, n - 3]
    return [2, 2, 2, n - 6]


PHASE_NAMES = {1: ('起',), 2: ('起', '转'), 3: ('起', '承', '转'), 4: ('起', '承', '转', '合')}


def main(apply):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    rows = db.query(PlotTemplateORM).filter(
        PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
    olds = [o for o in rows
            if (o.source_stats or {}).get('origin') in ('zx_v1', 'hist_v1')]
    print(f'[定位] 来源模板: {len(olds)}（预期 79 = zx42 + hist37）')

    if apply:
        bak = [{'id': o.id, 'name': o.name, 'structure': o.structure} for o in olds]
        bp = OUT / 'sk05k_结构备份.json'
        bp.write_text(json.dumps(bak, ensure_ascii=False, default=str), encoding='utf-8')
        print(f"[备份] {len(bak)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    changed = 0
    for o in olds:
        st = copy.deepcopy(o.structure or {})
        beats = [b for p in st.get('phases', []) for b in p.get('beats', [])]
        n = len(beats)
        sizes = seg_sizes(n)
        names = PHASE_NAMES[n] if n <= 4 else ('起', '承', '转', '合')
        phases, i = [], 0
        for name, cnt in zip(names, sizes):
            phases.append({'phase': name, 'beats': beats[i:i + cnt]})
            i += cnt
        old_sizes = [len(p.get('beats', [])) for p in st.get('phases', [])]
        if old_sizes == sizes and [p.get('phase') for p in st['phases']] == list(names):
            print(f'  [跳过] {o.name}（已符合）')
            continue
        print(f'  {o.name}: {len(st.get("phases", []))}段{old_sizes} → {len(phases)}段{sizes}')
        if apply:
            st['phases'] = phases
            o.structure = st  # deepcopy 新对象，变更检测可触发
            index_template(db, o)
            changed += 1

    if apply:
        db.commit()
        # 复核：79 张全部符合惯例
        bad = []
        for o in db.query(PlotTemplateORM).filter(
                PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all():
            if (o.source_stats or {}).get('origin') in ('zx_v1', 'hist_v1'):
                ph = (o.structure or {}).get('phases', [])
                sizes = [len(p.get('beats', [])) for p in ph]
                if sizes != seg_sizes(sum(sizes)):
                    bad.append(o.name)
        print(f'\n[verify] 惯例不符残留: {len(bad)}（预期 0）{bad or ""}')
        print(f'[verify] 重嵌完成 {changed} 张（向量块由 index_template 日志核对）')
        print('=== SK05K 完成 ===')
    else:
        print('（dry-run 结束，加 --apply 正式执行）')


if __name__ == '__main__':
    main('--apply' in sys.argv)
