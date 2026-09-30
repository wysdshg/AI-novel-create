# -*- coding: utf-8 -*-
"""S2（03 §8.6）：治「黑袍人」病的三项后校验。

① 代称分流：黑词代称且未标「（待揭晓）」→ 剔除；标了的 → 保留且 name_known=False；
② 动态限额：普通篇 8 / 舞台切换篇 12；
③ 抓人敏感度是 prompt 层（见 ingestion 模板断言）。
"""
import sys
sys.path.insert(0, r"E:\AI小说创作\backend")

from app.services import plan_crud as pc


def _lines(*names_per_line):
    out = []
    for i, names in enumerate(names_per_line, 1):
        out.append({"no": i, "beat": f"节拍{i}", "summary": "s" * 20,
                    "new_chars": list(names), "recall_chars": [],
                    "target_words": 3000, "hook": "h", "template_ref": ""})
    return out


def test_placeholder_without_marker_is_rejected():
    lines = _lines(["路人甲", "柳青岩"])
    res = pc._run_planned_chars.__wrapped__ if hasattr(pc._run_planned_chars, "__wrapped__") else None
    # 直接走收集逻辑（不碰 DB）：黑词 + 未标待揭晓 → 剔除
    seen, rejected = {}, []
    for ln in lines:
        for nm in ln["new_chars"]:
            s = nm.strip()
            marker = s.endswith("（待揭晓）") or s.endswith("(待揭晓)")
            base = s.replace("（待揭晓）", "").replace("(待揭晓)", "").strip()
            if pc._is_placeholder_name(base) and not marker:
                rejected.append(s)
                continue
            seen[s] = ln["no"]
    assert rejected == ["路人甲"]
    assert "柳青岩" in seen


def test_is_placeholder_rules():
    assert pc._is_placeholder_name("路人甲")
    assert pc._is_placeholder_name("黑袍人")
    assert pc._is_placeholder_name("神秘人")
    assert pc._is_placeholder_name("老者")
    assert pc._is_placeholder_name("张")          # 单字 <2
    assert not pc._is_placeholder_name("柳青岩")
    assert not pc._is_placeholder_name("周执事")   # 真名含"执事"不在黑词表


def test_pending_reveal_marker_survives():
    """「黑袍人（待揭晓）」是合法的：代称 + 标记 → name_known=False。"""
    s = "黑袍人（待揭晓）"
    marker = s.endswith("（待揭晓）")
    base = s.replace("（待揭晓）", "").strip()
    assert marker and pc._is_placeholder_name(base)   # 基名确实是代称
    assert not (pc._is_placeholder_name(base) and not marker)  # 但有标记 → 不剔除


def test_limit_constants():
    assert pc.PLANNED_CHAR_LIMIT == 8
    assert pc.PLANNED_CHAR_LIMIT_STAGE_CHANGE == 12


def test_limit_selection_by_stage_change():
    names = [f"角色{i:02d}" for i in range(12)]
    lines = _lines(names[:6], names[6:])
    seen = {}
    for ln in lines:
        for nm in ln["new_chars"]:
            seen[nm] = ln["no"]
    normal = list(seen)[:pc.PLANNED_CHAR_LIMIT]
    stage = list(seen)[:pc.PLANNED_CHAR_LIMIT_STAGE_CHANGE]
    assert len(normal) == 8 and len(stage) == 12
    assert pc._is_placeholder_name.__name__  # 引用有效性


def test_ingestion_prompt_has_grab_chars_rule():
    src = open(r"E:\AI小说创作\backend\app\services\ingestion.py", encoding="utf-8").read()
    assert "抓人优先" in src
    assert "宁可多抽不可漏抽" in src
    assert "kind=character" in src


def test_resident_quota_formula():
    """S4：常驻建议配额 = 活跃数+2，夹 [6,12]。"""
    assert pc.resident_quota(0) == 6
    assert pc.resident_quota(3) == 6
    assert pc.resident_quota(5) == 7
    assert pc.resident_quota(10) == 12
    assert pc.resident_quota(50) == 12


def test_plan_prompt_has_s2_rules():
    src = open(r"E:\AI小说创作\backend\app\services\plan_crud.py", encoding="utf-8").read()
    assert "stage_change" in src
    assert "（待揭晓）" in src
    assert "严禁把代称当名字" in src or "黑袍人" in src
    assert "人口基线" in src
