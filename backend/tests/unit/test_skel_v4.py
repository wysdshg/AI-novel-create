# -*- coding: utf-8 -*-
"""[DEV-SK03] 骨架库重组 v4：A17 解析器 + (大类,子事件) 精确同键重组 的零 LLM 单测。

三条红线各有专属回归用例，全部锚在**真实数据形态**上：
  1. A17：win_<书>.json 的 seq 是**窗口内重编号**（九星 484 原子只 23 个 seq），
     禁止全局字典解析；主判据 = 「seq 相等 ∧ 原子区间与弧区间**真重叠**」。
     锚点 = 九星 #234 拍5：旧口径 ⊆±2 会把上一弧尾巴 A04 c762 收进来，
     真重叠口径只认 G02 c774~775（PM 已据此出弧速览-v6）。
  2. 太荒回退台账：全库 2063 拍里**唯一**一拍无真重叠候选 = #111 拍4，
     其同 seq 候选是坏原子 F01 c346~345（chapter_start > chapter_end）→
     该拍**缺失并记台账**，这正是 v6 把 #111 从 4 拍收成 3 拍的机制。
  3. 组装不借邻：弧只落进**自己判类的那一个** (大类,子事件) 键，绝不借邻类弧凑数。

用例不碰生产库：纯函数 + tmp_path 造的 mini win/shaosong 文件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]      # backend/
REPO = Path(__file__).resolve().parents[3]          # 项目根
sys.path.insert(0, str(BACKEND / "scripts"))

import skel_v4_rebuild as skel  # noqa: E402

# 单测**不碰生产库**（conftest 铁律）：原子词表用这份真实 id→名子集。
VOCAB = {
    "A01": "单挑决斗", "A02": "擂台比试", "A03": "群殴混战", "A04": "设伏偷袭",
    "A05": "越阶硬撼", "A08": "反杀复仇", "B01": "拍卖竞价", "B03": "谈判交涉",
    "C05": "立威震慑", "D03": "围困被困", "E02": "赶路迁徙", "F01": "闭关突破",
    "G02": "秘境夺宝",
}


# ────────────────────────────── A17 解析器 ──────────────────────────────
class TestInterval:
    def test_overlap_is_symmetric_and_inclusive(self):
        # 相交 1 章即算真重叠（PM 原话「区间相交≥1 章」）
        assert skel.overlaps(1, 5, 5, 9)
        assert skel.overlaps(5, 9, 1, 5)
        assert skel.overlaps(1, 9, 5, 6)

    def test_adjacent_not_overlapping(self):
        # 762 与 [763,775] 相邻但不相交 → A17 的病根就在这里
        assert not skel.overlaps(762, 762, 763, 775)
        assert not skel.overlaps(763, 775, 762, 762)

    def test_inverted_range_never_overlaps(self):
        # 太荒坏原子 c346~345（start>end）：任何口径都取不到
        assert not skel.overlaps(346, 345, 332, 345)


class TestResolveAtom:
    def _cands(self):
        # 九星 seq=5 的真实三个候选（截取）
        return [
            {"seq": 5, "atomic_id": "D05", "chapter_start": 721, "chapter_end": 727},
            {"seq": 5, "atomic_id": "A04", "chapter_start": 762, "chapter_end": 762},
            {"seq": 5, "atomic_id": "G02", "chapter_start": 774, "chapter_end": 775},
        ]

    def test_picks_the_true_overlap_not_the_plusminus2_neighbour(self):
        """A17 回归锚点：#234 拍5 弧 c763~775。
        旧口径 ⊆±2（视作 [761,777]）会把上一弧尾巴 A04 c762 一起收进来；
        真重叠口径只认 G02。"""
        got = skel.resolve_atom(5, 763, 775, self._cands())
        assert got is not None
        assert got["atomic_id"] == "G02"

    def test_old_tolerance_would_have_been_wrong(self):
        """把旧口径显式写出来当反证：它确实多收 A04 —— 证明新口径有实际差异。"""
        cands = self._cands()
        old = [c for c in cands
               if c["chapter_start"] >= 763 - 2 and c["chapter_end"] <= 775 + 2]
        assert {c["atomic_id"] for c in old} == {"A04", "G02"}
        assert [c["atomic_id"] for c in old if c["atomic_id"] == "A04"]

    def test_multiple_overlaps_takes_the_longest(self):
        cands = [
            {"seq": 2, "atomic_id": "X1", "chapter_start": 10, "chapter_end": 12},
            {"seq": 2, "atomic_id": "X2", "chapter_start": 11, "chapter_end": 40},
        ]
        assert skel.resolve_atom(2, 10, 20, cands)["atomic_id"] == "X2"

    def test_no_overlap_returns_none(self):
        assert skel.resolve_atom(4, 332, 345, [
            {"seq": 4, "atomic_id": "F01", "chapter_start": 346, "chapter_end": 345},
        ]) is None

    def test_seq_must_match_even_when_chapters_overlap(self):
        cands = [{"seq": 9, "atomic_id": "Z", "chapter_start": 10, "chapter_end": 12}]
        assert skel.resolve_atom(4, 10, 20, cands) is None


def _mini_win(atoms, arcs, book="测试书"):
    return {"book": book, "atoms": atoms, "arcs": arcs, "windows": []}


def _atom(seq, aid, lo, hi, tags=None, summary="起：a｜承：b｜转：c｜合：d"):
    return {"seq": seq, "atomic_id": aid, "chapter_start": lo, "chapter_end": hi,
            "summary": summary, "tags": tags or ["标签一", "标签二"]}


class TestParseWinBook:
    def test_seq_is_window_local_so_global_dict_would_be_wrong(self):
        """同一 seq 在文件里出现多次 —— 全局字典解析必错，这就是 A17。"""
        atoms = [_atom(1, "A01", 1, 5), _atom(1, "B01", 101, 105), _atom(2, "C01", 6, 9)]
        arcs = [{"arc_name": "弧一", "atoms": [1, 2], "chapter_start": 1,
                 "chapter_end": 10, "core_atomics": ["A01"]}]
        arcs2 = skel.parse_win_data(_mini_win(atoms, arcs))
        assert len(arcs2) == 1
        assert [b.atomic_id for b in arcs2[0].beats] == ["A01", "C01"]

    def test_beat_carries_real_atom_desc_and_tags(self):
        """desc/tags 必须来自**解析后的真原子**（A17 污染下的乱码 tags 随之消失）。"""
        atoms = [_atom(1, "A01", 1, 5, tags=["扮猪吃虎"], summary="真概括")]
        arcs = [{"arc_name": "弧一", "atoms": [1], "chapter_start": 1,
                 "chapter_end": 5, "core_atomics": ["A01"]}]
        a = skel.parse_win_data(_mini_win(atoms, arcs))[0]
        assert a.beats[0].summary == "真概括"
        assert a.beats[0].tags == ["扮猪吃虎"]

    def test_unresolvable_beat_is_dropped_and_ledgered(self):
        """无真重叠候选 → 该拍缺失 + 记台账（不拿邻弧尾巴凑数）。"""
        atoms = [_atom(1, "A01", 1, 5), _atom(2, "F01", 346, 345)]
        arcs = [{"arc_name": "弧一", "atoms": [1, 2], "chapter_start": 1,
                 "chapter_end": 345, "core_atomics": ["A01"]}]
        a = skel.parse_win_data(_mini_win(atoms, arcs))[0]
        assert [b.atomic_id for b in a.beats] == ["A01"]
        assert a.dropped_seqs == [2]
        assert a.ledger and a.ledger[0]["seq"] == 2
        assert "无真重叠候选" in a.ledger[0]["reason"]

    def test_arc_with_no_resolvable_beat_is_kept_flagged(self):
        """整弧拍全丢也要留痕（供报告台账），不能静默消失。"""
        atoms = [_atom(1, "F01", 900, 899)]
        arcs = [{"arc_name": "空弧", "atoms": [1], "chapter_start": 1,
                 "chapter_end": 5, "core_atomics": []}]
        a = skel.parse_win_data(_mini_win(atoms, arcs))[0]
        assert a.beats == [] and a.dropped_seqs == [1]

    def test_consecutive_same_atom_is_compressed_with_repeats(self):
        """v3 契约（拍板⑥）：连续同原子压成一步，repeats 记次数。"""
        atoms = [_atom(1, "A01", 1, 5), _atom(2, "A01", 6, 9), _atom(3, "B01", 10, 12)]
        arcs = [{"arc_name": "弧一", "atoms": [1, 2, 3], "chapter_start": 1,
                 "chapter_end": 12, "core_atomics": []}]
        a = skel.parse_win_data(_mini_win(atoms, arcs))[0]
        assert a.seq == ["A01", "B01"]
        assert a.repeats == [2, 1]

    def test_atomic_id_may_be_empty_for_no_suitable_atom(self):
        """原始数据里 atomic_id 可以是空串（tags 首项「无合适原子」），不能当缺失。"""
        atoms = [_atom(1, "", 1, 5, tags=["无合适原子", "重生布局"])]
        arcs = [{"arc_name": "弧一", "atoms": [1], "chapter_start": 1,
                 "chapter_end": 5, "core_atomics": []}]
        a = skel.parse_win_data(_mini_win(atoms, arcs))[0]
        assert a.seq == [""]
        assert a.beats[0].tags == ["无合适原子", "重生布局"]


class TestParseShaosong:
    def test_segment_no_becomes_beat_in_order(self):
        arr = [
            {"segment_no": 1, "atomic_id": "", "text": "起因…", "tags": ["身份转换"]},
            {"segment_no": 2, "atomic_id": "G05", "text": "经过…", "tags": ["打听消息"]},
            {"segment_no": 3, "atomic_id": "B03", "text": "结果是…", "tags": ["谈判交涉"]},
        ]
        a = skel.parse_shaosong_data(arr, "绍宋", arc_no=2)
        # 弧号取自**文件名**（绍宋#2.txt），不是首个 segment_no
        assert a.arc_name == "绍宋#2"
        assert a.seq == ["", "G05", "B03"]
        assert a.beats[0].summary == "起因…"
        assert a.dropped_seqs == [] and a.ledger == []

    def test_arc_no_defaults_to_first_segment(self):
        arr = [{"segment_no": 7, "atomic_id": "A01", "text": "t", "tags": []}]
        assert skel.parse_shaosong_data(arr, "绍宋").arc_name == "绍宋#7"

    def test_no_seq_resolution_needed_so_no_ledger(self):
        """绍宋 singles 是独立 JSON 数组，无 seq 问题 → 台账必空。"""
        arr = [{"segment_no": i, "atomic_id": "A01", "text": "t", "tags": []} for i in range(1, 4)]
        assert skel.parse_shaosong_data(arr, "绍宋").ledger == []


class TestRealDataA17Regression:
    """跑真实素材验 A17 修复（只读文件，不写库）。"""

    WIN = REPO / "outputs" / "_atomic_raw"

    def test_234_beat5_resolves_to_real_content_not_previous_arc_tail(self):
        p = self.WIN / "win_九星霸体诀.json"
        if not p.exists():
            pytest.skip("素材缺失")
        arcs = skel.parse_win_book(p)
        a = next(x for x in arcs if x.arc_name == "宗门启程与立威")
        assert a.seq == ["F01", "C05", "E02", "A03", "G02"]
        assert a.seq[-1] != "A04", "旧口径会把上一弧尾巴 A04 c762 收进来"

    def test_111_ghost_beat_dropped_to_three_beats(self):
        """PM 在 v6 把 #111 从 4 拍收成 3 拍；机制 = 拍4 的同 seq 候选是坏原子。"""
        p = self.WIN / "win_太荒吞天诀.json"
        if not p.exists():
            pytest.skip("素材缺失")
        arcs = skel.parse_win_book(p)
        a = next(x for x in arcs if x.arc_name == "初涉险地扬名")
        assert a.seq == ["A01", "B01", "A08"]
        assert a.dropped_seqs == [4] and len(a.ledger) == 1
        assert a.ledger[0]["book"] == "太荒吞天诀"

    def test_only_one_beat_in_the_whole_corpus_lacks_a_true_overlap(self):
        """全库兜底台账口径：2063 拍里恰好 1 拍缺失（= #111 拍4）。"""
        total = dropped = 0
        for name in ("凡人修仙传", "斗破苍穹", "太荒吞天诀",
                     "九星霸体诀", "圣墟", "蛊真人", "遮天"):
            p = self.WIN / f"win_{name}.json"
            if not p.exists():
                continue
            for a in skel.parse_win_book(p):
                total += len(a.declared_seqs)
                dropped += len(a.dropped_seqs)
        assert total == 2063
        assert dropped == 1

    def test_corpus_is_591_win_arcs_plus_25_shaosong_equals_616(self):
        n = 0
        for name in ("凡人修仙传", "斗破苍穹", "太荒吞天诀",
                     "九星霸体诀", "圣墟", "蛊真人", "遮天"):
            p = self.WIN / f"win_{name}.json"
            if p.exists():
                n += len(skel.parse_win_book(p))
        ss = self.WIN / "shaosong"
        if ss.is_dir():
            n += len(skel.parse_shaosong_dir(ss))
        assert n == 616


