# [SK05F-3] 都市文词表增环：A14 人质劫持·对峙营救 / A15 循线围捕·缉凶擒拿 / C22 审讯逼供·问案对峙 + H13 定义放宽（用户 2026-10-10 拍板）
import sys, sqlite3
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

DB = r'C:\Users\w3013\.ai_novel\data\novel_agent.db'
NEW = [
    ('A14', '人质劫持·对峙营救', 'A',
     '劫匪挟持人质占据据点与警方或主角对峙，谈判周旋、强攻狙击或潜入营救——银行/校园/宅邸劫案皆此。核心=人质生死为筹码的对峙博弈。',
     '劫匪挟持人质据守，警方或主角围而对峙，谈判开场',
     '互换条件、排查内情、布控狙击位，周旋中找破绽',
     '谈判破裂或破绽暴露，强攻/狙击/潜入营救，枪声骤起',
     '人质获救或遇害落定，劫匪伏法或遁走，对峙收场'),
    ('A15', '循线围捕·缉凶擒拿', 'A',
     '沿线索排查追踪、布网围堵缉拿凶徒归案——顺线寻人、破门逼逃、借势围堵夺械擒凶皆此。核心=以线索链收网而非单点对决。',
     '案发立线，顺名单或线索逐点排查走访',
     '锁定藏处、破门逼逃、追入围堵圈，凶徒现形',
     '凶徒突围或持械反扑，围堵网层层收拢',
     '擒凶归案交执法处置，线索闭环或留余案'),
    ('C22', '审讯逼供·问案对峙', 'C',
     '审讯室问案、私刑逼供或假借查案套供——正邪双方在问答间博弈，供词与口风即战场。正审/私刑/伪装查案三种形态皆此。',
     '人被押上审讯席或落入问案局，攻守定调',
     '灯光讯问、私刑施压、供词攻防，口风一紧一松',
     '关键问题戳中要害或身份底牌暴露，僵持生变',
     '供词到手或问案者反被将，局势随口供改写'),
]
H13_NEW_DEF = ('伪造天意、神迹或谶语制造动员符号以聚拢人心——丹书鱼腹、篝火狐鸣、白土书谶、符水咒说皆此；'
               '装神弄鬼唬制个体（斩犬留头、假鬼逼命）亦此环变体：神异为假、威吓为实。'
               '与 H09 分界：H09 以真实主张做舆论宣传，本环以虚构天意制造信仰。')

db = sqlite3.connect(DB)
now = '2026-10-10 12:00:00.000000'
for id, name, cat, dfn, bs, bm, bt, be in NEW:
    if db.execute("SELECT 1 FROM atomic_events WHERE id=?", (id,)).fetchone():
        print(f'{id} 已存在，跳过'); continue
    db.execute("""INSERT INTO atomic_events
        (id, name, category_id, definition, beat_start, beat_mid, beat_turn, beat_end,
         is_core_capable, domain, scope_tags, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?, 'active', ?)""",
        (id, name, cat, dfn, bs, bm, bt, be, 1 if id == 'A14' else 0, 'general', None, now))
    print(f'已插入 {id} {name}')
# H13 定义放宽
cur = db.execute("SELECT definition FROM atomic_events WHERE id='H13'").fetchone()
if cur and '装神弄鬼' not in cur[0]:
    db.execute("UPDATE atomic_events SET definition=? WHERE id='H13'", (H13_NEW_DEF,))
    print("H13 定义已放宽（装神弄鬼变体并入）")
else:
    print('H13 已放宽或不存在，跳过')
db.commit()
n = db.execute("SELECT COUNT(*) FROM atomic_events").fetchone()[0]
print(f'环词表总数: {n}（预期 91）')

