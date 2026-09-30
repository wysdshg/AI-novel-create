# -*- coding: utf-8 -*-
"""角色画像聚合（方案 A，零调用）回归 —— `docs/08-C8` / `outputs/角色性格维度设计-草案.md`。

背景（2026-09-16 实测）：`traits` 是**弧级观测**（凝练每个模板时顺手打的），
而同一角色会出现在多个模板里 → 逐组打分必然漂移。
实测九星「主角」在 9 个模板里拿到 9 套分（12 维极差合计 40、平均每维差 3.3 档）。
方案 A：**不调模型**，把这些观测按 `(book, alias)` 逐维取**中位数**，收敛成**一套**角色画像。
"""

from app.services.plot_distill import _median_profile


def test_median_resists_local_outlier():
    """🔴 用户举的那个例子：一直无恶不作、某篇做了两件好事 → **中位不翻正**（平均会被拉走）。"""
    obs = [{"mercy": -7}] * 8 + [{"mercy": 4}]
    traits, meta = _median_profile(obs)
    assert traits["mercy"] == -7          # 中位：-7（不动）
    assert meta["n"] == 9


def test_median_stays_on_anchor():
    """观测值都落在 9 档锚点上 → **中位数仍是锚点**，保持"档位可比"（平均会造出 +3.7 这种档间值）。"""
    obs = [{"altruism": v} for v in (1, 4, 4, 1, 4, 4, 7, 4, 4)]
    traits, meta = _median_profile(obs)
    assert traits["altruism"] == 4
    assert traits["altruism"] in (0, 1, -1, 4, -4, 7, -7, 10, -10)
    assert meta["spread"] == {"altruism": 6}      # 7-1=6，该维观测不稳


def test_empty_or_invalid_returns_empty():
    assert _median_profile([]) == ({}, {})
    assert _median_profile([None, "x", {}]) == ({}, {})      # 空 dict 不算观测


def test_single_observation_passes_through():
    traits, meta = _median_profile([{"honor": -7}])
    assert traits == {"honor": -7} and meta["n"] == 1 and meta["spread"] == {}


def test_missing_dims_are_skipped_not_zeroed():
    """某维在所有观测里都缺 → **不进画像**（不是补 0：0 是"确实无信息"的语义）。"""
    traits, _ = _median_profile([{"honor": 4}, {"honor": -4}])
    assert set(traits) == {"honor"}
    assert traits["honor"] == 0          # median([4,-4]) = 0，且 0 本身就是锚点


def test_even_count_median_snaps_back_to_anchor():
    """🔴 偶数个观测时 median 会落在档位之间 → 必须**吸附回锚点**，保持档位可比。"""
    from app.services.plot_distill import _snap_anchor
    assert _snap_anchor(0) == 0
    assert _snap_anchor(2.5) == 1          # (1+4)/2 → 吸附到 1（并列取绝对值小者，偏保守）
    assert _snap_anchor(-2.5) == -1
    assert _snap_anchor(5.5) == 4
    assert _snap_anchor(8.5) == 7
    assert _snap_anchor(99) == 10           # 越界也吸到极值锚点
    assert _median_profile([{"guile": 1}, {"guile": 4}])[0]["guile"] == 1


def test_all_outputs_are_on_anchors():
    """画像里**每一个值**都必须是 9 档锚点之一（这是"档位可比"的硬保证）。"""
    obs = [{"altruism": 1, "honor": 7}, {"altruism": 4, "honor": -1}, {"altruism": -4, "honor": 4}]
    traits, _ = _median_profile(obs)
    assert all(v in (0, 1, -1, 4, -4, 7, -7, 10, -10) for v in traits.values())