# ────────────────────────────── 判类结果解析 ──────────────────────────────
class TestParseJudgements:
    MD = REPO / "outputs" / "skel_v4" / "判类结果-v4.md"

    def test_reads_616_rows_57_classes_49_low_conf(self):
        rows = skel.parse_judgements(self.MD)
        assert len(rows) == 616
        assert [r.no for r in rows] == list(range(1, 617))
        assert len({r.cls for r in rows}) == 57
        assert sum(1 for r in rows if r.confidence == "低") == 49

    def test_dash_means_empty_sub_event(self):
        rows = {r.no: r for r in skel.parse_judgements(self.MD)}
        assert rows[1].sub_event == "宗门招新"
        assert rows[1].sub_tags == []
        assert rows[430].sub_event == "偏峰立足"

    def test_focus_low_conf_three_are_present_and_low(self):
        rows = {r.no: r for r in skel.parse_judgements(self.MD)}
        for no in (111, 234, 413):
            assert rows[no].confidence == "低", f"#{no} 应为低置信"
        assert rows[111].cls == "绝境反杀"
        assert rows[234].cls == "据点立足"
        assert rows[413].cls == "清除内患"

    def test_low_conf_list_section_is_cross_checkable(self):
        """§4 低置信清单 49 行与主表逐条一致（PM 的机检口径）。"""
        assert len(skel.low_conf_numbers(self.MD)) == 49

    def test_input_integrity_reproduces_the_pm_self_check(self):
        """脚本每次跑都重核 PM 亲验过的那几项，输入被动过就拒绝组装。"""
        ig = skel.check_input_integrity(skel.parse_judgements(self.MD))
        assert ig["rows"] == 616
        assert ig["no_gap"] is True
        assert ig["n_classes"] == 57
        assert ig["conf_dist"] == {"高": 101, "中": 466, "低": 49}
        assert ig["low_match"] is True          # 主表低置信 == §4 清单弧号集合
        assert ig["only_in_list"] == [] and ig["only_in_main"] == []
        assert ig["sum_by_class"] == 616
        assert set(ig["orphan_classes"]) == {"格局变动", "庙堂权谋", "跨域远征"}  # 3 孤例类

    def test_integrity_guard_rejects_a_tampered_row(self):
        rows = skel.parse_judgements(self.MD)
        rows[0] = skel.Judgement(no=1, book=rows[0].book, arc_name=rows[0].arc_name,
                                 cls=rows[0].cls, sub_event=rows[0].sub_event,
                                 confidence="低", reason="x", sub_tags=[])   # 多一条低置信
        ig = skel.check_input_integrity(rows)
        assert ig["low_match"] is False and ig["only_in_main"] == [1]


