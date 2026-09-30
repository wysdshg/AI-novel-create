# -*- coding: utf-8 -*-
"""v3 弧归并的纯逻辑回归（docs/08-C8，2026-09-16）。

v3 = 切点优先 + 确定性缝合。两轮 200 章实测证明：
  · 「让模型在窗口里直接划弧」必然出伪影（v1 网格伪影 9/14、v2 覆盖缺口 ~10%）；
  · 只有"定边界（Pass1）→ 缝合 → 写内容（Pass2）"分离才同时拿到 **100% 覆盖 + 无网格绑架**。
这里覆盖的是**纯函数**（不调模型、不写库）—— 它们是"覆盖由构造保证"这一承诺的实现。
"""

from app.services import plot_import as pi


class TestStitchCuts:
    def test_empty(self):
        assert pi.stitch_cuts([], 200) == []

    def test_drops_cut_at_chapter_one(self):
        # 第 1 章永远是弧起点，不该被当成切点
        assert pi.stitch_cuts([1], 100) == []

    def test_same_boundary_proposed_by_two_windows_collapses(self):
        """两个窗口对同一边界的提议会有抖动（±4 章）→ 必须收敛成**一刀**。

        取**上中位**（偶数个时偏后）：边界偏后 → 前一条弧偏长，更不容易触发最短弧长约束。
        """
        assert pi.stitch_cuts([100, 102], 200) == [102]
        assert pi.stitch_cuts([98, 99, 101, 102], 200) == [101]
        assert pi.stitch_cuts([99], 200) == [99]

    def test_distinct_boundaries_kept(self):
        got = pi.stitch_cuts([22, 34, 50, 62, 71, 83, 92, 101], 200)
        assert got == [22, 34, 50, 62, 71, 83, 92, 101]

    def test_enforces_min_arc_len_by_dropping_the_offending_cut(self):
        # 100 与 105 之间只有 5 章 < ARC_MIN_LEN(8) → 去掉造成短弧的那一刀
        got = pi.stitch_cuts([100, 105], 200)
        assert got == [100]
        assert pi.stitch_cuts([4], 200) == []          # 开头 3 章的碎弧 → 去掉

    def test_keeps_spacing_when_all_arcs_long_enough(self):
        cuts = [20, 40, 60, 80, 100]
        assert pi.stitch_cuts(cuts, 120) == cuts

    def test_real_run_shape_dense_cuts_get_consolidated(self):
        """真实一轮（core40 五窗）提到 16 刀，其中 112/118/123 挨得太近（相隔 5~6 章）
        → 会被"最短弧长"约束合并 —— 这正是 v1 那版产生 3 条碎弧的地方，就是要修的病。"""
        raw = [22, 34, 50, 62, 71, 82, 92, 112, 118, 123, 132, 148, 161, 173, 186, 193]
        got = pi.stitch_cuts(raw, 200)
        assert len(got) < len(raw)                       # 密集处被合并
        arcs = pi.arcs_from_cuts(got, 200)
        assert all(e - s + 1 >= pi.ARC_MIN_LEN for s, e in arcs)
        assert arcs[0][0] == 1 and arcs[-1][1] == 200
        assert sum(e - s + 1 for s, e in arcs) == 200    # 覆盖不丢章


class TestArcsFromCuts:
    def test_no_cuts_means_one_arc(self):
        assert pi.arcs_from_cuts([], 200) == [(1, 200)]

    def test_contiguous_and_complete(self):
        arcs = pi.arcs_from_cuts([14, 29, 46], 60)
        assert arcs == [(1, 13), (14, 28), (29, 45), (46, 60)]
        assert arcs[0][0] == 1 and arcs[-1][1] == 60
        for (a0, a1), (b0, _) in zip(arcs, arcs[1:]):
            assert b0 == a1 + 1            # 无缺口、无重叠（覆盖由构造保证）

    def test_cut_beyond_total_ignored(self):
        assert pi.arcs_from_cuts([50, 999], 60) == [(1, 49), (50, 60)]


