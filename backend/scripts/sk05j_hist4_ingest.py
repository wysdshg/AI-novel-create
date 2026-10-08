# [SK05J] 史书第 4 批入库（17 弧，origin=hist_v1，键=既有大类--子事件）
# 依据：outputs/史书模板任务包/pm-verdict-第4批.md（用户四项拍板+判类提案已批）
# 庞勋弧整弧撤下；5 拍环改判（H12×2/H13×3）；程婴传说层标记。
# 用法: python sk05j_hist4_ingest.py            (dry-run)
#       python sk05j_hist4_ingest.py --apply    (正式入库)
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
SRC = '史书弧库_第4批.json'
DROP = {'庞勋桂林戍卒之变'}          # 用户拍板整弧撤下

# 环改判映射：弧名 → {拍序: 新环}（pm-verdict-第4批.md）
RING_PATCH = {
    '海瑞备棺上治安疏': {1: 'H12 以死明志'},
    '程婴匿赵孤': {7: 'H12 以死明志'},
    '大泽乡起兵': {4: 'H13 造谶惑众'},
    '黄巾之乱': {1: 'H13 造谶惑众', 2: 'H13 造谶惑众'},
}

# 判类：弧名 → (大类, 子事件)（判类提案_第4批.md 已批）
CLASSIFY = {
    '商鞅变法': ('新政改革', '新政改制·颁行新制'),
    '杨炎建两税法': ('新政改革', '新政改制·颁行新制'),
    '王安石司马光青苗廷辩': ('新政改革', '新政改制·颁行新制'),
    '西门豹治邺': ('新政改革', '新政改制·颁行新制'),
    '海瑞备棺上治安疏': ('庙堂权谋', '犯颜直谏'),
    '甘露之变': ('夺权政变', '政变易主·夺权换旗'),
    '陈桥兵变': ('夺权政变', '政变易主·夺权换旗'),
    '康熙擒鳌拜': ('清除内患', '清除异己·杀掉反对者'),
    '和珅跌倒': ('清除内患', '清除异己·杀掉反对者'),
    '严嵩倒台': ('势力内斗', '派系倾轧·内斗夺权'),
    '吕不韦奇货可居': ('商战经营', '资本运作·融资周转'),
    '桑弘羊均输平准与告缗': ('新政改革', '新政改制·颁行新制'),
    '白圭人弃我取': ('商战经营', '资本运作·融资周转'),
    '卓王孙与文君当垆': ('婚恋联姻', '婚约风波·订退成婚'),
    '大泽乡起兵': ('开山建派', '立据建派·开基创业'),
    '黄巾之乱': ('开山建派', '立据建派·开基创业'),
    '程婴匿赵孤': ('据点立足', '潜伏藏身·隐于势力'),
}

# 已知争议/说明 → pitfalls（防后人把存疑内容当定论）
PITFALLS = {
    '程婴匿赵孤': [
        '传说层素材：主源《史记·赵世家》与《左传·成公八年》所载赵氏覆亡、赵武继嗣大异（左传无屠岸贾、程婴、公孙杵臼），两源不可折中',
        '伦理重负载：含「以他人婴儿代死」情节，模板使用时留意',
    ],
    '西门豹治邺': ['史源为《史记·滑稽列传》褚少孙所补，属补续材料而非《史记》本文'],
    '黄巾之乱': [
        '「斩首十余万级」出《后汉书》历来存疑，弧概括未取为定论',
        '告变者姓名有异文：《后汉书》作唐周，《通鉴》作济阴人魏延，两说并记',
    ],
    '严嵩倒台': ['严世蕃以「通倭」论死一节历来有锻炼之疑'],
    '吕不韦奇货可居': ['「姬自匿有身」按《史记》书写并标为始皇出身争议之源头说，《汉书》已疑之'],
    '和珅跌倒': ['家产银数各本互异、后世推算尤多，不取；「和珅跌倒，嘉庆吃饱」属民谚层不入依据'],
    '康熙擒鳌拜': ['索额图进言情节与殿前对话原话来源未载，全弧转述，该条存疑'],
}

PHASES4 = ['起', '承', '转', '合']
ARC_CONF = {'海瑞备棺上治安疏': '高'}   # 其余第4批置信整体下调一档 → 中


def split4(n):
    base, rem = divmod(n, 4)
    return [base + (1 if i < rem else 0) for i in range(4)]