# ────────────────────────────── 键分组（组装不借邻） ──────────────────────────────
class TestKeyGrouping:
    def _arc(self, cls, sub, book="书甲", arc_name="弧"):
        return skel.MemberArc(
            book=book, arc_name=arc_name, ch_lo=1, ch_hi=9,
            seq=["A01", "B03"], repeats=[1, 1], cores=["A01"],
            beats=[skel.Beat(seq=1, atomic_id="A01", ch_lo=1, ch_hi=4,
                             summary="s1", tags=["t1"]),
                   skel.Beat(seq=2, atomic_id="B03", ch_lo=5, ch_hi=9,
                             summary="s2", tags=["t2"])],
            judgement=skel.Judgement(no=1, book=book, arc_name=arc_name, cls=cls,
                                    sub_event=sub, confidence="高", reason="", sub_tags=[]),
        )

    def test_key_is_class_plus_sub_event(self):
        g = skel.group_by_key([self._arc("擂台大比", "生死约战"),
                               self._arc("擂台大比", "擂台应战", arc_name="弧2")])
        assert set(g) == {("擂台大比", "生死约战"), ("擂台大比", "擂台应战")}

    def test_empty_sub_event_falls_into_class_only_key(self):
        g = skel.group_by_key([self._arc("开山建派", "")])
        assert set(g) == {("开山建派", "")}

    def test_same_class_different_sub_event_never_shares_a_template(self):
        """组装不借邻：同大类不同子事件的两弧绝不进同一张模板。"""
        g = skel.group_by_key([self._arc("秘境夺宝", "古殿捡漏"),
                               self._arc("秘境夺宝", "地窖抢宝", arc_name="弧2")])
        tpls = skel.build_templates(g, VOCAB, {})
        assert len(tpls) == 2
        assert {t.name for t in tpls} == {"秘境夺宝--古殿捡漏", "秘境夺宝--地窖抢宝"}

    def test_template_name_uses_double_dash(self):
        g = skel.group_by_key([self._arc("擂台大比", "生死约战")])
        assert skel.build_templates(g, VOCAB, {})[0].name == "擂台大比--生死约战"

    def test_class_only_key_name_has_no_trailing_dash(self):
        g = skel.group_by_key([self._arc("开山建派", "")])
        assert skel.build_templates(g, VOCAB, {})[0].name == "开山建派"


