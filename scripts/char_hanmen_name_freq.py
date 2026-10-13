# -*- coding: utf-8 -*-
"""统计候选名在正文的命中章数（用于并档正名取高频者）+ 别名交叉污染核对。"""
import glob, json, os, re, collections

D = "小说/寒门枭士"
TXT = {}
for f in glob.glob(os.path.join(D, "*.txt")):
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

PAIRS = [("九公主", "陈文儿"), ("九公主", "舞阳公主"), ("九公主", "陈佶"),
         ("小北", "唐小北"), ("关小娥", "小娥"), ("露洁", "露洁公主"),
         ("魏先生", "魏无涯"), ("冯先生", "冯圣"), ("尕达", "尕达赞普"),
         ("亲卫副队长", "副队长"), ("周大人", "山羊胡"), ("赵县尉", "赵廷之"),
         ("老谭", "谭先生"), ("满仓", "张满仓"), ("婉娘", "润娘"),
         ("铁牛", "方雷"), ("铁锤", "铁锤")]

def chap_hits(name):
    return sum(1 for t in TXT.values() if name in t)

def total_hits(name):
    return sum(t.count(name) for t in TXT.values())

rows = []
for a, b in PAIRS:
    rows.append((a, b, chap_hits(a), chap_hits(b)))
print(f"{'名A':8s}{'名B':10s}{'A章':>6}{'B章':>6}{'高频正名':>10}")
for a, b, ca, cb in rows:
    print(f"{a:8s}{b:10s}{ca:>6}{cb:>6}{(a if ca >= cb else b):>10}")

GEN = ["公主", "殿下", "陛下", "皇帝", "夫人", "掌柜", "大当家", "二当家", "先生", "队长", "连长",
       "排长", "班长", "校尉", "将军", "侯爷", "大人", "老爷", "姑娘", "嫂子", "郡守", "县令",
       "村长", "管家", "镖师", "亲卫", "谋士", "幕僚", "统领", "单于", "赞普", "国王", "太后",
       "丞相", "太尉", "御史", "太守", "里正", "东家", "船娘", "死士", "细作", "探子", "向导"]
print("\n== 裸泛称命中章数（若被喂进 --names 会大面积误匹配）==")
for g in GEN:
    c = chap_hits(g)
    if c >= 5:
        print(f"  {g:5s} {c:>5}章", end="")
print()

alias = json.load(open("outputs/char_hanmen/花名册汇总.json", encoding="utf-8"))["正名→别称"]
print("\n== 现花名册里仍挂在人名的裸泛称（v2 需摘）==")
for nm, al in alias.items():
    bare = [a for a in al if a in set(GEN)]
    if bare:
        print(f"  {nm}: {bare}")
