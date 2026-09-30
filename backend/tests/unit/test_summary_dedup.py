# -*- coding: utf-8 -*-
"""批量概括「复制退化」检测回归（docs/08 B20）。

背景（2026-09-16 试验实测）：Qwen3-8B 在 10 章批量下，**第 17~20 章摘要逐字节相同**
（difflib 1.000），而这四章**正文完全不同**（相邻正文相似度仅 0.052~0.071）——
模型复制了上一章的概括。重复摘要是"合法文本"，能过 `_valid_summary` 并被原样入库，
**下游没有任何告警**；实测后果是把 16~34 章归成了一条 **19 章巨弧**。
"""

from app.services.plot_import import _dup_summary_chapters


def _batch(nos):
    return [{"no": n} for n in nos]


def test_identical_run_is_flagged():
    """真实形态：一条原稿 + 连续 4 个逐字复制 → 被复制的 4 章都要重跑（原稿不重跑）。"""
    base = "龙尘在炼药师公会中，被一名女子拦住，女子使用战技攻击，被龙尘轻松击退。云奇大师出现，指出女子是其徒弟。"
    got = {
        16: "龙尘前往炼药师公会，被药童拦住，亮出铭牌进入内院。在内院中遇到一名女子向他发难，使用战技攻击，被龙尘一脚踹飞。云奇大师出现。",
        17: base,
        18: base,
        19: base,
        20: base,
        21: "在炼丹室内，十几个炼丹师围坐观看龙尘炼丹。云奇大师说明预支药材需炼制丹药，龙尘选择暴气丹。",
    }
    assert _dup_summary_chapters(got, _batch([16, 17, 18, 19, 20, 21])) == {18, 19, 20}


def test_near_duplicate_of_previous_is_flagged():
    """半复制（≈0.9：只改了几个词）同样要抓 —— 实测 16→17（0.853）就是这种。"""
    a = ("龙尘前往炼药师公会，被药童拦住，亮出铭牌进入内院。在内院中遇到一名女子向他发难，"
         "使用战技攻击，被龙尘一脚踹飞，云奇大师出现并制止了冲突。")
    b = a.replace("被药童拦住", "被一名女子拦住").replace("制止了冲突", "制止了这场冲突")
    assert _dup_summary_chapters({1: a, 2: b}, _batch([1, 2])) == {2}


def test_distinct_summaries_not_flagged():
    """正常情况（相邻章各写各的）不许误报。"""
    got = {
        1: "龙尘在昏迷中醒来，发现灵根被夺，无法修行。母亲龙夫人请来老者诊治，他决定隐藏实力。",
        2: "龙尘在太学宫与李浩发生争执，反将一军拧断对方手指，逼出其生死决战之约，并借机索要金币。",
        3: "龙尘在战技阁习得牤牛劲，随后在生死台击败李浩，震动全场，并与于胖子等人结盟。",
    }
    assert _dup_summary_chapters(got, _batch([1, 2, 3])) == set()


def test_single_chapter_batch_never_flags():
    got = {7: "独一无二的一条概括，长度足够通过有效性校验，内容与其他章无关。"}
    assert _dup_summary_chapters(got, _batch([7])) == set()


def test_missing_summaries_are_skipped():
    """批内漏章（got 里没有）不参与比较，也不该被标成复制。"""
    a = "第一章的概括内容，讲清楚谁在哪做了什么，结果如何，长度足够。"
    got = {1: a, 3: a}          # 第 2 章缺失
    assert _dup_summary_chapters(got, _batch([1, 2, 3])) == {3}
