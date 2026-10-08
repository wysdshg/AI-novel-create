# [SK05I] 来源模板判类修正（62 张）：假大类「赘婿剧情/史实战役--弧名」→ 既有大类--子事件
# 依据：outputs/sk05h/判类提案_62弧.md（用户已批准，子事件全部复用既有词）
# 用法: python sk05i_reclassify.py            (dry-run)
#       python sk05i_reclassify.py --apply    (正式执行)
import json, sys, copy, re
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

# 弧名 → (大类, 子事件)——与判类提案_62弧.md 一一对应
MAP = {
    # ── 史书 20 ──
    '井陉之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    '官渡之战': ('守土防御', '智守·以计退敌'),
    '虎牢之战': ('大战征伐', '斩首夺旗·定点斩首'),
    '淝水之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    '香积寺之战': ('行军会战', '会战决战·主力对撞'),
    '鄢陵之战': ('行军会战', '会战决战·主力对撞'),
    '城濮之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    '崤之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    '长平之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    '巨鹿之战': ('行军会战', '会战决战·主力对撞'),
    '垓下之战': ('大战征伐', '覆国灭教·终局决战'),
    '潍水之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    '昆阳之战': ('守土防御', '守土御敌·据点死守'),
    '赤壁之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    '雪夜入蔡州': ('突袭伏击', '夜袭抄家·突袭老巢'),
    '采石之战': ('水战海战', '舟船交锋'),
    '郾城之战': ('行军会战', '会战决战·主力对撞'),
    '颍昌之战': ('守土防御', '守土御敌·据点死守'),
    '鄱阳湖之战': ('水战海战', '舟船交锋'),
    '萨尔浒之战': ('大战征伐', '诱敌歼灭·设伏合围'),
    # ── 赘婿 42 ──
    '上门赘婿才名初显': ('初入立足', '踏入新域·从零立足'),
    '双词惊世船屋反杀': ('商战经营', '产销扩张·打通商路'),
    '赈灾章程皇商风波': ('商战经营', '平台经营·抽成控市'),
    '覆乌家十步坡惊魂': ('寻仇报复', '灭门覆族·斩草除根'),
    '踏青诗会钱塘商路': ('商战经营', '产销扩张·打通商路'),
    '望海潮火夜陷城': ('围困突围', '被围脱困·撕开缺口'),
    '破湖州霸刀营入伙': ('军政整军', '选才募兵·整军备战'),
    '书院暗战楼家覆灭': ('智斗布局', '诱杀灭口·除掉对手'),
    '四季斋火起假婚惊变': ('婚恋联姻', '婚约风波·订退成婚'),
    '暴雨劫狱血洗苏府': ('救人援场', '救亲闯关·入险地救人'),
    '洪泽湖劫纲擒卢俊义': ('资源掠夺', '潜行掠夺·暗中下手'),
    '汴京诗战震慑高门': ('技艺比试', '文会雅集·才艺扬名'),
    '独龙岗心战破梁山': ('大战征伐', '势力火并·家族宗门内战'),
    '安平城中救红提': ('救人援场', '救亲闯关·入险地救人'),
    '方七佛押京血染吊桥': ('救人援场', '当场出手·救人于危'),
    '粮价商战逼世家放粮': ('商战经营', '资本运作·融资周转'),
    '入吕梁火雷退群匪': ('守土防御', '智守·以计退敌'),
    '青木成婚北关战起': ('大战征伐', '御敌死守·强敌压境'),
    '忻州血战宗师殉国': ('守土防御', '守土御敌·据点死守'),
    '夜袭牟驼岗夏村立垒': ('突袭伏击', '夜袭抄家·突袭老巢'),
    '夏村火雨太原再破': ('守土防御', '守土御敌·据点死守'),
    '逐相南归吕梁兴兵': ('夺权政变', '政变易主·夺权换旗'),
    '荒谷建国黑旗初举': ('开山建派', '立据建派·开基创业'),
    '董志塬炮碎铁鹞子': ('行军会战', '会战决战·主力对撞'),
    '金虏五伐苍河拒守': ('守土防御', '守土御敌·据点死守'),
    '泽州非人间晋王伏诛': ('突袭伏击', '定点斩首·杀主脑'),
    '邓州救孤对酒岳飞': ('救人援场', '护弱庇幼·救人立名'),
    '林冲之死凉山对峙': ('寻仇报复', '当众清算·旧怨了结'),
    '大名血战林州反间': ('大战征伐', '主动征伐·扫荡开疆'),
    '齐府火光新岁兵锋': ('突袭伏击', '定点斩首·杀主脑'),
    '龙船弑秦君武承祧': ('夺权政变', '政变易主·夺权换旗'),
    '荆湖夜袭梓州坚守': ('守土防御', '守土御敌·据点死守'),
    '望远桥火箭碎三万': ('大战征伐', '诱敌歼灭·设伏合围'),
    '西南围歼宗翰殒身': ('大战征伐', '诱敌歼灭·设伏合围'),
    '战后经纬成都聚义': ('新政改革', '新政改制·颁行新制'),
    '成都阅兵云中谍起': ('军政整军', '整编军队·重编编制'),
    '通山血案送亲劫道': ('寻仇报复', '当众清算·旧怨了结'),
    '五湖火并龙傲扬名': ('大战征伐', '势力火并·家族宗门内战'),
    '茶楼爆炸五王决裂': ('智斗布局', '借力布局·借刀杀人'),
    '江宁巷战土改春潮': ('新政改革', '新政改制·颁行新制'),
    '仙霞立摊福州暗潮': ('据点立足', '新域落脚·插旗扎下根'),
    '铁拳陨落怀云坊围杀': ('寻仇报复', '斩首擒主·定点猎杀'),
}
FAKE_CLASSES = {'赘婿剧情', '史实战役'}
DYNASTIES = {'春秋（东周）', '战国', '秦末', '汉', '楚汉', '东汉', '东汉末', '新莽末', '唐', '东晋', '南宋'}
OUT = Path(r'E:\AI小说创作\outputs\sk05h')


