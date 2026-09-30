# -*- coding: utf-8 -*-
"""原子事件库灌数据脚本（docs/10 P0）。

数据源：`outputs/原子事件表报告.md`（49 原子 / 8 大类 / 6 骨架）
      + `docs/10 §7` 豆包补充建议（转正 4 / 新增 6 / candidate 4）。
      + 2026-09-18 P1 人工抽查后的两条拍板：`G07 解读古籍秘录` 转正 active；
        新增 `B08 非公平交易` 进 candidate（段122 暴露交易类缺「非对价获取」原子）。
产物：8 大类 + **64 原子（60 active + 4 candidate）** + 6 骨架。

用法（在 backend/ 下执行）：

    ..\\.venv\\Scripts\\python.exe scripts/seed_atomic_events.py --dry-run
    ..\\.venv\\Scripts\\python.exe scripts/seed_atomic_events.py

**幂等**：按主键 upsert（存在则就地更新，不存在则插入），可任意重跑。
**非破坏性**：只写 atomic_categories / atomic_events / event_skeletons 三张表，
不触碰 plot_templates（164 条旧模板按 docs/10 §9 保留为回标素材）、
不触碰 atomic_variants（P1 才写入）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import create_engine, text                        # noqa: E402
from sqlalchemy.orm import sessionmaker                           # noqa: E402
from sqlalchemy.pool import StaticPool                            # noqa: E402

# ===========================================================================
# ① 8 个大类（docs/10 §7：大类仍 8 个，domain 全部 general）
# 分类依据 = 「事件发生的场域」（报告 §4.2），不按题材分。
# ===========================================================================
CATEGORIES = [
    ("A", "战斗", 10, "有对手身体对抗"),
    ("B", "交易", 20, "有对价交换"),
    ("C", "社交", 30, "有公开场合与身份关系变动"),
    ("D", "危机", 40, "主体处于被动受损状态"),
    ("E", "移动", 50, "空间位置改变"),
    ("F", "修炼", 60, "自身能力值改变"),
    ("G", "探索", 70, "获取未知物或信息"),
    ("H", "情感", 80, "人际关系状态改变"),
]

# ===========================================================================
# ② 64 个原子 = 60 active + 4 candidate
#   active     = 49 报告基线 + 转正 4（B06/C11/C12/G06）+ 新增 6（C13/C15/D07/F06/H07/H08）
#                + G07（2026-09-18 P1 抽查后转正）
#   candidate  = B07 质押抵押 / B08 非公平交易 / C14 当众册封 / D08 记忆遗失恢复
# 字段顺序：(id, name, category_id, definition, 起, 承, 转, 合, status)
# ===========================================================================
ATOMS = [
    # ---------------- 战斗类（9）----------------
    ("A01", "单挑决斗", "A", "两人以约战方式一对一了结恩怨",
     "因旧怨或挑战立下生死约", "当众对阵，招式与底牌逐层亮出",
     "一方施秘术服药，局势翻转", "一方败亡，恩怨当众了结", "active"),
    ("A02", "擂台比试", "A", "有规则有排名的公开比试中逐轮取胜",
     "大比或选拔开启，设名次与奖励", "逐轮淘汰，主角连胜引来关注",
     "主办方偏私或强敌加码下黑手", "夺魁晋级，扬名并得资源资格", "active"),
    ("A03", "群殴混战", "A", "多方势力在场打成一片，局势失控",
     "多方齐聚，利益或旧怨引爆", "主角被围攻或率众迎战，反复拉锯",
     "强援介入或战场突变（塌陷、自爆）", "一方溃退，主角趁势占资源或保体面", "active"),
    ("A04", "设伏偷袭", "A", "一方布局埋伏或掷暗箭，主角落入算计",
     "对方布下埋伏或暗箭", "偷袭得手，主角受伤同伴被制",
     "主角识破机关用底牌反打", "主谋被诛或逃走，埋下后续仇怨", "active"),
    ("A05", "越阶硬撼", "A", "面对明显强于己者硬扛并翻盘",
     "对手实力境界明显高出一档", "以秘物身法爆发力硬扛，几度被压",
     "借外力或临阵突破抹平差距", "以低境胜高境，震动旁观者", "active"),
    ("A06", "追击追杀", "A", "被强敌锁定后长途奔逃与反打",
     "被锁定或结死仇，追兵紧咬不放", "长途奔逃，途中遇地形与第三方势力",
     "反身一击或引第三方互斗脱困", "甩开或斩杀追兵，落脚新地点", "active"),
    ("A07", "突围脱身", "A", "从包围封锁中撕开缺口逃出生天",
     "被困局中（包围、地牢、封闭空间）", "试探出口，付出代价（弃宝、自伤）",
     "找到生门或强援登场，强撕缺口", "成功脱身，转入新场景留未了之局", "active"),
    ("A08", "反杀复仇", "A", "为血债追凶并当场了结",
     "主角受创或同伴被害，结下血债", "追查仇主、积蓄实力、克制隐忍",
     "与仇主正面对决，底牌尽出", "仇主毙命，震慑旁观者", "active"),
    ("A09", "猎兽夺丹", "A", "猎杀魔兽取其内丹晶核为己所用",
     "为取丹材入兽域或兽潮来袭", "与魔兽周旋，借地形设陷",
     "魔兽进阶暴走或引来更强争夺者", "取得内丹材料，实力随之提升", "active"),

    # ---------------- 交易类（5 + 立约定契 = 6）----------------
    ("B01", "拍卖竞价", "B", "在拍卖场上以叫价争夺珍品",
     "珍品上拍或主角携宝入场，强敌同席", "轮番加价，虚张声势试探底线",
     "搅局抬价、赝品被识破或规矩被压", "花落谁家，地位与资源重新分配", "active"),
    ("B02", "赌石切宝", "B", "押注原石并当众切开揭宝",
     "奇石原石摆上台前，各方叫价押注", "凭特殊感知挑选，开出惊人内里",
     "露宝招忌，两族争抢或邪物现形", "夺宝或弃宝脱身，名声与杀机同至", "active"),
    ("B03", "谈判交涉", "B", "双方坐定谈条件以交换或止战",
     "各有所需或势均力敌，坐下说条件", "试探亮牌互相压价，暗中各有算计",
     "一方亮底牌或第三势力插入", "立约或谈崩，共存或当场翻脸", "active"),
    ("B04", "销赃出货", "B", "把到手的宝物变现并分账",
     "宝物到手需变现或买家上门看货", "验货议价，消息在圈内扩散",
     "压价、掉包、货丢或仇家上门", "成交或砸手里，分钱散伙引出下一票", "active"),
    ("B05", "以物易物", "B", "用自有资源换所缺之物",
     "缺某样资源（药/丹方/法器/情报）", "找集市秘店或持有者，讨价还价",
     "对方抬价、加条件或以次充好", "成交得手，资源转为实力", "active"),
    ("B06", "立约定契", "B", "以期限或赌注立下契约约束各方",
     "双方僵持或需担保，须立约定限", "谈定条款、押上赌注或人质",
     "一方想反悔，或限期将至压力骤增", "契约成立，成为后续行动的时间压力", "active"),
    ("B07", "质押抵押", "B", "押上人或物换取资源，到期赎回或索赔",
     "急需资源或需担保，手中只有可押之物", "请中间人估价立契，押上重宝或至亲",
     "逾期无力赎回，或抵押物被暗中做手脚", "赎回或失去，关系与处境因此改变", "candidate"),
    # 2026-09-18 P1 人工抽查补入（段122「从不愿出售的店主手中取得工具」硬凑 B05 以物易物
    # → 暴露交易类缺「非对价获取」这一原子）。用户拍板：先进 candidate 待定池观察命中率。
    ("B08", "非公平交易", "B", "以威压、胁迫或信息差取得对方之物，非等价交换",
     "对方手里有主角必需之物，却不肯平等出让", "以势压人、半强迫或以假信息置换，逼对方就范",
     "对方服软交出，或反咬、事后报复", "东西到手但结下怨气，或交易当场被掀翻", "candidate"),

    # ---------------- 社交类（10 + 4 = 14）----------------
    ("C01", "集会宴请", "C", "在大会寿宴诗会等公开场合交锋",
     "大会寿宴诗会开场，各方齐聚", "席间比试论道试探与结仇同步",
     "有人当场挑衅行刺或爆出秘闻", "主角扬名或结怨，各方重新站队", "active"),
    ("C02", "试炼考核", "C", "在宗门学院的关卡考核中获得身份",
     "发布考核，规则与名额是赌注", "逐关闯过（猎兽、答题、斗阵、过塔）",
     "考官偏私或对手联手做局", "通过并获身份资源，被人记恨", "active"),
    ("C03", "拜师结盟", "C", "以本事换取师门或盟友的庇护",
     "主角势孤，需师门或盟友庇护", "以本事换取接纳（献药、解法、救人）",
     "对方试探利用甚至背叛", "结成名义关系，获资源与落脚点", "active"),
    ("C04", "认亲婚约", "C", "认亲或婚约牵出身份旧仇与交易",
     "婚约婚讯传来，牵出身份旧仇", "主角亮明身份或强行介入，各方角力",
     "被逼婚退婚或爆出已有身孕等变数", "亲缘婚约关系落定，格局改变", "active"),
    ("C05", "立威震慑", "C", "当众出手压制质疑并树立名声",
     "主角被轻视或新到一地，众人不服", "当众出手，以实绩压制质疑",
     "更高层人物现身，场面升级", "名声大振，资源与追随者随之而来", "active"),
    ("C06", "招揽拉拢", "C", "被各方开条件争取站队",
     "主角展露价值，各方上门相邀", "开出条件，各说各的好处",
     "被拒后转为威胁或暗中针对", "主角选一方或谁也不选，结下人情与仇怨", "active"),
    ("C07", "求助求援", "C", "危局中向外部求兵求药",
     "己方陷入危局，必须向外求援", "付出代价换取援手（人情、宝物、地盘）",
     "援兵迟到、条件加码或援手本身出事", "局面缓解或援手赶到扭转战局", "active"),
    ("C08", "问罪清算", "C", "当众审问对质并定性处置",
     "有人犯事或旧账被翻出", "审问逼供对质揭底，牵出更多人",
     "被问者反咬、真凶另有其人、上层压下", "定罪处罚或不了了之，阵营划清", "active"),
    ("C09", "受托领命", "C", "接下他人请托或高层派下的任务",
     "有人上门求助或高层派下任务", "提出条件、做准备（炼器、配药）",
     "任务中变数丛生（目标已死、被设局）", "任务了结或转向更大冲突，报酬到手", "active"),
    ("C10", "夺权易主", "C", "势力控制权在内外压力下转移",
     "势力内部派系争斗或外敌压境", "主角被卷入站队或扶植某人上位",
     "内鬼叛变、关键人物身亡或宗主倒向", "地盘易主，新势力接管，立场确立", "active"),
    ("C11", "身世揭秘", "C", "主角或某人的真实血脉与出身被揭开",
     "旧物、旧识或遗物透出线索，出身存疑", "顺着线索追查，亲人旧部态度反常",
     "当众揭开或由当事人亲口承认，身份翻覆", "身份落定，资源与仇怨随之重排", "active"),
    ("C12", "收服部众", "C", "主角收编人手或收服对手，为己所用",
     "主角手里有资源或名望，缺可用之人", "示恩立威并用，逐一试探忠诚",
     "有人不服挑衅或临阵反水", "部众归心，主角得班底与势力雏形", "active"),
    ("C13", "安置建设据点", "C", "为主角安置人手与经营地盘奠基",
     "主角有一块合法或抢来的地盘，无从经营", "清点人手、筹措物料，立规矩定分工",
     "旧势力反扑、内部分利不均或资源告急", "据点初立，成为落脚与后勤支点", "active"),
    ("C14", "当众册封", "C", "在公开典礼上受封名位与属地",
     "上层要示恩或需有人顶事，拟下封赏", "择日设典，当众宣读名位与领地",
     "有人质疑资格或旧势力暗中阻挠", "名位到手，一处地盘与人望随之到位", "candidate"),
    ("C15", "流放囚禁", "C", "主角或亲近者被夺权流放、囚禁",
     "斗争失利或背锅，被判流放囚禁", "押解途中受辱，困于荒僻之地",
     "狱中或流放地遇旧人、得秘闻或寻得脱身机会", "脱困回归或是被救离，心态与立场改变", "active"),

    # ---------------- 危机类（6 + 1 + 1 = 8；其中 D08 为 candidate）----------------
    ("D01", "身份暴露", "D", "伪装藏拙被识破，被迫换策略",
     "主角伪装或藏拙，勉力维持", "破绽累积（信物、功法、旧识认脸）",
     "当众被识破，通缉追杀随之而来", "改换身份离开原地，或反手压制知情者", "active"),
    ("D02", "中毒疗伤", "D", "中毒或重伤后的挣扎与恢复",
     "主角或同伴中毒重伤，命悬一线", "试药逼毒寻药，屡试屡挫",
     "毒性异变或治疗引来新麻烦", "伤愈但留隐患（黑指、损耗、被窥知）", "active"),
    ("D03", "围困被困", "D", "被封在险地中等待或寻找出路",
     "主角被封锁在险地（矿洞、地下、绝谷）", "粮水告急，试探出口，队伍内讧",
     "有人来救、被当人质或找到隐藏通道", "脱困或达成新交易，代价是欠下人情", "active"),
    ("D04", "天劫雷劫", "D", "引动天罚并借雷力淬炼己身",
     "境界或宝物引动天罚，雷云压顶", "硬抗雷击，肉身神魂受创",
     "借雷力淬体或引来外人趁火打劫", "渡劫成功实力暴涨，或重伤留后患", "active"),
    ("D05", "夺舍反噬", "D", "神魂被侵入后反吞或反噬失控",
     "主角或同伴神魂受侵（夺舍、走火入魔）", "意志拉锯，身体被外力接管",
     "以特殊体质或功法反吞入侵者", "反噬成功实力提升，留异象被人察觉", "active"),
    ("D06", "崩塌绝境", "D", "空间结构失稳导致退路断绝",
     "秘境圣地洞窟结构失稳，退路断绝", "乱象中抢最后一件宝物或救人",
     "空间法则断裂，被卷入虚无或随机传送", "侥幸脱身落于新地图，或与某人失散", "active"),
    ("D07", "诅咒缠身", "D", "被诅咒或血誓等外力长期侵蚀身体",
     "主角中咒或立下血誓，异象初显", "病症渐进，寻解法屡屡受挫",
     "查到施咒者或发现解咒之物的线索", "暂压制或解咒，留隐患与追查目标", "active"),
    ("D08", "记忆遗失恢复", "D", "记忆缺失或被篡改后的寻回",
     "主角或他人出现记忆空白、认识错位", "寻找旁证、旧物、旧地比照",
     "发现记忆被人动过手脚或出于自保", "部分恢复，真相与旧怨随之浮出", "candidate"),

    # ---------------- 移动类（3）----------------
    ("E01", "潜入潜行", "E", "伪装或潜行进入目标场地取物救人",
     "需进某处取物救人，不能正面来", "伪装身份避过巡逻，逐步深入",
     "被识破、遇见意外之人或撞上突发凶案", "得手撤离或被迫转为强攻", "active"),
    ("E02", "赶路迁徙", "E", "长途移动中边走边遇事",
     "目的地在远方，主角带队启程", "途中遇匪遇险遇旧识，边走边打",
     "路线被封锁或队伍有人掉队失散", "抵达新地，环境与规则全变", "active"),
    ("E03", "传送跨界", "E", "借传送阵或裂缝远遁换地图",
     "旧地无法立足，须借传送远遁", "筹齐传送条件（灵石、符箓、人情）",
     "传送出错、落入危险地带或封锁被卡", "抵达全新地域，从零开始立足", "active"),

    # ---------------- 修炼类（5 + 1 = 6）----------------
    ("F01", "闭关突破", "F", "闭关或觅机缘跨过修为瓶颈",
     "修为遇瓶颈，须闭关或寻机缘", "服丹炼体参悟，反复尝试",
     "突破引动天象或走火入魔，外人趁虚而入", "境界跃升，身份地位水涨船高", "active"),
    ("F02", "炼丹炼药", "F", "为疗伤突破或取信而开炉炼丹",
     "需某丹药（疗伤、突破、取信），药方在手", "备料控火试炼，屡败屡试",
     "丹成引天象、被人觊觎或药性有变", "成丹得用，或以丹术立名换资源地位", "active"),
    ("F03", "炼器铸兵", "F", "打造或重铸本命法宝重兵",
     "需重兵利器，或受托为他人铸器", "寻材打造，反复失败，器胚渐成",
     "器灵失控、雷劫引动或被人夺器", "得到本命法宝，战力倍增并引来觊觎", "active"),
    ("F04", "参悟传承", "F", "从功法秘典遗卷中悟得关键手段",
     "得到功法秘典遗卷，残缺或晦涩", "苦读参悟，与自身所学互相印证",
     "发现功法有暗伤陷阱，或参悟引动异象", "悟得关键一式或完整功法，战力质变", "active"),
    ("F05", "吞噬异宝", "F", "强吞与自身相斥的异宝并炼化",
     "遇到异火异宝或神兽精血，与自身相斥", "强吞炼化，肉身剧痛濒临崩溃",
     "与体内已有之物融合，或反噬失控", "异宝入体，功法进化实力暴涨也留隐患", "active"),
    ("F06", "秘术献祭", "F", "以血肉神魂等代价换取力量或改命",
     "走投无路或为救人破境而求捷径", "备祭品设阵，代价逐步兑现",
     "献祭超出预期，性情、寿元或亲近者受损", "所得到手但背负重债与后患", "active"),

    # ---------------- 探索类（5 + 1 + 1 = 7；其中 G07 为 candidate）----------------
    ("G01", "探墓开棺", "G", "组织下坑盗掘并开棺取物",
     "锁定某座古墓遗址，组织人手家伙", "探洞破机关下坑，逐层深入",
     "墓室已被先行者破坏，或机关反噬，或他人抢入", "取出陪葬品撤离，线索指向下一处", "active"),
    ("G02", "秘境夺宝", "G", "在开启的秘境中与各方争抢宝物",
     "秘境遗址开启，各方涌入争名额", "分头搜寻，与竞争者妖兽禁制周旋",
     "宝物出世或地图突变引发正魔混战", "抢得关键宝物并脱身，实力人望双收", "active"),
    ("G03", "破阵解谜", "G", "破解阵法机关禁制打开通路",
     "前路被阵法机关禁制封死", "观察规律、试探解法，队友分工",
     "解法反噬、阵中有阵或触发机关引来敌人", "破阵而入，取得阵后之物或打开通道", "active"),
    ("G04", "采集寻觅", "G", "亲自入野采得所需药材矿材",
     "需要某味药矿材料，须亲自去找", "入山入林，边寻边与人兽冲突",
     "找到时已被他人占据，或守物暴起", "采得材料，转化为丹药或战力", "active"),
    ("G05", "寻访查探", "G", "为解疑问四处打听追查线索",
     "有疑问未解（找人或找线索），开始打听", "多地问询，遇错人走错路，线索渐明",
     "查到大人物忌惮的内幕，被跟踪或警告", "拿到关键情报，锁定下一处目标", "active"),
    ("G06", "主动布下阵局", "G", "主角主动设阵布局，以阵御敌",
     "预判强敌或势力来犯，须提前设防", "勘察地形、筹措阵材、布置杀阵与诱饵",
     "敌人破阵或有人泄露阵眼", "阵起御敌成功或失守，主角掌握主动", "active"),
    ("G07", "解读古籍秘录", "G", "从看不懂的古籍秘录中解出关键信息",
     "得到一部文字艰深或残缺的古籍秘录", "逐字比对、寻人请教，解出零碎条目",
     "解出的内容指向机密或招来觊觎", "关键信息到手，牵出新的目标", "active"),

    # ---------------- 情感类（6 + 2 = 8）----------------
    ("H01", "救人护人", "H", "当场出手救下陷入危险的弱者或同伴",
     "同伴或弱者陷入危险，主角当场察觉", "出手相救，付出代价或暴露实力",
     "救人引来更强敌人，或被救者另有身份", "人救下并结下情义，为后续埋下伏笔", "active"),
    ("H02", "情愫生变", "H", "关系在并肩与阻碍中升温或转折",
     "与某人相处而羁绊渐生", "并肩历险照护伤病，亲密度上升",
     "身份婚约或第三人插入，关系受阻", "确立关系或忍痛分开，成为行动动机", "active"),
    ("H03", "背叛反目", "H", "并肩之人倒戈导致局势逆转",
     "并肩之人另有立场，秘密渐露", "主角起疑并试探，对方遮掩",
     "关键节点上倒戈，局势瞬间逆转", "叛者被清算或放走，主角戒心加重", "active"),
    ("H04", "争执冲突", "H", "因挑衅羞辱或利益当众起冲突",
     "对方当众挑衅、羞辱或逼让资源", "言语交锋，双方各自搬后台",
     "一方先动手或祭出更狠手段", "以压制退让或转为正式决斗收场", "active"),
    ("H05", "诀别辞行", "H", "因必然离开而与亲友作别",
     "主角必须离开（寻亲、避祸、赴远地）", "与亲友逐一道别，交代未了之事",
     "有人请求同行或留下托付，情绪推向高点", "断然上路，留下约定与牵挂作牵引", "active"),
    ("H06", "伤亡哀悼", "H", "同伴师长战死后安葬立誓",
     "一场恶战中同伴师长倒地", "主角试图施救或接手未了之事",
     "死者身份或遗愿揭出新的责任", "安葬立誓，悲怒转为复仇或守护动机", "active"),
    ("H07", "离间构陷", "H", "散布谣言或伪造证据使主角被孤立",
     "主角在阵营中站稳，招来忌惮", "谣言或伪证逐步发酵，旧识态度转冷",
     "主角当面拆穿或反手设局，构陷者暴露", "主谋被清算或反成公敌，主角重建信任", "active"),
    ("H08", "宽恕放过", "H", "抓获仇敌后选择放过以收人心",
     "仇敌落败被擒，众人围观等着处置", "主角权衡利害，压住杀意与众怒",
     "被放过者感激或日后反咬，旁人各执一词", "主角得人心或留后患，立下行事风格", "active"),
]

# ===========================================================================
# ③ 6 个骨架（报告 §2）
# steps 规则（可复现，见 docs/10 §2 的「流程」与「可选环节与触发条件」两段）：
#   · required=True  —— 列在「流程」主链上，且未被「可选环节」以
#                       「仅在…」「才挂」「可省」限定；
#   · required=False —— 被「可选环节」限定（trigger 记明挂载条件）或不在主链上。
# order = 组装时的环节位置（可选环节也占位，因为它是骨架的一部分）。
# ===========================================================================
SKELETONS = [
    ("S01", "拍卖会风云", "B01", [
        {"atomic_id": "D01", "required": False, "order": 1,
         "trigger": "主角是伪装身份入场时的可选前段"},
        {"atomic_id": "C01", "required": True, "order": 2, "trigger": None},
        {"atomic_id": "B01", "required": True, "order": 3, "trigger": None},
        {"atomic_id": "H04", "required": True, "order": 4, "trigger": None},
        {"atomic_id": "A04", "required": False, "order": 5,
         "trigger": "竞价出现搅局者、或主角亮出底牌被人识破时挂上"},
        {"atomic_id": "A06", "required": False, "order": 6,
         "trigger": "主角拍得关键物后挂上；若只旁观竞价，骨架在 H04 收束"},
        {"atomic_id": "A07", "required": True, "order": 7, "trigger": None},
    ], ["九星霸体诀#5", "斗破苍穹#5", "斗破苍穹#7", "斗破苍穹#18",
        "斗破苍穹#23", "太荒吞天诀#20", "蛊真人#20"]),

    ("S02", "宗门大比立威", "A02", [
        {"atomic_id": "C02", "required": True, "order": 1, "trigger": None},
        {"atomic_id": "C01", "required": True, "order": 2, "trigger": None},
        {"atomic_id": "A02", "required": True, "order": 3, "trigger": None},
        {"atomic_id": "A04", "required": False, "order": 4,
         "trigger": "比试被第三方设局时的可选段（太荒#22 雇佣杀手、九星#14 卧底阴谋）"},
        {"atomic_id": "H04", "required": True, "order": 5, "trigger": None},
        {"atomic_id": "C05", "required": True, "order": 6, "trigger": None},
        {"atomic_id": "C06", "required": False, "order": 7,
         "trigger": "仅在主角在比试中露出超凡资质后挂上（太荒#4、蛊真人#22）"},
    ], ["太荒吞天诀#22", "斗破苍穹#19", "斗破苍穹#24", "蛊真人#20",
        "太荒吞天诀#4", "太荒吞天诀#30"]),

    ("S03", "秘境夺宝", "G02", [
        {"atomic_id": "E03", "required": True, "order": 1, "trigger": None},
        {"atomic_id": "G03", "required": True, "order": 2, "trigger": None},
        {"atomic_id": "G02", "required": True, "order": 3, "trigger": None},
        {"atomic_id": "A04", "required": True, "order": 4, "trigger": None},
        {"atomic_id": "A03", "required": True, "order": 5, "trigger": None},
        {"atomic_id": "D06", "required": False, "order": 6,
         "trigger": "秘境结构失稳的写法才挂（太荒#58、凡人#33、九星#21）"},
        {"atomic_id": "A07", "required": True, "order": 7, "trigger": None},
        {"atomic_id": "H01", "required": False, "order": 8,
         "trigger": "救下同场被困者后挂上，同时是后续人情线的入口（凡人#25、太荒#55）"},
        {"atomic_id": "H03", "required": False, "order": 9,
         "trigger": "盟友设伏背叛是高频变体（凡人#25、九星#23）"},
    ], ["凡人修仙传#25", "太荒吞天诀#55", "九星霸体诀#21",
        "凡人修仙传#33", "太荒吞天诀#58"]),

    ("S04", "追杀逃生", "A06", [
        {"atomic_id": "A04", "required": True, "order": 1, "trigger": None},
        {"atomic_id": "D02", "required": False, "order": 2,
         "trigger": "追杀起因是主角已负伤时前置（九星#33、九星#34）"},
        {"atomic_id": "A06", "required": True, "order": 3, "trigger": None},
        {"atomic_id": "D03", "required": False, "order": 4,
         "trigger": "追兵封路或落入封闭地形时挂（凡人#28 传送阵被封锁、九星#29 同伴断后被困）"},
        {"atomic_id": "A07", "required": True, "order": 5, "trigger": None},
        {"atomic_id": "E02", "required": False, "order": 6,
         "trigger": "与 E03 二选一收尾：以长途赶路转入新地"},
        {"atomic_id": "E03", "required": False, "order": 7,
         "trigger": "与 E02 二选一收尾：以传送跨界换地图"},
        {"atomic_id": "D04", "required": False, "order": 8,
         "trigger": "逃亡途中撞上渡劫，可作「转」（九星#33 山林遇天劫反成机缘）"},
    ], ["九星霸体诀#33", "凡人修仙传#31", "九星霸体诀#29",
        "凡人修仙传#19", "太荒吞天诀#37"]),

    ("S05", "认亲定局", "C04", [
        {"atomic_id": "C09", "required": False, "order": 1,
         "trigger": "若主角是自己找上门而非受托，前段可省（九星#2 重修婚约）"},
        {"atomic_id": "E02", "required": True, "order": 2, "trigger": None},
        {"atomic_id": "C04", "required": True, "order": 3, "trigger": None},
        {"atomic_id": "H04", "required": True, "order": 4, "trigger": None},
        {"atomic_id": "A04", "required": False, "order": 5,
         "trigger": "认亲牵出投毒/夺产阴谋时挂（太荒#41 蚀日毒、太荒#42 银针证据）"},
        {"atomic_id": "H01", "required": True, "order": 6, "trigger": None},
        {"atomic_id": "C10", "required": False, "order": 7,
         "trigger": "家族内部分家/派系斗争写法才挂（太荒#41、太荒#50、太荒#64）"},
    ], ["太荒吞天诀#41", "太荒吞天诀#29", "斗破苍穹#1",
        "九星霸体诀#6", "太荒吞天诀#21"]),

    ("S06", "盗墓起棺", "G01", [
        {"atomic_id": "G05", "required": True, "order": 1, "trigger": None},
        {"atomic_id": "E02", "required": True, "order": 2, "trigger": None},
        {"atomic_id": "G01", "required": True, "order": 3, "trigger": None},
        {"atomic_id": "G03", "required": True, "order": 4, "trigger": None},
        {"atomic_id": "D03", "required": True, "order": 5, "trigger": None},
        {"atomic_id": "A07", "required": True, "order": 6, "trigger": None},
        {"atomic_id": "B04", "required": True, "order": 7,
         "trigger": None},
    ], ["北派盗墓笔记#8", "北派盗墓笔记#21", "北派盗墓笔记#44",
        "北派盗墓笔记#54", "北派盗墓笔记#60"]),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="灌入原子事件库（docs/10 P0）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将写入/更新的行数，不落库")
    args = ap.parse_args()

    # 一致性自检：原子 id 唯一、category 存在、骨架引用的 atomic_id 必须在表内
    atom_ids = [a[0] for a in ATOMS]
    assert len(atom_ids) == len(set(atom_ids)), "原子 id 重复"
    cat_ids = {c[0] for c in CATEGORIES}
    for a in ATOMS:
        assert a[2] in cat_ids, f"{a[0]} 的 category {a[2]} 不存在"
    for sid, name, core, steps, _ev in SKELETONS:
        assert core in atom_ids, f"{sid} 核心原子 {core} 不在原子表"
        for st in steps:
            assert st["atomic_id"] in atom_ids, f"{sid} 引用了不存在的原子 {st['atomic_id']}"
        orders = [s["order"] for s in steps]
        assert orders == sorted(orders) and len(set(orders)) == len(orders), f"{sid} order 异常"

    n_active = sum(1 for a in ATOMS if a[8] == "active")
    n_cand = sum(1 for a in ATOMS if a[8] == "candidate")
    print(f"数据源自检通过：大类 {len(CATEGORIES)}｜原子 {len(ATOMS)}"
          f"（active {n_active} + candidate {n_cand}）｜骨架 {len(SKELETONS)}")

    import app.core.database as dbmod
    from app.models.orm import (AtomicCategoryORM, AtomicEventORM,
                               EventSkeletonORM, AtomicVariantORM)

    # 🔴 本机硬需求：StaticPool 单连接（多连接在本机文件虚拟化下互不可见）
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    if args.dry_run:
        cur = {}
        for label, model in (("categories", AtomicCategoryORM), ("events", AtomicEventORM),
                             ("skeletons", EventSkeletonORM), ("variants", AtomicVariantORM)):
            try:
                cur[label] = db.query(model).count()
            except Exception as e:  # noqa: BLE001  表尚未创建
                cur[label] = f"表不存在({type(e).__name__})"
        print(f"[dry-run] 当前库内：{cur}")
        print(f"[dry-run] 本次将 upsert：categories={len(CATEGORIES)} "
              f"events={len(ATOMS)} skeletons={len(SKELETONS)}；"
              f"不写 atomic_variants、不触碰 plot_templates")
        db.close()
        return 0

    dbmod.init_db()   # 先确保 4 张表存在（create_all + 增量加列，幂等）

    ins = upd = 0

    def _upsert(model, key, vals):
        """按主键 upsert。created_at 交给 ORM 默认值（更新时不动它，保留首次灌入时间）。"""
        nonlocal ins, upd
        row = db.get(model, key)
        if row is None:
            db.add(model(id=key, **vals))
            ins += 1
        else:
            for k, v in vals.items():
                setattr(row, k, v)
            upd += 1

    for cid, name, order, note in CATEGORIES:
        _upsert(AtomicCategoryORM, cid,
                {"name": name, "domain": "general", "sort_order": order, "note": note})

    for aid, name, cat, definition, b1, b2, b3, b4, status in ATOMS:
        _upsert(AtomicEventORM, aid,
                {"name": name, "category_id": cat, "definition": definition,
                 "beat_start": b1, "beat_mid": b2, "beat_turn": b3, "beat_end": b4,
                 "is_core_capable": True, "domain": "general",
                 "scope_tags": None, "status": status})

    for sid, name, core, steps, evidence in SKELETONS:
        _upsert(EventSkeletonORM, sid,
                {"name": name, "core_atomic": core, "steps": steps,
                 "domain": "general", "scope_tags": None, "evidence": evidence,
                 "status": "active"})

    db.commit()

    # 🔴 收尾：同连接 checkpoint，否则 WAL 帧对跨进程读者不可见（本机硬需求）
    try:
        ck = db.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
        print(f"checkpoint(PASSIVE): {ck}")
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint 失败: {type(e).__name__}: {e}")

    print(f"\n写入完成：新增 {ins} 行 / 更新 {upd} 行")
    print("===== 分类计数 =====")
    for cid, cname, _o, _n in CATEGORIES:
        tot = db.query(AtomicEventORM).filter(AtomicEventORM.category_id == cid).count()
        act = (db.query(AtomicEventORM)
               .filter(AtomicEventORM.category_id == cid,
                       AtomicEventORM.status == "active").count())
        can = (db.query(AtomicEventORM)
               .filter(AtomicEventORM.category_id == cid,
                       AtomicEventORM.status == "candidate").count())
        print(f"  {cid} {cname:<4} 合计 {tot:>2}（active {act} / candidate {can}）")

    print("===== 总账 =====")
    print(f"  atomic_categories : {db.query(AtomicCategoryORM).count()}")
    print(f"  atomic_events     : {db.query(AtomicEventORM).count()}"
          f"（active {db.query(AtomicEventORM).filter_by(status='active').count()}"
          f" + candidate {db.query(AtomicEventORM).filter_by(status='candidate').count()}）")
    print(f"  atomic_variants   : {db.query(AtomicVariantORM).count()}（P0 应为 0）")
    print(f"  event_skeletons   : {db.query(EventSkeletonORM).count()}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
