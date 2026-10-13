# -*- coding: utf-8 -*-
"""花名册修正 v2：并档（正名取正文高频形式）+ 摘除误挂别名 + 裸泛称剥离清单。
依据全部为正文实测：形式频率（去子串裸用章数）与同章共现（同人铁证 / 异人铁证）。"""
import json, glob, os, re

TXT = {}
for f in glob.glob("小说/寒门枭士/*.txt"):
    b = os.path.basename(f)
    m = re.match(r"(\d+)_", b)
    if not m:
        continue
    t = ""
    for enc in ("utf-8", "gb18030"):
        try:
            t = open(f, encoding=enc).read()
            break
        except UnicodeDecodeError:
            pass
    TXT[int(m.group(1))] = t

def ch(name):
    return sorted(n for n, t in TXT.items() if name in t)

def co(a, b):
    return sorted(n for n, t in TXT.items() if a in t and b in t)

R = json.load(open("outputs/char_hanmen/花名册汇总.json", encoding="utf-8"))["正名→别称"]

MERGE = [
 # (保留正名, 降为别称者, 同人判据)
 ("九公主", "陈文儿", "同章两称并用：c318 原文「你看那是魏先生吗」式互证；c460-461 巫哲称「大康公主陈文儿」，c371 庆慕岚称「舞阳是陛下与庆妃所生、排行第九」"),
 ("小娥", "关小娥", "同章共现14章(c3,16,17,18,37…)，同一人被两种叫法并用；关小娥仅14章、小娥111章"),
 ("露洁公主", "露洁", "同章并用；完整称呼「露洁公主」166章 > 裸露洁64章，取完整名为正名以免与「露洁夫人/露洁妹妹」混淆"),
 ("魏无涯", "魏先生", "同章共现63章；c318 原文先称「魏先生」随后写明「来人正是…魏无涯」；魏无涯112章 > 魏先生94章"),
 ("冯先生", "冯圣", "同章共现16章；c309 同段先「那就听冯先生的」后「我冯圣发誓此生与此人不死不休」；冯先生82章 > 冯圣27章"),
 ("尕达", "尕达赞普", "同章共现8章(c372,437,445,449,450)；尕达81章 > 尕达赞普8章"),
 ("赵县尉", "赵廷之", "同章共现5章(c282,304,305,306,307)；赵县尉37章 > 赵廷之5章"),
 ("满仓", "张满仓", "同章共现23章(c2,3,5,6,10,11…)同段两称并用；裸满仓206章 > 张满仓23章"),
 ("铁牛", "方雷", "方雷仅 c52 一章（庆怀称「张凉的黑大个战友…方雷」），c105 同一黑大个被唤「铁牛大哥」；两称 0 同章共现＝改名而非两人；铁牛123章 > 方雷1章"),
 ("老谭", "谭先生", "谭先生仅 c401 一章与老谭同章并用；老谭41章 > 谭先生1章"),
]
NO_MERGE = [
 ("小北", "唐小北", "**两个活人**：小北=c32 起西河湾十岁纺线村女（叫金锋锋哥哥），唐小北=c188 起唐冬冬堂妹、春风楼头牌后随军掌柜。c190 起两称同段并用只发生在唐小北身上；两人 481 章共现纯属子串包含，不是同人证据",
  "村女小北真身＝同章无「唐小北」的 44 章（c32~c187 段）；唐小北＝481 章"),
 ("润娘", "婉娘", "**两个活人**：同章共现 7 章（c230,269,416,832,855,1127…）＝同时在场；婉娘是 c145 起铁罐山安置的二十出头姑娘，润娘是谢光之妻后留西河湾帮工",
  "润娘314章／婉娘27章，各自独立成条"),
 ("副队长", "亲卫副队长", "「副队长」是多职衔泛称（另有死士副队长、镖师副队长），12 章同章共现不足以并档",
  "[待定] 建议亲卫副队长保名，裸副队长整条摘出喂料"),
]
STRIP = [
 ("九公主", "陈佶", "陈佶是当朝皇帝（九公主之父/先皇一线），**同章共现 12 章**(c493,494,504,611,733,743…)＝两人同场，绝不可能是别名；现 陈佶 252 章里仅 36 章为「同章无九公主/陈文儿」"),
 ("小北", "唐小北", "村女条目挂了堂妹的名字，直接把唐小北 481 章吞进小北 525 章"),
 ("小北", "小北姑娘", "「小北姑娘」在 c190~196 指唐小北（春风楼头牌称呼叫法）"),
 ("唐小北", "小北", "裸「小北」239 章含村女用法，喂密度会把 c32~c187 算进她"),
 ("九公主", "舞阳", "「舞阳」321 章中 4 章作地名「舞阳县」用（c371,767,769,812），作别称会误匹配；完整称呼「舞阳公主」8 章可保留"),
 ("金锋", "先生", "裸「先生」1619 章为通称（教书匠、郎中、乡绅皆可称先生）"),
 ("陈文儿", "殿下", "裸「殿下」313 章泛指皇族"),
 ("九公主", "皇帝", "裸「皇帝」436 章多指陈佶/新帝"),
 ("九公主", "陛下", "裸「陛下」421 章多指陈佶"),
 ("九公主", "公主", "裸「公主」1130 章含露洁公主/太平永乐等"),
]
GEN = ["先生", "公主", "殿下", "陛下", "皇帝", "夫人", "掌柜", "大当家", "二当家", "队长", "连长", "排长",
       "班长", "校尉", "将军", "侯爷", "大人", "老爷", "姑娘", "嫂子", "郡守", "县令", "村长", "管家",
       "镖师", "亲卫", "谋士", "幕僚", "统领", "单于", "赞普", "死士", "细作", "探子", "向导", "里正", "东家", "船娘"]