def patch_rings(p, anchor):
    t = Path(p).read_text(encoding='utf-8')
    if 'A14 人质劫持' in t:
        print(f'{Path(p).name}: 已含 A14，跳过'); return
    assert anchor in t, f'{p} 未找到锚点'
    Path(p).write_text(t.replace(anchor, '"A13 战后复盘", "A14 人质劫持·对峙营救", "A15 循线围捕·缉凶擒拿"'), encoding='utf-8')
    print(f'{Path(p).name}: A14/A15 已同步')

patch_rings(r'E:\AI小说创作\outputs\cr300\校验脚本.py', '"A13 战后复盘"')
patch_rings(r'E:\AI小说创作\outputs\史书模板任务包\校验脚本.py', '"A12 阵前斩将擒将","A13 战后复盘"')

def patch_c22(p, anchor):
    t = Path(p).read_text(encoding='utf-8')
    if 'C22 审讯逼供' in t:
        print(f'{Path(p).name}: 已含 C22，跳过'); return
    assert anchor in t, f'{p} 未找到 C22 锚点'
    Path(p).write_text(t.replace(anchor, '"C21 城池易主", "C22 审讯逼供·问案对峙"'), encoding='utf-8')
    print(f'{Path(p).name}: C22 已同步')

patch_c22(r'E:\AI小说创作\outputs\cr300\校验脚本.py', '"C21 城池易主"')
patch_c22(r'E:\AI小说创作\outputs\史书模板任务包\校验脚本.py', '"C21 城池易主"')

# 速览快照
vp = Path(r'E:\AI小说创作\outputs\原子事件词表速览.md')
t = vp.read_text(encoding='utf-8')
if 'A14' not in t:
    t = t.replace('SK05F-2 后 88 类', 'SK05F-3 后 91 类').replace('（88 类）', '（91 类）')
    for anchor, add in [
        ('| A13 |', '\n| A14 | 人质劫持·对峙营救 🆕 | 劫匪挟持人质占据据点与警方或主角对峙，谈判周旋、强攻狙击或潜入营救。核心=人质生死为筹码的对峙博弈。 | 0 | 0 |'
                     '\n| A15 | 循线围捕·缉凶擒拿 🆕 | 沿线索排查追踪、布网围堵缉拿凶徒归案。核心=以线索链收网而非单点对决。 | 0 | 0 |'),
        ('| C21 |', '\n| C22 | 审讯逼供·问案对峙 🆕 | 审讯室问案、私刑逼供或假借查案套供，供词与口风即战场。正审/私刑/伪装查案三形态皆此。 | 0 | 0 |'),
    ]:
        i = t.index(anchor)
        eol = t.index('\n', i)
        t = t[:eol] + add + t[eol:]
    t = t.replace('装神弄鬼唬制个体', '装神弄鬼唬制个体')  # H13 行如有旧定义则同步
    if 'H13 | 造谶惑众' in t and '装神弄鬼' not in t.split('H13 |')[1][:200]:
        i = t.index('H13 |')
        eol = t.index('\n', i)
        t = t[:eol] + '\n| H13 | 造谶惑众 ✏️ | 伪造天意神迹或谶语制造动员符号聚拢人心；装神弄鬼唬制个体（斩犬留头、假鬼逼命）亦此环变体。与 H09 分界：H09 以真实主张宣传，本环以虚构天意造信仰。 | 0 | 0 |' + t[eol:]
    vp.write_text(t, encoding='utf-8')
    print('速览快照: 91 类已同步')
else:
    print('速览快照: 已同步')

# cr300 契约词表清单
cp = Path(r'E:\AI小说创作\outputs\cr300\输出格式契约.md')
t = cp.read_text(encoding='utf-8')
if 'A14 人质劫持' not in t:
    t = t.replace('A13 战后复盘、B01', 'A13 战后复盘、A14 人质劫持·对峙营救、A15 循线围捕·缉凶擒拿、B01')
    t = t.replace('C21 城池易主、D01', 'C21 城池易主、C22 审讯逼供·问案对峙、D01')
    t = t.replace('环词表（88 类', '环词表（91 类')
    cp.write_text(t, encoding='utf-8')
    print('cr300 契约: 91 类已同步')