# ────────────────────────────── 合并判据 ──────────────────────────────
class TestMergeCriteria:
    def _m(self, seq, cores=(), book="书甲", arc_name="弧"):
        a = self._arc_of(seq, book, arc_name)
        a.cores = list(cores)
        return a

    def _arc_of(self, seq, book="书甲", arc_name="弧"):
        m = skel.MemberArc(
            book=book, arc_name=arc_name, ch_lo=1, ch_hi=100, seq=list(seq),
            repeats=[1] * len(seq), cores=[],
            beats=[skel.Beat(seq=i + 1, atomic_id=x, ch_lo=0, ch_hi=0,
                             summary=f"{x}的走法", tags=["共同标签", x])
                   for i, x in enumerate(seq)],
            judgement=skel.Judgement(no=1, book=book, arc_name=arc_name, cls="大战征伐",
                                     sub_event="家族战争", confidence="高",
                                     reason="", sub_tags=[]),
        )
        return m

    def test_anchor_example_merges(self):
        """docs/10 §11 提案合并例：6 环 × 4 环（LCS=3）应并，score 0.75。"""
        A = self._m("甲乙丙丁戊己", ["甲"], arc_name="A")
        B = self._m("甲丙戊庚", ["甲"], arc_name="B")
        ok, score, why = skel.can_merge_arcs(A, B)
        assert ok and abs(score - 0.75) < 1e-9

    def test_core_coverage_is_unidirectional(self):
        """v4.2 单向覆盖：A 核心全在 B 里即可并，即便 B 的另一个核心不在 A 里。
        （旧双向判据会把这种「一方是另一方超集/姊妹弧」的正常情况卡死。）"""
        A = self._m("甲乙丙丁", ["甲"], arc_name="A")
        B = self._m("甲乙丙戊", ["甲", "戊"], arc_name="B")
        ok, score, why = skel.can_merge_arcs(A, B)
        assert ok and abs(score - 0.75) < 1e-9 and why == ""

    def test_both_directions_missing_a_core_blocks_merge(self):
        """单向的下限：两边各有核心落在对方序列之外 → 仍不并。"""
        A = self._m("甲乙丙", ["甲"], arc_name="A")
        B = self._m("乙丙丁", ["丁"], arc_name="B")
        ok, _, why = skel.can_merge_arcs(A, B)
        assert ok is False and "单向" in why

    def test_segmented_thresholds(self):
        """分段档：较长方 ≤3 环 0.50，>3 环 0.75（取代 SK03 的 0.65/0.70/0.85 三段）。"""
        assert skel.seg_threshold(2, 2) == 0.50
        assert skel.seg_threshold(2, 3) == 0.50
        assert skel.seg_threshold(3, 3) == 0.50
        assert skel.seg_threshold(3, 4) == 0.75      # 分界看较长方，3×4 落长档
        assert skel.seg_threshold(4, 4) == 0.75
        assert skel.seg_threshold(10, 12) == 0.75

    def test_two_beat_pair_at_050_merges_at_075_would_not(self):
        """2 拍弧格点：LCS/min=1/2=0.50 正好踩分段档短弧门槛（旧 0.65 会拦）。"""
        A = self._m("甲乙", arc_name="A")
        B = self._m("甲丙", arc_name="B")
        ok, score, _ = skel.can_merge_arcs(A, B)
        assert ok is True and abs(score - 0.5) < 1e-9
        assert skel.can_merge_arcs(A, B, thr_min=0.75)[0] is False

    def test_three_beat_at_067_merges_under_short_tier(self):
        """3 拍 × 3 拍，LCS=2 → 0.667 ≥ 0.50 → 并。"""
        A = self._m("甲乙丙", arc_name="A")
        B = self._m("甲乙丁", arc_name="B")
        ok, score, _ = skel.can_merge_arcs(A, B)
        assert ok is True and abs(score - 2 / 3) < 1e-9

    def test_four_beat_at_050_rejected_under_long_tier(self):
        """4 拍 × 4 拍，LCS=2 → 0.50 < 0.75 → 不并（长档不放水）。"""
        A = self._m("甲乙丙丁", arc_name="A")
        B = self._m("甲乙戊己", arc_name="B")
        ok, score, why = skel.can_merge_arcs(A, B)
        assert ok is False and abs(score - 0.5) < 1e-9 and "0.75" in why

    def test_max_ratio_blocks_lopsided_pair(self):
        """4 环 vs 12 环只 3 环相同：r_max=0.25 < 0.5 → 拦。"""
        A = self._m("ABCDEFGHIJKL", arc_name="A")
        B = self._m("ABD", arc_name="B")
        ok, _, why = skel.can_merge_arcs(A, B)
        assert not ok and ("max" in why or "悬殊" in why)

    def test_scs_over_15_is_rejected(self):
        """SCS 合并 > 15 环且非完全包含 → 不并（另起单弧模板）。"""
        A = self._m("".join(chr(65 + i) for i in range(9)), arc_name="A")
        B = self._m("".join(chr(97 + i) for i in range(9)), arc_name="B")
        assert len(skel.scs_merge(A.seq, A.repeats, B.seq, B.repeats)[0]) == 18
        assert skel.can_merge_arcs(A, B)[0] is False