hung = []
for nm, al in R.items():
    for a in al:
        if a in GEN:
            hung.append({"正名": nm, "误挂裸泛称": a, "该词正文命中章数": len(ch(a))})

new = {k: list(v) for k, v in R.items()}
for keep, drop, why in MERGE:
    new.setdefault(keep, [])
    new[keep] = sorted(set(new.get(keep, [])) | set(new.get(drop, [])) | {drop})
    if drop in new:
        del new[drop]
for nm, a in [(s[0], s[1]) for s in STRIP]:
    if nm in new and a in new[nm]:
        new[nm].remove(a)

out = {
 "书": "寒门枭士", "版本": "花名册修正 v2（P1/P2 中期校准后，按 PM 裁决①②）",
 "口径": "正名一律取「正文高频形式」（按去子串后的裸用章数与同章并用判据实测）；并档判据只用原文同章并用/同章共现，不用子串包含",
 "并档": [{"正名": k, "降为别称": d, "判据": w,
           "正名命中章数": len(ch(k)), "别名命中章数": len(ch(d)),
           "同章共现章数": len(co(k, d)), "同章共现样例": co(k, d)[:5]} for k, d, w in MERGE],
 "不并档": [{"条目": a, "疑被混作": b, "判据": w, "真身章数": cz} for a, b, w, cz in NO_MERGE],
 "摘除误挂": [{"正名": n, "摘除别名": a, "判据": w, "该别名正文命中章数": len(ch(a))} for n, a, w in STRIP],
 "仍需剥离的裸泛称挂载": sorted(hung, key=lambda x: -x["该词正文命中章数"]),
 "修正后条目数": len(new),
 "修正后名册": {k: sorted(v) for k, v in sorted(new.items())},
}

