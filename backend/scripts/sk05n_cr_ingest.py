# [SK05N] 橙红年代模板入库（49 弧 origin=cr_v1，F6T2 概括路线，判类已定案）
# 用法: python sk05n_cr_ingest.py            (dry-run)
#       python sk05n_cr_ingest.py --apply    (正式入库，含备份)
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

CR = Path(r'E:\AI小说创作\outputs\cr300')
OUT = Path(r'E:\AI小说创作\outputs\cr300')
SRC = [('留档_试水批_c2-300/橙红年代_原子.json', '留档_试水批_c2-300/橙红年代_弧库.json'),
       ('橙红年代_原子.json', '橙红年代_弧库.json')]

CLASSIFY = {
    '旧城保安连番立威与银行劫持对峙': ('初入立足', '踏入新域·从零立足'),
    '银行劫案扬名·乡里平霸立足': ('救人援场', '当场出手·救人于危'),
    '凑本起家·随扈竞标与黑道对峙': ('商战经营', '资本运作·融资周转'),
    '商会围猎与码头立势': ('商战经营', '产销扩张·打通商路'),
    '街头护人到执教两校': ('救人援场', '护弱庇幼·救人立名'),
    '女主播旧债与幼儿园劫持': ('大战征伐', '人质劫持·对峙营救'),
    '退婚兴园与市井恩仇': ('婚恋联姻', '婚约风波·订退成婚'),
    '借刀除毒首·夜场抢滩': ('智斗布局', '借力布局·借刀杀人'),
    '组建保卫队·强拆揭旧勋': ('开山建派', '立据建派·开基创业'),
    '砖拍案发与军地善后风波': ('智斗布局', '反制算计·识破拆招'),
    '金碧辉煌黑幕总清算': ('寻仇报复', '当众清算·旧怨了结'),
    '娱乐城覆灭后的黑白洗牌': ('大战征伐', '势力火并·家族宗门内战'),
    '攀附权贵与别墅血案': ('拜师入门', '拜师结缘·择枝附势'),
    '命案翻案与本地恶势力清算': ('寻仇报复', '当众清算·旧怨了结'),
    '假纪检套供与金三角跨境营救': ('救人援场', '救亲闯关·入险地救人'),
    '跨境拆机运回与入山问贫': ('禁地探秘', '异域闯入·险地闯阵'),
    '山峪血债与南下兴业': ('寻仇报复', '当众清算·旧怨了结'),
    '迁城股战与老厂接管': ('商战经营', '资本运作·融资周转'),
    '砸店旧怨与连环缉凶': ('大战征伐', '循线围捕·缉凶擒拿'),
    '千万受托赴边练兵': ('军政整军', '选才募兵·整军备战'),
    '凯旋受命与纪念碑风暴': ('大战征伐', '主动征伐·扫荡开疆'),
    '毒奶洪灾与南线谍影': ('智斗布局', '反制算计·识破拆招'),
    '公海归来与钢厂商路': ('商战经营', '产销扩张·打通商路'),
    '京华萍踪与专案收网': ('大战征伐', '循线围捕·缉凶擒拿'),
    '空难脱险与缉骗对弈': ('大战征伐', '循线围捕·缉凶擒拿'),
    '跨境钓行动与余波': ('大战征伐', '循线围捕·缉凶擒拿'),
    '官场斗法与援非军械': ('势力内斗', '派系倾轧·内斗夺权'),
    '孤身赴非湖区血火': ('大战征伐', '主动征伐·扫荡开疆'),
    '整军备轮跨境夺宝': ('秘境夺宝', '禁区险地·深入取物'),
    '远洋商战海外困局': ('商战经营', '产销扩张·打通商路'),
    '豪门招揽与大桥平凶': ('结盟共进', '以利结盟·利益捆绑'),
    '接军单安民越洋夺货': ('秘境夺宝', '禁区险地·深入取物'),
    '海外军火行动与欧陆潜盗复仇': ('寻仇报复', '斩首擒主·定点猎杀'),
    '归国认女风波与返乡兴产': ('身世揭秘', '身世揭开·血脉来历'),
    '跨洋赴典凯旋在望': ('初入立足', '踏入新域·从零立足'),
    '血色加冕与映山红': ('夺权政变', '政变易主·夺权换旗'),
    '县政风波与股市护盘': ('势力内斗', '派系倾轧·内斗夺权'),
    '联手御敌与整编制红星': ('军政整军', '整编军队·重编编制'),
    '烟波博弈与据点经营': ('据点立足', '新域落脚·插旗扎下根'),
    '少年恩怨与千里追凶': ('大战征伐', '循线围捕·缉凶擒拿'),
    '铁厂易主·工潮鏖战': ('夺权政变', '政变易主·夺权换旗'),
    '殉厂双葬·围猎反擒': ('大战征伐', '循线围捕·缉凶擒拿'),
    '处长劫案·连环追查': ('大战征伐', '循线围捕·缉凶擒拿'),
    '情人节缠斗·街头火并': ('大战征伐', '势力火并·家族宗门内战'),
    '血债血偿·跨海截书': ('寻仇报复', '斩首擒主·定点猎杀'),
    '脱险归国与跨海布商': ('商战经营', '产销扩张·打通商路'),
    '重器攻关与家门变故': ('技艺精进', '专精一门'),
    '蒙冤下狱与群英查案': ('大战征伐', '循线围捕·缉凶擒拿'),
    '越狱平乱·衣锦还乡收官': ('终章收束', '落幕收官'),
}
# meta 改判映射（试水批，按章定位）：章号 → 正确环
RING_PATCH = {33: 'A14 人质劫持·对峙营救', 37: 'A14 人质劫持·对峙营救', 114: 'A14 人质劫持·对峙营救',
              41: 'C22 审讯逼供·问案对峙', 69: 'C22 审讯逼供·问案对峙', 265: 'C22 审讯逼供·问案对峙',
              104: 'A15 循线围捕·缉凶擒拿', 76: 'H13 造谶惑众', 223: 'H13 造谶惑众',
              169: 'C17 民生治安与基层治理', 222: 'B08 非公平交易', 245: 'A03 群殴混战',
              109: 'A06 追击追杀', 180: 'D03 围困被困'}