class TestClustering:
    def _m(self, seq, cores=(), arc_name="弧", book="书甲", no=1, conf="高"):
        m = skel.MemberArc(
            book=book, arc_name=arc_name, ch_lo=1, ch_hi=100, seq=list(seq),
            repeats=[1] * len(seq), cores=list(cores),
            beats=[skel.Beat(seq=i + 1, atomic_id=x, ch_lo=0, ch_hi=0,
                             summary=f"{x}的走法", tags=["共同标签", x])
                   for i, x in enumerate(seq)],
            judgement=skel.Judgement(no=no, book=book, arc_name=arc_name, cls="大战征伐",
                                     sub_event="家族战争", confidence=conf,
                                     reason="", sub_tags=[]),
        )
        return m

    def test_compatible_arcs_collapse_into_one_template(self):
        arcs = [self._m("甲乙丙", arc_name="A"), self._m("甲丙", arc_name="B")]
        clusters = skel.cluster_within_key(arcs)
        assert len(clusters) == 1
        assert {m.arc_name for m in clusters[0].members} == {"A", "B"}
        assert clusters[0].n == 2

    def test_incompatible_arcs_each_become_their_own_single_arc_template(self):
        arcs = [self._m("甲乙丙丁戊己己己己乙", arc_name="A"),
                self._m("甲甲甲甲乙乙乙乙丙", arc_name="B")]
        clusters = skel.cluster_within_key(arcs)
        assert len(clusters) == 2
        assert all(c.n == 1 for c in clusters)

    def test_three_arcs_two_merge_one_solo(self):
        arcs = [self._m("甲乙丙", arc_name="A"), self._m("甲丙", arc_name="B"),
                self._m("丁戊己己己己己己己己己己乙", arc_name="C")]
        clusters = skel.cluster_within_key(arcs)
        assert sorted(c.n for c in clusters) == [1, 2]

    def test_singleton_key_is_one_cluster_of_one(self):
        assert len(skel.cluster_within_key([self._m("甲乙丙")])) == 1

    def test_never_merges_across_different_keys(self):
        """借邻防护：即使 seq 完全相同，不同键的弧也不得并到一起。"""
        a = self._m("甲乙丙", arc_name="A", no=1)
        a.judgement.sub_event = "家族战争"
        b = self._m("甲乙丙", arc_name="B", no=2, book="书乙")
        b.judgement.sub_event = "帮派死斗"
        g = skel.group_by_key([a, b])
        tpls = skel.build_templates(g, VOCAB, {})
        assert len(tpls) == 2
        assert all(t.source_stats["n_members"] == 1 for t in tpls)