class TestPostcheckArcs:
    def test_short_arc_merges_into_previous(self):
        arcs, rep = pi.postcheck_arcs([(1, 5), (6, 30), (31, 200)], 200)
        assert arcs == [(1, 30), (31, 200)]
        assert rep["merged"] == 1

    def test_leading_short_arc_merges_into_next(self):
        arcs, rep = pi.postcheck_arcs([(1, 4), (5, 40), (41, 200)], 200)
        assert arcs == [(1, 40), (41, 200)]
        assert rep["merged"] == 1

    def test_iterates_until_stable(self):
        arcs, rep = pi.postcheck_arcs([(1, 3), (4, 8), (9, 60), (61, 200)], 200)
        assert all(e - s + 1 >= pi.ARC_MIN_LEN for s, e in arcs)
        assert rep["merged"] >= 1

    def test_flags_over_max(self):
        arcs, rep = pi.postcheck_arcs([(1, 100), (101, 200)], 200)
        assert rep["over_max"] == ["1~100", "101~200"]
        assert rep["under_min"] == []

    def test_coverage_flag(self):
        _, ok = pi.postcheck_arcs([(1, 50), (51, 100)], 100)
        assert ok["coverage_ok"] is True
        _, bad = pi.postcheck_arcs([(1, 40)], 100)
        assert bad["coverage_ok"] is False

    def test_single_arc_is_never_merged_away(self):
        arcs, rep = pi.postcheck_arcs([(1, 200)], 200)
        assert arcs == [(1, 200)] and rep["merged"] == 0

    def test_real_run_lens_are_reasonable(self):
        """真实一轮（core100）的弧长分布：均值落在目标区间、无 <8 章碎弧。"""
        cuts = [22, 34, 51, 62, 71, 83, 92, 101, 112, 132, 148, 162, 173, 191]
        arcs = pi.arcs_from_cuts(cuts, 200)
        arcs, rep = pi.postcheck_arcs(arcs, 200)
        assert rep["coverage_ok"]
        assert all(e - s + 1 >= pi.ARC_MIN_LEN for s, e in arcs)
        assert 8 <= rep["mean"] <= 25


class TestPrompts:
    def test_cuts_prompt_asks_json_only_and_forbids_grid(self):
        p = pi._cuts_prompt(1, 120, "第1章：甲")
        assert '"cuts"' in p
        assert "不要迁就任何固定的章号间隔" in p
        assert "不要包含第 1 章" in p

    def test_arc_content_prompt_carries_range_and_context(self):
        p = pi._arc_content_prompt([22, 23, 24, 25], "第22章：甲", "第19章：乙", "第28章：丙")
        assert "第 22~25 章" in p
        assert "节拍" in p and '"beats"' in p
        assert "第19章：乙" in p and "第28章：丙" in p

    def test_arc_content_prompt_omits_empty_context(self):
        p = pi._arc_content_prompt([1, 2, 3], "第1章：甲", "", "")
        assert "仅供理解背景" not in p and "仅供理解走向" not in p


class TestStreaming:
    """流式编排（与章摘要并行）的纯逻辑 —— 正确性全靠窗口定型判定。"""

    def test_window_specs_tile_the_book(self):
        specs = pi.window_specs(1, 200, 100, 20)
        assert specs == [{"lo": 1, "core_end": 100, "need": 120},
                         {"lo": 101, "core_end": 200, "need": 200}]
        # 相邻窗口首尾相接，不留缝
        for a, b in zip(specs, specs[1:]):
            assert b["lo"] == a["core_end"] + 1

    def test_window_need_includes_tail_but_clamps_to_book(self):
        assert pi.window_specs(1, 60, 100, 20)[0]["need"] == 60          # 全书不足一窗
        assert pi.window_specs(1, 300, 100, 20)[1]["need"] == 220        # core_end 200 + tail 20

    def test_settled_arcs_excludes_the_horizon_arc(self):
        cuts = [22, 34, 51, 62, 71, 83, 92, 101]
        settled = pi.settled_arcs(cuts, 100)          # horizon = 108
        assert all(e < 108 for _, e in settled)
        assert (92, 100) in settled                   # 结束于 100 < 108 ✓
        assert (101, 108) not in settled              # 恰好顶到 horizon → 右边界还要看后续切点

    def test_settled_arcs_ignores_cuts_beyond_horizon(self):
        """超过 horizon 的切点（属于后续窗口）不得参与本窗的定型判定。"""
        assert pi.settled_arcs([22, 34, 51, 200], 60) == [(1, 21), (22, 33), (34, 50)]

    def test_settled_arc_is_stable_when_later_cuts_arrive(self):
        """🔴 本设计的正确性核心：**已定型弧必须仍出现在最终弧表里**。

        依据：窗口 w 的可信切点上限是 core_end+8，而 w+1 的最早可信切点是 core_end+9
        ⇒ 落在 [.., core_end+8] 内的切点再也不会被后续窗口改动。
        """
        early = [22, 34, 51]                  # 窗口 core_end=60 时已知
        later = [62, 71, 83, 92]              # 后续窗口才知道（都 ≥ 69）
        settled = pi.settled_arcs(early, 60)
        assert settled == [(1, 21), (22, 33), (34, 50)]
        final = pi.arcs_from_cuts(pi.stitch_cuts(early + later, 200), 200)
        for a in settled:
            assert a in final

    def test_settled_arcs_empty_without_cuts(self):
        assert pi.settled_arcs([], 100) == []


class TestDefaults:
    def test_window_defaults_are_the_validated_values(self):
        # 200 章实测：core 100 + 后视 20 优于 core 40（碎弧 3 vs 6）
        assert pi.ARC_CORE_DEFAULT == 100
        assert pi.ARC_TAIL_DEFAULT == 20
        assert pi.CUT_TRUST_MARGIN == 8
        assert (pi.ARC_AMIN, pi.ARC_AMAX) == (12, 18)
