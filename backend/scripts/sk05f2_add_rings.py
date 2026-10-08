# [SK05F-2] 词表增环：H12 以死明志 / H13 造谶惑众（用户 2026-10-08 拍板）
import sys, sqlite3
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

DB = r'C:\Users\w3013\.ai_novel\data\novel_agent.db'
NEW = [
    ('H12', '以死明志', 'H',
     '主角以自己的死为手段达成主张或承诺——两档：死谏（以死进谏、市棺诀别待罪于朝）与偿诺赴死（践托孤之诺、报知己而死）。核心=死亡本身是主动选择的表达手段，非战斗阵亡、非逃亡自尽。',
     '主角认定此事重于己命（进谏必触怒、承诺在身），决意以死相搏',
     '备后事、市棺诀别亲人待罪，或忍辱负重完成所托',
     '死谏触怒上位下狱论死，或承诺对象犹在、生死悬于一线',
     '目的达成或诺言已偿，从容赴死或死而名立'),
    ('H13', '造谶惑众', 'H',
     '伪造天意、神迹或谶语制造动员符号以聚拢人心——丹书鱼腹、篝火狐鸣、白土书谶、符水咒说皆此。与 H09 分界：H09 以真实主张做舆论宣传，本环以虚构天意制造信仰。',
     '要聚众起事或立威而人力号召不足，谋造天意符号',
     '丹书鱼腹、夜火狐鸣、书谶于门、符水咒说，符号悄然传开',
     '有人起疑或告发，符号面临戳穿风险',
     '众心归附「天命所归」，起事聚众成势'),
]

db = sqlite3.connect(DB)
now = '2026-10-08 12:00:00.000000'
for id, name, cat, dfn, bs, bm, bt, be in NEW:
    if db.execute("SELECT 1 FROM atomic_events WHERE id=?", (id,)).fetchone():
        print(f'{id} 已存在，跳过'); continue
    db.execute("""INSERT INTO atomic_events
        (id, name, category_id, definition, beat_start, beat_mid, beat_turn, beat_end,
         is_core_capable, domain, scope_tags, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?, 'active', ?)""",
        (id, name, cat, dfn, bs, bm, bt, be, 0, 'general', None, now))
    print(f'已插入 {id} {name}')
db.commit()
n = db.execute("SELECT COUNT(*) FROM atomic_events").fetchone()[0]
print(f'环词表总数: {n}（预期 88）')

# ── 同步 RINGS 副本 + 速览快照 ──
def patch_rings(p, anchor):
    t = Path(p).read_text(encoding='utf-8')
    if 'H12 以死明志' in t:
        print(f'{Path(p).name}: 已含 H12，跳过'); return
    assert anchor in t, f'{p} 未找到插入锚点'
    Path(p).write_text(t.replace(anchor, '"H11 贵人相助", "H12 以死明志", "H13 造谶惑众"'), encoding='utf-8')
    print(f'{Path(p).name}: RINGS 已同步')

