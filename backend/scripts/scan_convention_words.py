# -*- coding: utf-8 -*-
"""E3 惯例词校验：种子条目在 7 书语料中的跨书出现数（G3 拍板：≥2 本即收）。

采样每本书前 200 章原文（bytes 单次扫描全部关键词），输出：
- 达标（≥2 本）/ 不达标（<2 本）的种子词清单
- 为人工复核提供依据；不达标的词进 disabled 待议，不删（可回滚）。
"""
import os
import sys

BASE = r"E:/AI小说创作/小说"
SAMPLE = 400
# 全部书目录自动发现（仙侠词看仙侠书、历史词看历史书，各自解读）
BOOKS = sorted(d for d in os.listdir(BASE) if os.path.isdir(os.path.join(BASE, d)))

WORDS = [
    # 种子条目（主名 + 主要别名）
    "筑基丹", "洗髓丹", "疗伤丹", "解毒丹", "聚气丹", "大还丹", "破障丹",
    "灵石", "储物袋", "乾坤袋", "须弥戒", "飞剑", "飞舟", "鼎炉", "丹炉", "阵盘", "传送阵",
    "护身符", "灵草", "妖丹", "内丹", "符箓", "玉简", "身份令牌", "精铁", "秘铁",
    "炼气", "筑基", "金丹", "元婴", "化神",
    "御剑术", "火球术", "大火球术", "土遁术", "轻身术", "幻术", "禁制", "聚灵阵", "护山大阵",
    "炼丹术", "炼器术",
    # 历史池（应在历史书出现，仙侠书可为 0）
    "银两", "铜钱", "佩剑", "腰牌", "圣旨", "海捕文书", "金疮药", "官印",
]


def main() -> int:
    hits = {w: set() for w in WORDS}
    pats = {w: w.encode("utf-8") for w in WORDS}
    for book in BOOKS:
        d = os.path.join(BASE, book)
        if not os.path.isdir(d):
            print(f"⚠️ 找不到书目录: {book}")
            continue
        files = sorted(f for f in os.listdir(d) if f.lower().endswith(".txt"))[:SAMPLE]
        for f in files:
            try:
                data = open(os.path.join(d, f), "rb").read()
            except OSError:
                continue
            for w, p in pats.items():
                if p in data:
                    hits[w].add(book)

    ok, low = [], []
    for w in WORDS:
        (ok if len(hits[w]) >= 2 else low).append((w, len(hits[w])))
    print(f"== 达标（≥2 本，共 {len(ok)} 词）==")
    for w, n in sorted(ok, key=lambda x: -x[1]):
        src = sorted(hits[w])[:6]
        print(f"  {w}: {n}/21  例: {src}")
    print(f"== 不达标（<2 本，共 {len(low)} 词）==")
    for w, n in low:
        print(f"  {w}: {n}/21  例: {sorted(hits[w])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
