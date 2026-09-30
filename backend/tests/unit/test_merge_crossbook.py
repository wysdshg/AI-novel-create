# -*- coding: utf-8 -*-
"""跨书归并判据单测（2026-09-19 用户拍板版：双门槛 + 15 环参考规则 + 动态 min 分段）
用例全部来自 docs/10 §11.17/§11.18/§11.19 拍板记录与提案合并例，can_merge 为纯函数、不依赖 DB。
动态 min 分段（拍板②）：较长方 <5 环 → t_short(0.65)；[5,10) → 0.70；≥10 → 0.85。
调用约定：can_merge(A, B, thr_min, thr_max, max_len) 的 thr_min = **短弧段门槛 t_short**。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from merge_arcs_crossbook import Group, align_positions, can_merge, compress, lcs_len, scs_merge


def G(seq, cores=()):
    return Group(gid=0, seq=list(seq), repeats=[1] * len(seq), cores=list(cores))


# ---- 提案自己的合并例（锚点：唯一权威的「该并」样例）----
def test_proposal_example_merges():
    A = G("甲乙丙丁戊己")           # 6 环
    B = G("甲丙戊庚")               # 4 环，LCS=3
    mode, score, _ = can_merge(A, B, 0.7, 0.5, 15)
    assert mode == "merge"
    assert score == 0.75           # 3/min(4,6)
    # SCS = 7 环：甲乙丙丁戊己 + 庚（= 提案的合并结果形态）
    m, _ = scs_merge(A.seq, A.repeats, B.seq, B.repeats)
    assert len(m) == 7 and m[0] == "甲"


# ---- max 门槛：拦长短悬殊（2026-09-19 拍板）----
def test_single_atom_vs_long_rejected():
    # 1 环挂 7 环：r_min = 1/1 = 1.0 过 min，r_max = 1/7 = 0.14 → max 拦
    mode, _, why = can_merge(G("ABCDEFG"), G("A"), 0.7, 0.5, 15)
    assert mode == "reject" and "max" in why


def test_4ring_vs_12ring_3same_rejected():
    # 用户点名的情形：「4 个弧的有 3 个弧相同，就不要加了」。
    # 动态门槛后 max(12,4)=12 ≥ 10 → t_eff=0.85 → r_min=0.75 先被 min(动态长组段) 拦；
    # r_max=0.25 也远低于 0.5 —— 双卡，拦的语义不变。
    mode, score, why = can_merge(G("ABCDEFGHIJKL"), G("ABCM"), 0.65, 0.5, 15)
    assert mode == "reject" and abs(score - 0.75) < 1e-9
    assert "动态" in why


# ---- 动态 min·短弧段（拍板②）：3 环只有 0.33/0.67/1.0 三档，0.65 解锁「只差一环」档 ----
def test_short_arc_dynamic_gate_merges():
    # 同构 3 环对 3 环 LCS=2 → min=max=0.67 ≥ 0.65（动态短弧门槛）→ 并（原斗×太跨书桥死因，拍板②解锁）
    mode, score, _ = can_merge(G("AHF"), G("AGF"), 0.65, 0.5, 15)
    assert mode == "merge" and abs(score - 0.67) < 0.01


def test_short_arc_gate_value_still_bites():
    # 对照：同对若短弧门槛给旧静态值 0.7 → 0.67 < 0.7 仍拒 —— 门槛值本身仍起作用
    mode, score, why = can_merge(G("AHF"), G("AGF"), 0.7, 0.5, 15)
    assert mode == "reject" and abs(score - 0.67) < 0.01 and "动态" in why


# ---- 动态 min·长组段（拍板②「主环十个时 5/6 没必要并、至少 6/7」）----
def test_long_group_gate_blocks_5_of_6():
    # 10 环组吸 6 环 LCS=5 → r_min=5/6≈0.83 < 0.85 → 拒（即使 r_max=0.5 刚好过 max）
    mode, score, why = can_merge(G("ABCDEFGHIJ"), G("ABCDEZ"), 0.65, 0.5, 15)
    assert mode == "reject" and abs(score - 5 / 6) < 1e-9 and "动态" in why


def test_long_group_gate_allows_6_of_7():
    # 10 环组吸 7 环 LCS=6 → r_min=6/7≈0.857 ≥ 0.85 且 r_max=6/10=0.6 ≥ 0.5 → 并
    mode, score, _ = can_merge(G("ABCDEFGHIJ"), G("ABCDEFZ"), 0.65, 0.5, 15)
    assert mode == "merge" and abs(score - 6 / 7) < 1e-9


def test_max_gate_applies_below_15():
    # 15 环组吸 1 环：SCS=15 未超限 → 走骨架通道 → max 0.067 拦
    mode, _, why = can_merge(G("ABCDEFGHIJKLMNO"), G("A"), 0.7, 0.5, 15)
    assert mode == "reject" and "max" in why


# ---- 15 环超限规则：仅完全包含挂参考，否则另起模板（2026-09-19 拍板）----
def test_overlong_fully_contained_reference():
    mode, score, _ = can_merge(G("ABCDEFGHIJKLMNOP"), G("BDF"), 0.7, 0.5, 15)
    assert mode == "reference" and score == 1.0


def test_overlong_contained_9ring_reference():
    mode, _, _ = can_merge(G("ABCDEFGHIJKLMNOP"), G("BCDEFGHIJ"), 0.7, 0.5, 15)
    assert mode == "reference"


def test_overlong_not_contained_new_template():
    mode, _, why = can_merge(G("ABCDEFGHIJKLMNOP"), G("BDX"), 0.7, 0.5, 15)
    assert mode == "reject" and "另起模板" in why


def test_15ring_4ring_3same_rejected():
    # SCS=16 超限且非完全包含 → 另起模板
    mode, _, _ = can_merge(G("ABCDEFGHIJKLMNO"), G("BDFZ"), 0.7, 0.5, 15)
    assert mode == "reject"


# ---- 单向核心覆盖（提案 §11.1③ 语义）----
def test_core_unidirectional():
    A = G(["A", "B"], cores=["A"])          # 组核心 A
    B = G(["A", "B", "C"])                  # B 含 A 核心 → 可并
    mode, _, _ = can_merge(A, B, 0.7, 0.5, 15)
    assert mode == "merge"
    # B 的核心组没有 → 不拦（单向语义），合并后进核心并集
    A2 = G(["A", "B"])
    B2 = G(["A", "B", "C"], cores=["C"])
    mode2, _, _ = can_merge(A2, B2, 0.7, 0.5, 15)
    assert mode2 == "merge"
    # B 缺组核心 → 拒
    A3 = G(["A", "B"], cores=["A"])
    B3 = G(["B", "C"])
    mode3, _, why = can_merge(A3, B3, 0.7, 0.5, 15)
    assert mode3 == "reject" and "核心" in why


# ---- 基础算子回归 ----
def test_compress_repeats():
    seq, rep = compress(["A02", "A02", "A02", "B01"])
    assert seq == ["A02", "B01"] and rep == [3, 1]


def test_scs_subsequence_identity():
    a, b = list("ABCD"), list("BC")
    m, _ = scs_merge(a, [1] * 4, b, [1] * 2)
    assert m == a                            # 完全包含时 SCS=长序列
    assert lcs_len(a, b) == 2


# ---- align_positions（2026-09-19 修复：旧 LCS 前向回溯值错位 → 多原子成员全空）----
def test_align_positions_multi_atom():
    g = ["G06", "C05", "A05", "A08", "A01", "A08", "F04", "C06"]
    assert align_positions(["C05", "A05", "A08"], g) == [[1], [2], [3]]
    assert align_positions(["G06", "A05", "A01"], g) == [[0], [2], [4]]
    assert align_positions(["A01"], g) == [[4]]


def test_align_positions_missing_atom():
    # 成员独有环（组上无位）→ []，后续原子找不到位也 []
    assert align_positions(["C05", "B99"], ["G06", "C05", "A05"]) == [[1], []]
