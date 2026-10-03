# -*- coding: utf-8 -*-
"""F4 组装演示单测：两段式去重（归一化全等 ＋ sim≥0.85 语义合并）、专名脱敏、beat 解析。

sim 一律注入假函数，不依赖网关/网络（真实 bge-m3 口径的标定证据见
outputs/f4_poc/inventory_embed.txt：同一事件不同写法 0.872、不同事件 0.628）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from f4_assemble import (Audit, Route, Source, Variant, cluster_by_similarity, cosine,
                         find_proper_nouns, merge_identical, norm_text, parse_beat,
                         render_route, scrub, sort_routes)


def V(text, book="书A", ref="弧#1", channel="表侧"):
    return Variant(text=text, sources=[Source(book=book, ref=ref, channel=channel)])


def table_sim(pairs):
    """假 sim：只查显式给出的成对分值，其余按 0。sim 的注入契约即如此。"""
    def sim(a, b):
        if a == b:
            return 1.0
        for (x, y), s in pairs.items():
            if {a, b} == {x, y}:
                return s
        return 0.0
    return sim


# ---- 第一段：归一化全等合并（防两料源双计）----
def test_identical_text_with_different_whitespace_merges_and_keeps_both_sources():
    routes = merge_identical([
        V("主角佯退诱敌，  伏兵自芦苇荡涌出截断退路", book="绍宋", ref="绍宋#6 章66"),
        V("主角佯退诱敌，伏兵自芦苇荡涌出截断退路", book="遮天", ref="遮天#3（c20~33）",
          channel="内嵌·跨书骨架"),
    ])
    assert len(routes) == 1
    assert {s.book for s in routes[0].sources} == {"绍宋", "遮天"}


def test_different_texts_are_not_merged_by_stage_one():
    routes = merge_identical([V("甲方案：抬价后收手令对手高价接盘"), V("乙方案：借第三方势力互相牵制")])
    assert len(routes) == 2


def test_empty_text_dropped():
    assert len(merge_identical([V("   "), V("有效走法正文")])) == 1


# ---- 第二段：sim≥0.85 合并 / <0.85 保留（验收 2 的两类用例）----
LONG = "主角在诗会上公开批评重臣家族作风，借此试探各方反应并压制南迁舆论。"
SHORT = "主角借诗会批评重臣家族，压制南迁舆论。"
OTHER = "主角率水军佯装后撤，引敌方进入芦苇荡后伏兵四起截断退路。"


def test_similar_pair_at_or_above_threshold_merges_into_one_route():
    routes = [Route(text=LONG, members=[V(LONG, book="绍宋")], seq=0),
              Route(text=SHORT, members=[V(SHORT, book="九星霸体诀")], seq=1)]
    merged = cluster_by_similarity(routes, table_sim({(LONG, SHORT): 0.86}), 0.85)
    assert len(merged) == 1
    assert merged[0].text == LONG                      # 代表取最长（信息最全）
    assert len(merged[0].members) == 2
    assert merged[0].merged_sim == 0.86
    assert merged[0].books == ["绍宋", "九星霸体诀"]    # 合并后出处全保留


def test_pair_just_below_threshold_stays_two_routes():
    routes = [Route(text=LONG, members=[V(LONG)], seq=0),
              Route(text=SHORT, members=[V(SHORT, book="遮天")], seq=1)]
    merged = cluster_by_similarity(routes, table_sim({(LONG, SHORT): 0.849}), 0.85)
    assert len(merged) == 2
    assert all(r.merged_sim is None for r in merged)


def test_dissimilar_pair_kept_as_two_routes():
    routes = [Route(text=LONG, members=[V(LONG)], seq=0),
              Route(text=OTHER, members=[V(OTHER, book="遮天")], seq=1)]
    merged = cluster_by_similarity(routes, table_sim({(LONG, OTHER): 0.31}), 0.85)
    assert len(merged) == 2


def test_single_linkage_merges_transitively_and_keeps_seq_order():
    a, b, c = "甲甲甲甲甲甲", "甲甲甲甲乙乙", "丙丙丙丙丙丙"
    routes = [Route(text=a, members=[V(a)], seq=0), Route(text=b, members=[V(b)], seq=1),
              Route(text=c, members=[V(c)], seq=2)]
    sim = table_sim({(a, b): 0.9, (b, c): 0.88})
    merged = cluster_by_similarity(routes, sim, 0.85)
    assert len(merged) == 1          # a~b、b~c → 单链接成一束
    assert merged[0].seq == 0


def test_missing_vector_never_merges():
    """embedding 缺失（网关不可达/未缓存）时 sim 返回 0 → 宁多勿并，不会误合。"""
    routes = [Route(text=LONG, members=[V(LONG)], seq=0),
              Route(text=SHORT, members=[V(SHORT)], seq=1)]
    assert len(cluster_by_similarity(routes, lambda a, b: 0.0, 0.85)) == 2


def test_cosine_is_dot_product_on_normalized_vectors():
    assert cosine([0.6, 0.8], [0.6, 0.8]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0
    assert cosine([1.0, 2.0], [1.0]) == 0.0


# ---- 跨书优选排序（非硬条件）----
def test_cross_book_route_sorts_first():
    r1 = Route(text="单书走法正文" * 3, members=[V("单书走法正文" * 3, book="绍宋")], seq=0)
    r2 = Route(text="跨书走法正文", members=[V("跨书走法正文", book="绍宋"),
                                             V("跨书走法正文", book="遮天")], seq=1)
    assert sort_routes([r1, r2])[0] is r2


def test_tie_breaker_keeps_first_seen_order():
    r1 = Route(text="同为单书且等长的正文", members=[V("同为单书且等长的正文", book="绍宋")], seq=0)
    r2 = Route(text="同为单书且等长的正文", members=[V("同为单书且等长的正文", book="遮天")], seq=1)
    assert [r.seq for r in sort_routes([r2, r1])] == [0, 1]


# ---- beat 标签三形态解析（绍宋单弧侧是裸号，占 324/666）----
def test_parse_beat_three_shapes():
    assert parse_beat("A03 群殴混战×2") == ("A03", 2)
    assert parse_beat("G05") == ("G05", 1)
    assert parse_beat(" ") == (None, 1)
    assert parse_beat("") == (None, 1)


# ---- 专名脱敏 ----
BLACKLIST = {"青铜殿": "某势力殿宇", "鬼水河": "某条河", "修士": "某处"}


def test_scrub_replaces_proper_noun_with_placeholder():
    out, hits = scrub("主角潜入青铜殿外的秘境", BLACKLIST)
    assert hits == ["青铜殿"]
    assert "青铜殿" not in out and "某势力殿宇" in out
    assert find_proper_nouns(out, BLACKLIST) == []          # 脱敏后终核 0 命中


def test_scrub_handles_longest_word_first():
    bl = {"北玄域": "某域", "北玄": "某人"}
    out, hits = scrub("主角自北玄域南下", bl)
    assert hits == ["北玄域"] and "北玄" not in out


def test_route_block_keeps_normal_route_and_counts_scrub():
    audit = Audit()
    text = ("主角潜入青铜殿外反复权衡敌我兵力，先以言语试探守将态度，"
            "确认对方有降意后才放出信物，最终约定次日凌晨交割城防。")
    lines = render_route(1, Route(text=text, members=[V(text)], seq=0), BLACKLIST, audit)
    joined = "\n".join(lines)
    assert lines and audit.scrubbed == 1 and audit.dropped == 0
    assert "某势力殿宇" in joined and "青铜殿" not in joined


def test_route_block_drops_route_below_min_length():
    audit = Audit()
    short = Route(text="主角渡河。", members=[V("主角渡河。")], seq=1)
    assert render_route(1, short, BLACKLIST, audit) == []
    assert audit.dropped == 1


def test_scrub_cascades_until_clean():
    """占位词里又含黑名单词时，按长词优先的顺序能继续消掉（正常路径）。"""
    bl = {"鬼水河": "黑水河", "黑水": "某水"}
    out, hits = scrub("主角在鬼水河下游设伏，断其退路并逼其北移。", bl)
    assert hits == ["鬼水河", "黑水"]
    assert find_proper_nouns(out, bl) == []


def test_route_block_drops_route_with_residual_noun():
    """替换顺序导致残留（短词先替换、生成更长的黑名单词）→ 该路整条剔除，不带病注入。"""
    bl = {"青铜殿": "某处", "水": "青铜殿"}
    text = "主角沿水而下绕到敌后，烧其粮道并伏击运队。"
    out, hits = scrub(text, bl)
    assert "青铜殿" in out and find_proper_nouns(out, bl) == ["青铜殿"]
    audit = Audit()
    assert render_route(1, Route(text=text, members=[V(text)], seq=0), bl, audit) == []
    assert audit.dropped == 1


def test_norm_text_strips_all_whitespace():
    assert norm_text(" 主角 \t佯退\n 诱敌 ") == "主角佯退诱敌"