def main(apply: bool):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    data = json.loads((PKG / SRC).read_text(encoding='utf-8-sig'))
    arcs = [a for a in data['弧列表'] if a['弧名'] not in DROP]
    dropped = [a['弧名'] for a in data['弧列表'] if a['弧名'] in DROP]
    print(f'待入库弧: {len(arcs)}（撤下: {dropped}）')

    # 命名防撞：库内全名 + 批内互撞 → -N 递增
    taken = {r[0] for r in db.query(PlotTemplateORM.name).all()}
    key_seq: dict = {}
    names = {}
    for a in arcs:
        cls, sub = CLASSIFY[a['弧名']]
        base = f'{cls}--{sub}'
        key_seq.setdefault(base, 0)
        key_seq[base] += 1
        name = base if key_seq[base] == 1 and base not in taken else f'{base}-{key_seq[base]}'
        while name in taken:
            key_seq[base] += 1
            name = f'{base}-{key_seq[base]}'
        taken.add(name)
        names[a['弧名']] = name

    for a in arcs:
        patched = RING_PATCH.get(a['弧名'], {})
        beats = []
        for b in a['拍链']:
            ring = patched.get(b['拍序'], b['环草判'])
            beats.append({'beat': ring, 'variants': [{
                'src': f"{a['朝代']}·{a['弧名']}",
                'how': b['拍概要'],
                'desc': f"{b['拍概要']}｜依据：{b['原文依据']}",
            }]})
        n = len(beats)
        sizes = split4(n)
        phases, i = [], 0
        for pi, cnt in enumerate(sizes):
            phases.append({'phase': PHASES4[pi], 'beats': beats[i:i + cnt]})
            i += cnt
        cls, sub = CLASSIFY[a['弧名']]
        chain = ' → '.join(b['beat'] for b in beats)
        conf = ARC_CONF.get(a['弧名'], '中')
        d = {
            'name': names[a['弧名']],
            'scale': 'arc',
            'genre_tags': ['史书模板', cls, a['朝代'], '1源'],
            'logline': f'（{cls}--{sub}）1 条弧、1 源实证的情节骨架：{chain}。',
            'structure': {'phases': phases, 'skeleton': a},
            'pitfalls': PITFALLS.get(a['弧名'], []),
            'source_stats': {
                'origin': 'hist_v1', 'books': 1, 'book_names': [a['朝代']],
                'n_members': 1, 'single_arc': True,
                'class': cls, 'sub_event': sub,
                'member_arcs': [{'book': f"史书·{a['朝代']}", 'arc': a['弧名'],
                                 'src_ref': a['史源'], 'confidence': conf,
                                 'form_tag': '史实弧', 'judge_source': '契约草判+PM复核',
                                 'origin_gids': [], 'ch_lo': 0, 'ch_hi': 0}],
            },
            'status': 'active',
        }
        if apply:
            o = plot_template_crud.create(db, d)
            chunks = db.execute(
                text("SELECT COUNT(*) FROM vector_chunks WHERE source_type='plot_template' AND source_id=:sid"),
                {'sid': o.id}).scalar()
            print(f"[apply] {a['弧名']} → {d['name']} | {o.id[:8]}… 向量块 {chunks}")
        else:
            pend = sum(1 for b in beats if b['beat'] == '待定')
            print(f"[dry] {a['弧名']} → {d['name']}（{n} 拍，待定 {pend}）")

    if apply:
        db.commit()
        n_arc = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'")).scalar()
        print(f'\n[verify] arc active: 1040 → {n_arc}（预期 1057）')
        fake = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active' "
            "AND (name LIKE '赘婿剧情--%' OR name LIKE '史实战役--%')")).scalar()
        print(f'[verify] 假大类命名残留: {fake}（预期 0）')
        dup = db.execute(text(
            "SELECT COUNT(*) FROM (SELECT name, COUNT(*) c FROM plot_templates "
            "WHERE scale='arc' AND status='active' GROUP BY name HAVING c > 1)")).scalar()
        print(f'[verify] 重名模板: {dup}（预期 0）')
        # 新入 17 张键结构复核（Python 侧）
        bad = [o.name for o in db.query(PlotTemplateORM).filter(
            PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
            if (o.source_stats or {}).get('origin') == 'hist_v1' and '--' not in (o.name or '')]
        print(f'[verify] hist_v1 无键结构残留: {len(bad)}（预期 0）{bad or ""}')
    print('完成' if apply else '（dry-run 结束，加 --apply 正式入库）')


if __name__ == '__main__':
    main('--apply' in sys.argv)
