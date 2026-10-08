# [SK05G] genre_tags 规范化清洗：994 张 active arc 模板（PM 自执行）
# 用法: python sk05g_tags_clean.py            (dry-run)
#       python sk05g_tags_clean.py --apply    (正式执行+重嵌)
#       python sk05g_tags_clean.py --restore  (从备份还原)
# 口径（用户拍板 A）：genre_tags 只留 来源标记/键大类/朝代/状态标 四类；
#   子事件键删除（name 已含）、'待定:*' 备注迁 source_stats.tag_notes、'原子骨架'删（scale 已表达）、
#   'hist_v1' 删（source_stats.origin 已有），保留可读的'史书模板'。
import json, sys, hashlib, re, copy
from collections import Counter
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
from app.services.plot_template_crud import index_template  # noqa: E402
from app.models.orm import PlotTemplateORM  # noqa: E402

OUT = Path(r'E:\AI小说创作\outputs\sk05g')
OUT.mkdir(parents=True, exist_ok=True)

SOURCE_KEEP = {'v4生成', '史书模板'}
STATUS_RE = [re.compile(r'^孤例$'), re.compile(r'^占位弧$'), re.compile(r'^含低置信成员$'), re.compile(r'^\d+书$')]
DROP = {'原子骨架', 'hist_v1', '战役'}
PEND_RE = re.compile(r'^待定:')
# 史书朝代集（新批次加朝代时在此追加）
DYNASTIES = {'春秋（东周）', '战国', '秦末', '汉', '楚汉', '东汉', '东汉末', '新莽末', '唐', '东晋', '南宋'}


def main(apply, restore):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    log = []

    def w(m):
        print(m); log.append(m)

    bakp = OUT / 'sk05g_备份.json'
    if restore:
        bak = json.loads(bakp.read_text(encoding='utf-8'))
        for row in bak['rows']:
            db.execute(text("UPDATE plot_templates SET genre_tags=:g, source_stats=:s WHERE id=:id"),
                       {'g': json.dumps(row['genre_tags'], ensure_ascii=False),
                        's': json.dumps(row['source_stats'], ensure_ascii=False), 'id': row['id']})
            o = db.get(PlotTemplateORM, row['id'])
            if o:
                index_template(db, o)
        db.commit()
        w(f'[restore] 已还原 {len(bak["rows"])} 行并重嵌')
        return

    # 大类集合 = active arc 模板 name 的 '--' 前段
    classes = set()
    for (nm,) in db.execute(text("SELECT name FROM plot_templates WHERE scale='arc' AND status='active'")).fetchall():
        if '--' in nm:
            classes.add(nm.split('--', 1)[0])
    w(f'[口径] 键大类 {len(classes)} 个（从 name 前段推导）')

    rows = db.query(PlotTemplateORM).filter(
        PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()

    if apply:
        snapshot = [{'id': o.id, 'name': o.name, 'genre_tags': o.genre_tags, 'source_stats': o.source_stats}
                    for o in rows]
        bp = OUT / 'sk05g_备份.json'
        bp.write_text(json.dumps({'rows': snapshot}, ensure_ascii=False, default=str), encoding='utf-8')
        w(f"[备份] {len(snapshot)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    report, changed_tids = Counter(), []
    for o in rows:
        old_tags = list(o.genre_tags or [])
        new_tags, notes = [], []
        for t in old_tags:
            if t in DROP or PEND_RE.match(t):
                if PEND_RE.match(t):
                    notes.append(t)
                continue
            if t in SOURCE_KEEP or t in classes or t in DYNASTIES or any(p.match(t) for p in STATUS_RE):
                if t not in new_tags:
                    new_tags.append(t)
                continue
            notes.append(f'未归类:{t}')  # 不确定的一律进备注，不静默丢
        # 排序：键大类 → 来源 → 朝代 → 状态标
        cls = [t for t in new_tags if t in classes]
        src = [t for t in new_tags if t in SOURCE_KEEP]
        dyn = [t for t in new_tags if t in DYNASTIES]
        stt = [t for t in new_tags if any(p.match(t) for p in STATUS_RE)]
        other = [t for t in new_tags if t not in cls + src + dyn + stt]
        final = cls + src + dyn + stt + other
        if notes:
            ss = copy.deepcopy(o.source_stats) if isinstance(o.source_stats, dict) else {}
            ss['tag_notes'] = (ss.get('tag_notes') or []) + notes
        if final != old_tags or (notes and o.source_stats == ss):
            pass
        if final != old_tags or notes:
            report['changed'] += 1
            for t in set(old_tags) - set(final):
                report[f'移除:{t[:14]}'] += 1
        else:
            continue
        if apply:
            o.genre_tags = final  # 新 list 对象，变更可检测
            if notes:
                o.source_stats = ss
            changed_tids.append(o.id)

    w(f"\n[统计] 变更 {report['changed']} 张｜移除类别 top: " +
      '；'.join(f'{k}×{v}' for k, v in sorted(((k, v) for k, v in report.items() if k.startswith('移除')), key=lambda x: -x[1])[:8]))

    if apply:
        db.commit()
        w(f'[apply] 落库 {len(changed_tids)} 张，重嵌向量…')
        for k, tid in enumerate(changed_tids, 1):
            index_template(db, db.get(PlotTemplateORM, tid))
            if k % 200 == 0:
                w(f'  …重嵌 {k}/{len(changed_tids)}')
        # verify
        after = set()
        for (g,) in db.execute(text("SELECT genre_tags FROM plot_templates WHERE scale='arc' AND status='active'")).fetchall():
            for t in json.loads(g):
                after.add(t)
        bad = [t for t in after if PEND_RE.match(t) or t in DROP or ('·' in t and t not in classes and t not in DYNASTIES)]
        w(f'[verify] 清洗后 distinct tag: {len(after)}（前 218）｜残留违规: {bad if bad else "0"}')
        assert not bad, f'清洗后仍有违规: {bad}'
        w('=== SK05G 完成 ===')
    else:
        w('（dry-run 结束，加 --apply 正式执行）')
    (OUT / '执行日志.txt').write_text('\n'.join(log), encoding='utf-8')


if __name__ == '__main__':
    main('--apply' in sys.argv, '--restore' in sys.argv)
