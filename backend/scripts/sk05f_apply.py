# [SK05F] 词表修订 v2：六环转正 + A05 改名以弱胜强 + 史书草判修订（PM 自执行）
# 用法: python sk05f_apply.py            (dry-run)
#       python sk05f_apply.py --apply    (正式执行)
#       python sk05f_apply.py --restore  (从备份还原)
import json, sys, hashlib, copy
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
from app.models.orm import PlotTemplateORM, AtomicEventORM  # noqa: E402

OUT = Path(r'E:\AI小说创作\outputs\sk05f')
OUT.mkdir(parents=True, exist_ok=True)
NOW = '2026-10-08 12:00:00.000000'
from datetime import datetime as _dt  # noqa: E402
NOW_DT = _dt(2026, 10, 8, 12, 0, 0)
OLD, NEW = 'A05 越阶硬撼', 'A05 以弱胜强'


def split4(n):
    base, rem = divmod(n, 4)
    return [base + (1 if i < rem else 0) for i in range(4)]


PHASES4 = ['起', '承', '转', '合']

NEW_RINGS = [
    {"id": "A12", "name": "阵前斩将擒将", "category_id": "A",
     "definition": "两军阵前，单骑或神射突入直取敌方将领——斩杀、生擒、狙射敌主将皆算（不限于约战形式）。",
     "beat_start": "两军对垒，敌方将领显眼出列（麾盖、夸阵、督战位置）",
     "beat_mid": "单骑或神射突入阵中直取敌将",
     "beat_turn": "敌方救援反应或得手瞬间惊阵",
     "beat_end": "斩将擒俘得手，敌军气势大挫",
     "is_core_capable": 1, "domain": "general", "scope_tags": "null", "status": "active"},
    {"id": "A13", "name": "战后复盘", "category_id": "A",
     "definition": "战事结束后复盘得失——兵法问答、成败检讨、论功与定策（史书'问兵法'与小说战后议皆算）。",
     "beat_start": "战事尘埃落定，回到营中或朝堂",
     "beat_mid": "复盘成败缘由，兵法问答或得失检讨",
     "beat_turn": "争议点激辩、旧疑被点破或认识升华",
     "beat_end": "结论定策，为后续行动埋下认知",
     "is_core_capable": 0, "domain": "general", "scope_tags": "null", "status": "active"},
    {"id": "C21", "name": "城池易主", "category_id": "C",
     "definition": "城池、要地或势力版图在战争与博弈中易手——攻克、开城请降、举众归附、势力消长版图重划皆算。",
     "beat_start": "城池或势力归属悬而未决",
     "beat_mid": "兵临城下、劝降或内变，归属天平倾斜",
     "beat_turn": "守方决断——死守到底还是开城请降",
     "beat_end": "版图易主，格局重划",
     "is_core_capable": 1, "domain": "general", "scope_tags": "null", "status": "active"},
    {"id": "D11", "name": "军势失控", "category_id": "D",
     "definition": "军心士气在压力或意外下崩溃失控、阵脚自乱——久候饥倦、争抢辎重、讹言惊营、退势不可遏止皆算（非多方混战本身）。",
     "beat_start": "大军列阵或行军，表面完整",
     "beat_mid": "意外因素（久候饥倦、争抢、讹言）侵蚀纪律",
     "beat_turn": "失控蔓延，阵脚自乱不可遏止",
     "beat_end": "全线溃乱或意外转机，代价惨重",
     "is_core_capable": 1, "domain": "general", "scope_tags": "null", "status": "active"},
    {"id": "D12", "name": "天变助战", "category_id": "D",
     "definition": "风雨雷水火雾等自然天象在关键时刻介入战场左右胜负——一方得利一方受害，时运成分显著。",
     "beat_start": "战局胶着或一方危殆",
     "beat_mid": "天象骤起——雷风、洪水、大雾、风向转势",
     "beat_turn": "一方得利一方受害，局面逆转",
     "beat_end": "胜负分定，留时运之叹",
     "is_core_capable": 0, "domain": "general", "scope_tags": "null", "status": "active"},
    {"id": "E04", "name": "撤退退却", "category_id": "E",
     "definition": "军队脱离战场或辖地的退却行军——战略后撤、败退、佯退皆算。与 A07 突围脱身互锚：A07=被围撕口求生，本环=已脱接触后的退却组织。",
     "beat_start": "决意脱离当前战场或辖地",
     "beat_mid": "组织退却——断后、弃辎重、选路线，敌或追或缓",
     "beat_turn": "退却途中遭袭、遇险阻或成功脱离接触",
     "beat_end": "抵达集结地或付出代价，转入下一阶段",
     "is_core_capable": 1, "domain": "general", "scope_tags": "null", "status": "active"},
]
A05_NEW = {"name": "以弱胜强",
           "definition": "以明显劣势（境界低、兵力少、装备差、地位弱）正面迎战占优之敌并取胜——越阶硬撼、以少胜多、以弱克强皆此环变体。"}