# ────────────────────────────── 模板装配 ──────────────────────────────
class TestTemplateBuild:
    def _m(self, seq, cores=(), arc_name="弧", book="书甲", no=1, conf="高", tags=None):
        """seq 传字符串按字符展开（"甲乙丙" → 3 环）；传列表原样使用（["A05"] → 单环）。

        desc 里带弧名 —— 这是断言「各弧 desc 互不相同」的前提，否则同 seq 会生成同文案。
        """
        return skel.MemberArc(
            book=book, arc_name=arc_name, ch_lo=10, ch_hi=20, seq=list(seq),
            repeats=[1] * len(seq), cores=list(cores),
            beats=[skel.Beat(seq=i + 1, atomic_id=x, ch_lo=0, ch_hi=0,
                             summary=f"{arc_name}·{x}的走法{i}",
                             tags=list(tags or ["共同标签", x]))
                   for i, x in enumerate(seq)],
            judgement=skel.Judgement(no=no, book=book, arc_name=arc_name, cls="大战征伐",
                                     sub_event="家族战争", confidence=conf,
                                     reason="依据", sub_tags=["守土防御"]),
        )

    def test_structure_follows_v3_contract(self):
        t = skel.build_templates({("大战征伐", "家族战争"): [self._m("甲乙丙", ["甲"])]},
                                 VOCAB, {})[0]
        st = t.structure
        assert "phases" in st and st["display_top"] == 5
        assert st["phases"][0]["phase"] in ("起", "承", "转", "合")
        b = st["phases"][0]["beats"][0]
        assert set(b) == {"beat", "variants"}
        assert set(b["variants"][0]) == {"src", "how", "desc", "tags"}

    def test_beat_label_is_atomic_id_plus_name(self):
        vocab = VOCAB
        t = skel.build_templates({("大战征伐", "家族战争"): [self._m(["A05"], ["A05"])]},
                                 vocab, {})[0]
        beat = t.structure["phases"][0]["beats"][0]["beat"]
        assert beat.startswith("A05 ")
        assert vocab["A05"] in beat

    def test_variant_desc_comes_from_that_arcs_own_beat(self):
        """v3 的病：同一拍下所有 variant 的 desc 一模一样（A17 全局字典解析所致）。
        v4 必须逐弧各不相同。"""
        a = self._m("甲乙", ["甲"], arc_name="弧A", no=1)
        b = self._m("甲乙", ["甲"], arc_name="弧B", no=2, book="书乙")
        t = skel.build_templates({("大战征伐", "家族战争"): [a, b]},
                                 VOCAB, {})[0]
        vs = t.structure["phases"][0]["beats"][0]["variants"]
        assert len({v["desc"] for v in vs}) == 2
        assert {v["src"] for v in vs} == {"书甲", "书乙"}

    def test_variant_how_carries_arc_name_and_chapter_range(self):
        a = self._m("甲", ["甲"], arc_name="弧A")
        t = skel.build_templates({("大战征伐", "家族战争"): [a]}, VOCAB, {})[0]
        assert "弧A" in t.structure["phases"][0]["beats"][0]["variants"][0]["how"]
        assert "c10~20" in t.structure["phases"][0]["beats"][0]["variants"][0]["how"]

    def test_every_beat_has_at_least_one_variant(self):
        """无块模板 = 0：任何 beat 的 variants 都不能为空。"""
        a = self._m("甲乙丙", ["甲"])
        b = self._m("甲丙", ["甲"], arc_name="弧B", no=2, book="书乙")
        t = skel.build_templates({("大战征伐", "家族战争"): [a, b]},
                                 VOCAB, {})[0]
        beats = [b for ph in t.structure["phases"] for b in ph["beats"]]
        assert beats and all(x["variants"] for x in beats)

    def test_no_template_has_zero_beats(self):
        g = skel.group_by_key([self._m("甲乙丙", ["甲"])])
        for t in skel.build_templates(g, VOCAB, {}):
            n = sum(len(ph["beats"]) for ph in t.structure["phases"])
            assert n > 0

    def test_variants_sorted_core_anchored_first(self):
        """拍型相似度排序：核心命中的变体排在前面。"""
        a = self._m("甲乙", cores=["甲"], arc_name="有核", no=1)
        b = self._m("甲乙", cores=["乙"], arc_name="无核", no=2, book="书乙")
        t = skel.build_templates({("大战征伐", "家族战争"): [a, b]},
                                 VOCAB, {})[0]
        beat = t.structure["phases"][0]["beats"][0]
        assert "有核" in beat["variants"][0]["how"]

    def test_variants_sorted_by_tag_similarity_then_stable(self):
        """拍型签名 = 出现在 ≥半数变体里的 tag。B/C 与签名完全重合 → 排前；
        A 只有 1/3 重合 → 排后。B、C 同分 → 按 (src, how) 稳定收尾，
        书名用 A/B/C 保证**码位序**与直觉一致（不用中文，避免拼音序歧义）。"""
        a = self._m("甲", arc_name="A", no=1, book="书A", tags=["罕见标签", "甲"])
        b = self._m("甲", arc_name="B", no=2, book="书B", tags=["共同标签", "甲"])
        c = self._m("甲", arc_name="C", no=3, book="书C", tags=["共同标签", "甲"])
        t = skel.build_templates({("大战征伐", "家族战争"): [a, b, c]},
                                 VOCAB, {})[0]
        hows = [v["how"] for v in t.structure["phases"][0]["beats"][0]["variants"]]
        assert [h[:1] for h in hows] == ["B", "C", "A"], hows

    def test_consensus_tags_needs_half_the_variants(self):
        assert skel._consensus_tags([["x", "y"], ["x"], ["x"]]) == {"x"}
        assert skel._consensus_tags([["x"], ["y"], ["z"]]) == set()

    def test_display_top_five_marked_on_structure(self):
        g = skel.group_by_key([self._m("甲", arc_name=f"弧{i}", no=i, book=f"书{i}")
                               for i in range(1, 9)])
        t = skel.build_templates(g, VOCAB, {})[0]
        assert t.structure["display_top"] == 5

    def test_skeleton_block_kept_for_program_use(self):
        a = self._m("甲乙", ["甲"])
        t = skel.build_templates({("大战征伐", "家族战争"): [a]},
                                 VOCAB, {})[0]
        assert t.structure["skeleton"]["seq"] == ["甲", "乙"]
        assert t.structure["skeleton"]["repeats"] == [1, 1]
        assert t.structure["skeleton"]["origin"] == "skel_v4"

    def test_source_stats_fields(self):
        a = self._m("甲乙", ["甲"], book="书甲", arc_name="弧A", no=7, conf="低")
        b = self._m("甲乙", ["甲"], book="书乙", arc_name="弧B", no=8, conf="高")
        t = skel.build_templates({("大战征伐", "家族战争"): [a, b]},
                                 VOCAB, {})[0]
        ss = t.source_stats
        assert ss["origin"] == "skel_v4"
        assert ss["books"] == 2 and ss["book_names"] == ["书乙", "书甲"]
        assert ss["n_members"] == 2
        assert ss["class"] == "大战征伐" and ss["sub_event"] == "家族战争"
        assert ss["low_conf_members"] == [7]
        assert ss["single_arc"] is False

    def test_single_member_template_is_flagged_and_tagged(self):
        a = self._m("甲乙", ["甲"], no=5)
        t = skel.build_templates({("大赛征伐", "家族战争"): [a]},
                                 VOCAB, {})[0]
        assert t.source_stats["single_arc"] is True
        assert "孤例" in t.genre_tags

    def test_orphan_class_of_one_is_not_merged_but_flagged(self):
        """孤例类（1 成员）免合并、直接单弧模板 + 孤例标（裁决 3A）。"""
        m = self._m("甲乙丙", ["甲"], no=1)
        m.judgement.cls = "格局变动"
        g = skel.group_by_key([m])
        tpls = skel.build_templates(g, VOCAB,
                                    {"格局变动", "跨域远征", "庙堂权谋"})
        assert len(tpls) == 1
        assert tpls[0].source_stats["single_arc"] is True
        assert tpls[0].source_stats["orphan_class"] is True
        assert "孤例" in tpls[0].genre_tags

    def test_placeholder_arc_is_tagged(self):
        m = self._m("甲乙", ["甲"], no=430)
        m.judgement.book, m.judgement.arc_name = "遮天", "宗门崛起篇"
        t = skel.build_templates({("据点立足", "偏峰立足"): [m]},
                                 VOCAB, {})[0]
        assert "占位弧" in t.genre_tags
        assert t.source_stats["placeholder_arcs"] == [430]

    def test_variant_count_per_beat_is_capped_by_display_top_not_truncated(self):
        """全量存储：variants 存全部成员走法，只是消费端取 display_top。"""
        g = skel.group_by_key([self._m("甲", arc_name=f"弧{i}", no=i, book=f"书{i}")
                               for i in range(1, 13)])
        t = skel.build_templates(g, VOCAB, {})[0]
        assert len(t.structure["phases"][0]["beats"][0]["variants"]) == 12

    def test_same_key_multiple_templates_get_dash_one_two(self):
        """同键合不拢 → 多张模板 → -1/-2 后缀。"""
        a = skel.MemberArc(book="书甲", arc_name="长弧", ch_lo=1, ch_hi=10,
                           seq=[chr(65 + i) for i in range(9)], repeats=[1] * 9,
                           cores=[], beats=[], judgement=skel.Judgement(
                               no=1, book="书甲", arc_name="长弧", cls="大战征伐",
                               sub_event="家族战争", confidence="高", reason="",
                               sub_tags=[]))
        b = skel.MemberArc(book="书乙", arc_name="短弧", ch_lo=1, ch_hi=5,
                           seq=[chr(97 + i) for i in range(9)], repeats=[1] * 9,
                           cores=[], beats=[], judgement=skel.Judgement(
                               no=2, book="书乙", arc_name="短弧", cls="大战征伐",
                               sub_event="家族战争", confidence="高", reason="",
                               sub_tags=[]))
        tpls = skel.build_templates({("大战征伐", "家族战争"): [a, b]},
                                    VOCAB, {})
        names = sorted(t.name for t in tpls)
        assert names == ["大战征伐--家族战争-1", "大战征伐--家族战争-2"]

    def test_name_length_fits_column(self):
        """name 是 VARCHAR(120)，长模板名必须截断而不抛错。"""
        long_sub = "超长" * 40
        m = self._m("甲", ["甲"])
        m.judgement.sub_event = long_sub
        t = skel.build_templates({("大战征伐", long_sub): [m]}, VOCAB, {})[0]
        assert len(t.name) <= 120

    def test_genre_tags_carry_class_sub_event_and_origin(self):
        m = self._m("甲", ["甲"])
        t = skel.build_templates({("大战征伐", "家族战争"): [m]},
                                 VOCAB, {})[0]
        assert "大战征伐" in t.genre_tags
        assert "家族战争" in t.genre_tags
        assert "v4生成" in t.genre_tags