def main(apply):
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    log = []

    def w(m):
        print(m)
        log.append(m)

    # 取 62 张来源模板（🔴 E24：genre_tags 是 \u 转义 JSON，必须全取后 Python 侧解析）
    rows = db.query(PlotTemplateORM).filter(
        PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
    olds = [o for o in rows if any(t in {'史书模板', '赘婿模板'} for t in (o.genre_tags or []))]
    w(f'[定位] 来源模板: {len(olds)}（预期 62）')

    # 解析弧名 → 校验映射全覆盖
    arcname = {}
    for o in olds:
        ss = o.source_stats or {}
        arcname[o.id] = str(ss.get('sub_event') or '')
    missing = [a for a in arcname.values() if a not in MAP]
    w(f'[映射] 未覆盖弧名: {missing or "无"}')

    # 命名防撞：新键 + 既有 994 小说模板名 + 批内互撞 → -1/-2 后缀
    taken = {r[0] for r in db.execute(text("SELECT name FROM plot_templates")).fetchall()}
    assign = {}  # id -> new_name
    key_seq: dict = {}
    for o in sorted(olds, key=lambda x: x.id):
        arc = arcname[o.id]
        cls, sub = MAP[arc]
        key_seq.setdefault((cls, sub), 0)
        key_seq[(cls, sub)] += 1
        base = f'{cls}--{sub}'
        name = base if key_seq[(cls, sub)] == 1 and base not in taken else f'{base}-{key_seq[(cls, sub)]}'
        while name in taken:  # 与库内小说模板撞名时继续递增
            key_seq[(cls, sub)] += 1
            name = f'{base}-{key_seq[(cls, sub)]}'
        taken.add(name)
        assign[o.id] = (name, cls, sub)

    for o in olds:
        name, cls, sub = assign[o.id]
        w(f'  {o.name} → {name}')
        if not apply:
            continue
        # 确定性重建 tags：[来源标, 新大类, 朝代?, 1源]
        tags_old = list(o.genre_tags or [])
        src_tag = '史书模板' if '史书模板' in tags_old else '赘婿模板'
        dynasty = next((t for t in tags_old if t in DYNASTIES), None)
        o.name = name
        m = re.match(r'^（.*?）(.*)$', o.logline or '')
        tail = m.group(1) if m else (o.logline or '')
        o.logline = f'（{cls}--{sub}）{tail}'
        o.genre_tags = [src_tag, cls] + ([dynasty] if dynasty else []) + ['1源']
        ss = copy.deepcopy(o.source_stats or {})
        ss['class'], ss['sub_event'] = cls, sub
        o.source_stats = ss
        n = index_template(db, o)
        w(f'    [向量] 重嵌 {n} 块')

    if apply:
        db.commit()
        n_arc = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active'")).scalar()
        w(f'\n[verify] arc active 总数: {n_arc}（应与改前一致）')
        fake = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates WHERE scale='arc' AND status='active' "
            "AND (name LIKE '赘婿剧情--%' OR name LIKE '史实战役--%')")).scalar()
        w(f'[verify] 假大类命名残留: {fake}（预期 0）')
        nokey = db.execute(text(
            "SELECT COUNT(*) FROM plot_templates pt, json_each(pt.genre_tags) je "
            "WHERE pt.scale='arc' AND pt.status='active' "
            "AND je.value IN ('史书模板','赘婿模板') AND pt.name NOT LIKE '%--%'")).scalar()
        w(f'[verify] 来源模板无键结构残留: {nokey}（预期 0）')
        # Python 侧复核 source_stats 无假大类（E24：不能 LIKE 中文）
        left = [o.source_stats.get('class') for o in db.query(PlotTemplateORM).filter(
            PlotTemplateORM.scale == 'arc', PlotTemplateORM.status == 'active').all()
            if (o.source_stats or {}).get('class') in FAKE_CLASSES]
        w(f'[verify] source_stats 假大类残留: {len(left)}（预期 0）')
        dup = db.execute(text(
            "SELECT COUNT(*) FROM (SELECT name, COUNT(*) c FROM plot_templates "
            "WHERE scale='arc' AND status='active' GROUP BY name HAVING c > 1)")).scalar()
        w(f'[verify] 重名模板: {dup}（预期 0）')
        w('=== SK05I 完成 ===')
    else:
        w('（dry-run 结束，加 --apply 正式执行）')
    (OUT / 'SK05I_执行日志.txt').write_text('\n'.join(log), encoding='utf-8')


if __name__ == '__main__':
    main('--apply' in sys.argv)