# 史书 16 弧草判修订表：(弧名, 拍序) → 新环；MERGE=拍并入下一拍
REMAP = {
    ('井陉之战', 8): 'D11 军势失控', ('井陉之战', 10): 'A13 战后复盘',
    ('官渡之战', 10): 'C21 城池易主',
    ('虎牢之战', 5): 'A12 阵前斩将擒将', ('虎牢之战', 6): 'D11 军势失控', ('虎牢之战', 10): 'C21 城池易主',
    ('淝水之战', 6): 'D11 军势失控', ('淝水之战', 9): 'D11 军势失控',
    ('香积寺之战', 6): 'D11 军势失控', ('香积寺之战', 11): 'C21 城池易主',
    ('鄢陵之战', 4): 'D09 谍报刺探与反谍', ('鄢陵之战', 7): 'A12 阵前斩将擒将',
    ('城濮之战', 3): 'A10 军事筹谋与战前部署', ('城濮之战', 4): 'E04 撤退退却',
    ('崤之战', 1): 'D09 谍报刺探与反谍', ('崤之战', 4): 'D09 谍报刺探与反谍',
    ('潍水之战', 4): 'A04 设伏偷袭',
    ('昆阳之战', 7): 'D12 天变助战',
    ('赤壁之战', 6): 'E04 撤退退却',
}
MERGE = {('长平之战', 7): 8}  # 拍7（赵括突围战死）并入拍8
COLS = ['id', 'name', 'category_id', 'definition', 'beat_start', 'beat_mid', 'beat_turn',
        'beat_end', 'is_core_capable', 'domain', 'scope_tags', 'status', 'created_at']


def tmpl_dict(o):
    return {c.name: getattr(o, c.name) for c in PlotTemplateORM.__table__.columns}