class TestSingleArcReconciliation:
    """验收标准 3：孤例标数量 == 单成员模板数。"""

    def test_counts_reconcile(self):
        g = {
            ("大赛征伐", "家族战争"): [
                skel.MemberArc(book="书甲", arc_name="A", ch_lo=1, ch_hi=9,
                               seq=["甲", "乙"], repeats=[1, 1], cores=["甲"],
                               beats=[], judgement=skel.Judgement(
                                   no=1, book="书甲", arc_name="A", cls="大战征伐",
                                   sub_event="家族战争", confidence="高",
                                   reason="", sub_tags=[])),
                skel.MemberArc(book="书乙", arc_name="B", ch_lo=1, ch_hi=9,
                               seq=["甲", "乙"], repeats=[1, 1], cores=["甲"],
                               beats=[], judgement=skel.Judgement(
                                   no=2, book="书乙", arc_name="B", cls="大战征伐",
                                   sub_event="家族战争", confidence="高",
                                   reason="", sub_tags=[])),
            ],
            ("开山建派", ""): [
                skel.MemberArc(book="书甲", arc_name="C", ch_lo=1, ch_hi=9,
                               seq=["丙"], repeats=[1], cores=[],
                               beats=[], judgement=skel.Judgement(
                                   no=3, book="书甲", arc_name="C", cls="开山建派",
                                   sub_event="", confidence="高", reason="",
                                   sub_tags=[])),
            ],
        }
        tpls = skel.build_templates(g, VOCAB, {})
        flagged = [t for t in tpls if t.source_stats.get("single_arc")]
        assert len(flagged) == 1
        assert flagged[0].name == "开山建派"
        assert sum(1 for t in tpls if t.source_stats["n_members"] == 1) == len(flagged)