# ---- 可直接喂 f8_density_stats.py --names 的白名单 ----
strip_table = json.load(open("outputs/char_hanmen/泛称剥离表.json", encoding="utf-8"))
doomed = {(x["正名"], x["别称"]) for x in strip_table["剥离清单"]}
nick = set(json.load(open("outputs/char_hanmen/花名册汇总.json", encoding="utf-8"))["诨号(不进names不喂密度)"])
GENSET = set(GEN)
# 职衔/称谓/自称词库：整条别名若只由这些词拼成 → 不可归属，剔除
TITLES = """公主 殿下 陛下 皇帝 女王 皇上 娘娘 王爷 王妃 太子 驸马 国师 丞相 宰相 太尉 御史 尚书
将军 统领 都尉 校尉 司马 太守 郡守 县令 知县 大人 老爷 夫人 太太 姑娘 小姐 嫂子 婶子 大姐 妹妹 姐姐
哥哥 弟弟 叔叔 大爷 老者 先生 老师 师父 师傅 师兄 师姐 队长 连长 排长 班长 营长 团长 厂长 船长 馆长
东家 掌柜 管家 伙计 镖师 亲卫 护卫 死士 细作 探子 密谍 斥候 旗手 水手 船娘 向导 谋士 幕僚 文书 文档
仆人 丫鬟 婢女 奴仆 家丁 门房 厨子 铁匠 郎中 大夫 巫医 单于 赞普 可汗 土司 头人 当家 首领 头目 王者
朕 本宫 寡人 夫君 浑家 相公 官人 少爷 公子 小姑娘 老朽 在下 大人们 当今陛下 先皇 今上 皇上""".split()
def only_titles(a):
    """别名能否被职衔词整段覆盖（覆盖=不专指某人)"""
    rest = a
    for t in sorted(TITLES, key=len, reverse=True):
        rest = rest.replace(t, "")
    return rest == ""

allnames = set(new)
white = {}
for k, v in new.items():
    keep = []
    for a in v:
        if a == k:
            continue                      # 正名不重复列
        if a in GENSET or only_titles(a):
            continue                      # 裸/纯职衔泛称
        if (k, a) in doomed:
            continue                      # 泛称剥离表判定的撞车项
        if any(a != n and a in n for n in allnames):
            continue                      # 该别名是别人正名的子串（如「小北」之于「唐小北」）
        if not re.fullmatch(r"[\u4e00-\u9fa5·]{2,8}", a):
            continue                      # 乱码/拼音/含空格项（如 Mancang）
        keep.append(a)
    white[k] = keep
out["喂密度白名单"] = {"说明": "每行 = 正名<TAB>可归属别称。剔除依据：①正名自身 ②纯职衔/自称词 ③泛称剥离表撞车项 ④是别人正名的子串（防吞章）⑤乱码拼音项 ⑥v2 新判的误挂。诨号条目（256 条）不在此表内。",
 "行数": sum(1 for k in white if k not in nick),
 "行": [f"{k}\t{','.join(v)}" if v else k for k, v in sorted(white.items()) if k not in nick]}
out["白名单统计"] = {"保留别称条数": sum(len(v) for k, v in white.items() if k not in nick),
 "被再剔除条数": sum(len(v) for k, v in new.items() if k not in nick) - sum(len(v) for k, v in white.items() if k not in nick)}


json.dump(out, open("outputs/char_hanmen/花名册修正v2.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
open("outputs/char_hanmen/names_hanmen_v2.txt", "w", encoding="utf-8").write(
 "\n".join(out["喂密度白名单"]["行"]) + "\n")
print("并档", len(MERGE), "组 | 不并档", len(NO_MERGE), "组 | 摘除误挂", len(STRIP), "处 | 裸泛称挂载", len(hung), "处")
print("修正后条目数:", len(new), "（原 622）")
print("白名单可喂行数:", len(out["喂密度白名单"]["行"]), "| 保留别称", out["白名单统计"]["保留别称条数"], "| 再剔除", out["白名单统计"]["被再剔除条数"])
print("示例：并档后 九公主 别称 =", new["九公主"][:12])
print("      满仓 别称 =", new["满仓"][:10], "| 张满仓 已删:", "张满仓" not in new)
print("      小北 别称 =", new.get("小北"), "| 唐小北 别称 =", new.get("唐小北")[:12])
print("      白名单九公主行 =", [l for l in out["喂密度白名单"]["行"] if l.startswith("九公主")])
print("      白名单小北行 =", [l for l in out["喂密度白名单"]["行"] if l.startswith("小北\t")])