# 史书脚本 RINGS 整体过期（A05 旧名、缺 SK05F 七环）——整行替换为最新 88 环
RINGS_LATEST = ('{"A01 单挑决斗","A02 擂台比试","A03 群殴混战","A04 设伏偷袭","A05 以弱胜强","A06 追击追杀",'
    '"A07 突围脱身","A08 反杀复仇","A09 猎兽夺丹","A10 军事筹谋与战前部署","A11 会战攻城与战役歼灭",'
    '"A12 阵前斩将擒将","A13 战后复盘","B01 拍卖竞价","B02 赌石切宝","B03 谈判交涉","B04 销赃出货",'
    '"B05 以物易物","B06 立约定契","B07 质押抵押","B08 非公平交易","B09 经营实业与商路","C01 集会宴请",'
    '"C02 试炼考核","C03 拜师结盟","C04 认亲婚约","C05 立威震慑","C06 招揽拉拢","C07 求助求援",'
    '"C08 问罪清算","C09 受托领命","C10 夺权易主","C11 身世揭秘","C12 收服部众","C13 安置建设据点",'
    '"C14 当众册封","C15 流放囚禁","C16 赈灾放粮与济贫","C17 民生治安与基层治理","C18 变法施政与律法",'
    '"C19 朝堂党争与奏章对质","C20 教化办学与育才","C21 城池易主","D01 身份暴露","D02 中毒疗伤",'
    '"D03 围困被困","D04 天劫雷劫","D05 夺舍反噬","D06 崩塌绝境","D07 诅咒缠身","D09 谍报刺探与反谍",'
    '"D10 匿藏","D11 军势失控","D12 天变助战","E01 潜入潜行","E02 赶路迁徙","E03 传送跨界",'
    '"E04 撤退退却","E05 关卡蒙混","F01 闭关突破","F02 炼丹炼药","F03 炼器铸兵","F04 参悟传承",'
    '"F05 吞噬异宝","F06 秘术献祭","F07 工艺试制与技术研发","G01 探墓开棺","G02 秘境夺宝",'
    '"G03 破阵解谜","G04 采集寻觅","G05 寻访查探","G06 主动布下阵局","G07 解读古籍秘录",'
    '"G08 工程兴工与勘测","H01 救人护人","H02 情愫生变","H03 背叛反目","H04 争执冲突","H05 诀别辞行",'
    '"H06 伤亡哀悼","H07 离间构陷","H08 宽恕放过","H09 舆情宣传与民心","H10 仪典祭祀与丧葬",'
    '"H11 贵人相助","H12 以死明志","H13 造谶惑众"}')

def sync_hist_rings(p):
    import re
    t = Path(p).read_text(encoding='utf-8')
    if 'H12 以死明志' in t:
        print(f'{Path(p).name}: 已同步，跳过'); return
    t2, n = re.subn(r'RINGS = \{.*?\}', f'RINGS = {RINGS_LATEST}', t, count=1, flags=re.S)
    assert n == 1
    Path(p).write_text(t2, encoding='utf-8')
    print(f'{Path(p).name}: RINGS 整体同步至 88 环（A05 改名+SK05F 七环+本次两环）')

sync_hist_rings(r'E:\AI小说创作\outputs\史书模板任务包\校验脚本.py')
patch_rings(r'E:\AI小说创作\outputs\cr300\校验脚本.py', '"H11 贵人相助", "待定"')

# 速览快照：头注 + H11 行后加两行
vp = Path(r'E:\AI小说创作\outputs\原子事件词表速览.md')
t = vp.read_text(encoding='utf-8')
if 'H12' not in t:
    t = t.replace('SK05F 后 86 类', 'SK05F-2 后 88 类').replace('（86 类）', '（88 类）')
    anchor = '| H11 | 贵人相助 🆕 |'
    i = t.index(anchor)
    eol = t.index('\n', i)
    add = ('\n| H12 | 以死明志 🆕 | 主角以自己的死为手段达成主张或承诺——两档：死谏（市棺诀别待罪于朝）与偿诺赴死（践托孤之诺）。核心=死亡是主动选择的表达手段。 | 0 | 0 |'
           '\n| H13 | 造谶惑众 🆕 | 伪造天意神迹或谶语制造动员符号聚拢人心（丹书鱼腹、篝火狐鸣、白土书谶、符水咒说）。与 H09 分界：H09 以真实主张宣传，本环以虚构天意造信仰。 | 0 | 0 |')
    t = t[:eol] + add + t[eol:]
    vp.write_text(t, encoding='utf-8')
    print('速览快照: 88 类已同步')
else:
    print('速览快照: 已同步，跳过')

# cr300 契约词表清单同步
cp = Path(r'E:\AI小说创作\outputs\cr300\输出格式契约.md')
t = cp.read_text(encoding='utf-8')
if 'H12 以死明志' not in t:
    t = t.replace('H11 贵人相助、待定', 'H11 贵人相助、H12 以死明志、H13 造谶惑众、待定')
    t = t.replace('环词表（86 类', '环词表（88 类')
    cp.write_text(t, encoding='utf-8')
    print('cr300 契约: 88 类已同步')