# 多事件杂拍 tag_notes
MIXED = {413: '多事件杂拍（董事会反IPO+受邀参赛+授警衔）', 676: '多事件杂拍（家事+受训+点定职级+收权）'}
PHASES4 = ['起', '承', '转', '合']


def split4(n):
    base, rem = divmod(n, 4)
    return [base + (1 if i < rem else 0) for i in range(4)]


def main(apply):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    arcs = []
    for af, rf in SRC:
        at = json.loads((CR / af).read_text(encoding='utf-8-sig'))['原子列表']
        amap = {a['原子号']: a for a in at}
        for arc in json.loads((CR / rf).read_text(encoding='utf-8-sig'))['弧列表']:
            arcs.append((arc, amap))
    print(f'[输入] 弧 {len(arcs)}（预期 49）')

    taken = {r[0] for r in db.query(PlotTemplateORM.name).all()}
    key_seq: dict = {}
    names = {}
    for arc, _ in arcs:
        cls, sub = CLASSIFY[arc['弧名']]
        base = f'{cls}--{sub}' if sub else cls
        key_seq.setdefault(base, 0)
        key_seq[base] += 1
        name = base if key_seq[base] == 1 and base not in taken else f'{base}-{key_seq[base]}'
        while name in taken:
            key_seq[base] += 1
            name = f'{base}-{key_seq[base]}'
        taken.add(name)
        names[arc['弧名']] = name

    if apply:
        rows = [{c.name: getattr(o, c.name) for c in PlotTemplateORM.__table__.columns}
                for o in db.query(PlotTemplateORM).all()]
        bp = OUT / 'plot_templates_备份_cr入库前.json'
        bp.write_text(json.dumps({'table': 'plot_templates', 'rows': rows}, ensure_ascii=False, default=str), encoding='utf-8')
        print(f"[备份] {len(rows)} 行 → {bp.name} sha256={hashlib.sha256(bp.read_bytes()).hexdigest()[:16]}…")

    created = 0
    for arc, amap in arcs:
        cls, sub = CLASSIFY[arc['弧名']]
        key = cls if not sub else f'{cls}--{sub}'
        beats = []
        for i, g in enumerate(arc['拍链环草判'], 1):
            a = amap[arc['原子号'][i - 1]]
            ring = RING_PATCH.get(a['章起'], g)  # meta 改判按章定位
            beats.append({'beat': ring, 'variants': [{
                'src': '橙红年代',
                'how': f"{arc['弧名']}（c{a['章起']}~{a['章止']}）",
                'desc': a['走法'],
                'tags': list(a['标签']),
            }]})
        n = len(beats)
        sizes = split4(n)
        names4 = (['起'] if n == 1 else ['起', '转'] if n == 2 else ['起', '承', '转'] if n == 3 else ['起', '承', '转', '合'])
        phases, i = [], 0
        for pi, cnt in enumerate(sizes):
            phases.append({'phase': names4[pi], 'beats': beats[i:i + cnt]})
            i += cnt
        chain = ' → '.join(b['beat'] for b in beats)
        notes = [MIXED[c] for c in sorted(MIXED) if arc['章起'] <= c <= arc['章止']]
        d = {
            'name': names[arc['弧名']],
            'scale': 'arc',
            'genre_tags': [cls, '橙红模板', '1源', '孤例'],
            'logline': f'（{key}）1 条弧、1 源实证的情节骨架：{chain}。',
            'structure': {'phases': phases, 'display_top': 5, 'skeleton': arc},
            'pitfalls': [],
            'source_stats': {
                'origin': 'cr_v1', 'books': 1, 'book_names': ['橙红年代'],
                'n_members': 1, 'single_arc': True, 'class': cls, 'sub_event': sub,
                'low_conf_members': [], 'form_tags': {'切原子弧': 1}, 'tag_notes': notes,
                'member_arcs': [{'no': 1, 'book': '橙红年代', 'arc': arc['弧名'],
                                 'ch_lo': arc['章起'], 'ch_hi': arc['章止'],
                                 'src_ref': 'Qoder 概括切原子两批（F6T2 概括路线）', 'confidence': '中',
                                 'form_tag': '切原子弧', 'judge_source': '契约草判+PM复核',
                                 'origin_gids': [], 'ident': f"CR-{names[arc['弧名']][:12]}",
                                 'block': '', 'line_keys': '', 'downgraded': '', 'axis_src': ''}],
            },
            'status': 'active',
        }
        if apply:
            o = plot_template_crud.create(db, d)
            chunks = db.execute(text(
                "SELECT COUNT(*) FROM vector_chunks WHERE source_type='plot_template' AND source_id=:sid"),
                {'sid': o.id}).scalar()
            created += 1
            print(f"[apply] {arc['弧名']} → {d['name']} | {o.id[:8]}… 向量块 {chunks}")
        else:
            print(f"[dry] {arc['弧名']} → {d['name']}（{n} 拍）")

    if apply:
        db.commit()
        n_arc = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'")).scalar()
        print(f'\n[verify] arc active: 1057 → {n_arc}（预期 1106）')
        fake = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active' "
            "AND (name LIKE '赘婿剧情--%' OR name LIKE '史实战役--%')")).scalar()
        print(f'[verify] 假大类残留: {fake}（预期 0）')
        dup = db.execute(text(
            "SELECT COUNT(*) FROM (SELECT name, COUNT(*) c FROM plot_templates "
            "WHERE scale='arc' AND status='active' GROUP BY name HAVING c > 1)")).scalar()
        print(f'[verify] 重名: {dup}（预期 0）')
        miss = [r[0] for r in db.execute(text(
            "SELECT name FROM plot_templates pt WHERE pt.scale='arc' AND pt.status='active' "
            "AND pt.source_stats LIKE '%cr_v1%' AND NOT EXISTS "
            "(SELECT 1 FROM vector_chunks vc WHERE vc.source_id=pt.id AND vc.source_type='plot_template')")).fetchall()]
        print(f'[verify] cr_v1 缺向量: {len(miss)}（预期 0）{miss[:4]}')
        print('=== SK05N 完成 ===')
    else:
        print('（dry-run 结束，加 --apply）')


if __name__ == '__main__':
    main('--apply' in sys.argv)