def main(apply, restore):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    log = []

    def w(m):
        print(m); log.append(m)

    bakp = OUT / 'sk05f_备份.json'
    if restore:
        bak = json.loads(bakp.read_text(encoding='utf-8'))
        # 还原 atomic_events：删 6 新环 + 还原 A05
        db.execute(text("DELETE FROM atomic_events WHERE id IN ('A12','A13','C21','D11','D12','E04')"))
        a05 = bak['a05_old']
        db.execute(text("UPDATE atomic_events SET name=:n, definition=:d WHERE id='A05'"),
                   {'n': a05['name'], 'd': a05['definition']})
        # 还原模板：按备份逐行覆盖
        for row in bak['templates']:
            db.execute(
                "UPDATE plot_templates SET name=:name, logline=:logline, structure=:structure WHERE id=:id",
                {'name': row['name'], 'logline': row['logline'],
                 'structure': json.dumps(row['structure'], ensure_ascii=False), 'id': row['id']})
        db.commit()
        n_ev = db.execute(text("SELECT COUNT(*) FROM atomic_events")).scalar()
        w(f'[restore] atomic_events={n_ev}（预期 80），模板已按备份还原')
        return

    # ── 0. 备份 ──
    a05_old = dict(db.execute(
        text("SELECT name, definition FROM atomic_events WHERE id='A05'")).mappings().first())
    trows = [{c.name: getattr(o, c.name) for c in PlotTemplateORM.__table__.columns}
             for o in db.query(PlotTemplateORM).all()]
    bak = {'a05_old': a05_old, 'templates': trows,
           'backup_time': 'SK05F 写前备份（含史书16弧）'}
    bp = OUT / 'sk05f_备份.json'
    bp.write_text(json.dumps(bak, ensure_ascii=False, default=str), encoding='utf-8')
    w(f"[备份] atomic_events A05 原值 + plot_templates {len(trows)} 行 → {bp.name} "
      f"sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    # ── 1. 词表：6 INSERT + A05 UPDATE ──
    w('\n[词表] 六环转正：' + '、'.join(f"{r['id']} {r['name']}" for r in NEW_RINGS))
    w(f"[词表] A05 越阶硬撼 → {A05_NEW['name']}（定义泛化）")
    if apply:
        have = {r[0] for r in db.execute(text("SELECT id FROM atomic_events")).fetchall()}
        for r in NEW_RINGS:
            if r['id'] not in have:  # 幂等：上次网关中断可能已提交过
                db.add(AtomicEventORM(**{c: (NOW_DT if c == 'created_at' else r[c]) for c in COLS}))
        db.execute(text("UPDATE atomic_events SET name=:n, definition=:d WHERE id='A05'"),
                   {'n': A05_NEW['name'], 'd': A05_NEW['definition']})
        db.commit()

    # ── 2. 模板：A05 批量替换（170）∪ 史书草判修订（12 弧）∪ 长平并入 ──
    a05_ids = {r[0] for r in db.execute(
        text("SELECT id FROM plot_templates WHERE scale='arc' AND status='active' AND structure LIKE '%A05 %'")).fetchall()}
    hist_names = {k[0] for k in REMAP} | {k[0] for k in MERGE}
    hist_ids = {r[0] for r in db.query(PlotTemplateORM.id, PlotTemplateORM.name).all()
                if r[1] in hist_names}
    targets = a05_ids | hist_ids
    changed_tids = []
    reidx = 0
    w(f'\n[模板] A05 替换 {len(a05_ids)} 张 ∪ 草判修订 {len(hist_ids)} 张 = 处理 {len(targets)} 张')

    if apply:
        reidx = 0
        for tid in targets:
            o = db.get(PlotTemplateORM, tid)
            st = copy.deepcopy(o.structure)  # 🔴 必须深拷贝：原地改同一对象，SQLAlchemy 检测不到变更（本轮首跑即栽在这）
            changed = False
            # A05 替换（拍链 beat 值）
            for p in st.get('phases', []):
                for b in p.get('beats', []):
                    if b.get('beat') == OLD:
                        b['beat'] = NEW
                        changed = True
            logline = o.logline or ''
            new_log = logline.replace(OLD, NEW)
            if new_log != logline:
                changed = True
            # 史书草判修订（按 skeleton 弧名+拍序定位拍链；扁平化处理后按四段重建）
            sk = st.get('skeleton') or {}
            nm = sk.get('弧名')
            if nm in hist_names:
                flat = [b for p in st['phases'] for b in p['beats']]
                changed_any = False
                for (an, seq), ring in REMAP.items():
                    if an == nm and seq <= len(flat) and flat[seq - 1]['beat'] != ring:
                        flat[seq - 1]['beat'] = ring
                        changed_any = True
                drops = set()
                for (an, seq), nxt in MERGE.items():
                    # 幂等护栏：仅当该拍仍是"待定"才并入（已并过则拍序已重排，跳过）
                    if an == nm and seq <= len(flat) and flat[seq - 1].get('beat') == '待定':
                        src_b, dst_b = flat[seq - 1], flat[seq]
                        dst_b['variants'] = dst_b.get('variants', []) + src_b.get('variants', [])
                        drops.add(seq - 1)
                        changed_any = True
                if drops:
                    flat = [b for i, b in enumerate(flat) if i not in drops]
                    changed_any = True
                if changed_any:
                    sizes = split4(len(flat))
                    phases2, i2 = [], 0
                    for pi, cnt in enumerate(sizes):
                        phases2.append({'phase': PHASES4[pi], 'beats': flat[i2:i2 + cnt]})
                        i2 += cnt
                    st['phases'] = phases2
                    changed = True
            if changed:
                o.structure = st
                o.logline = new_log
                o.updated_at = NOW_DT
                changed_tids.append(tid)
        db.commit()
        # 向量统一重嵌（上次网关中断导致部分模板索引被跳过，此处强制全量重嵌）
        w(f'[apply] 文本变更 {len(changed_tids)} 张，强制重嵌全部目标向量…')
        for k, tid in enumerate(sorted(targets), 1):
            index_template(db, db.get(PlotTemplateORM, tid))
            if k % 30 == 0:
                w(f'  …重嵌 {k}/{len(targets)}')
        w(f'[apply] 处理 {len(targets)} 张，向量重嵌完成')

        # ── 3. verify ──
        n_ev = db.execute(text("SELECT COUNT(*) FROM atomic_events")).scalar()
        assert n_ev == 86, f'词表 {n_ev} != 86'
        a05 = dict(db.execute(
            text("SELECT name, definition FROM atomic_events WHERE id='A05'")).mappings().first())
        assert a05['name'] == '以弱胜强'
        old_left = db.execute(text("""
            SELECT COUNT(*) FROM plot_templates pt, json_tree(pt.structure) jt
            WHERE pt.scale='arc' AND pt.status='active' AND jt.type='text' AND jt.key='beat'
              AND jt.value='A05 越阶硬撼'""")).scalar()
        assert old_left == 0, f'旧环名残留 {old_left}'
        # 史书草判复核
        bad = []
        for (an, seq), ring in REMAP.items():
            o = db.query(PlotTemplateORM).filter(PlotTemplateORM.name == an).first()
            flat = [b for p in (o.structure.get('phases') or []) for b in p['beats']]
            if flat[seq - 1]['beat'] != ring:
                bad.append((an, seq, flat[seq - 1]['beat']))
        assert not bad, f'草判修订未生效: {bad}'
        w(f'[verify] 词表 86 ✓ A05 已改名 ✓ 旧环名残留 0 ✓ 史书草判 {len(REMAP)} 处生效 ✓')
        n_arc = db.execute(text("SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'")).scalar()
        w(f'[verify] arc active 仍为 {n_arc}（994，长平并入不减模板数）')
        w('\n=== SK05F 全部通过：词表 80→86，A05 已改名，草判修订完成 ===')
    else:
        w('（dry-run 结束，加 --apply 正式执行）')
    (OUT / '执行日志.txt').write_text('\n'.join(log), encoding='utf-8')


if __name__ == '__main__':
    main('--apply' in sys.argv, '--restore' in sys.argv)
