# -*- coding: utf-8 -*-
"""`parse_json_loose` 容错回归（docs/08 B19）。

背景（2026-09-16 试验实测）：九星霸体诀 40 章摘要批量入库时 **4 批丢了 3 批**，
其中 2 批（20 条摘要、章号完整）只是**结尾少了 `]`** —— 旧实现的三次修复
（尾逗号 → 全角引号 → 放弃）对"括号未闭合"这种最常见的截断形态毫无办法，
把已经生成好的内容整批丢弃，逼得生产只能用 `--batch-summarize 1`（2453 次调用）。
"""

from app.services.ingestion import parse_json_loose


def test_plain_json():
    d = parse_json_loose('{"chapters":[{"no":1,"summary":"甲"}]}')
    assert d and d["chapters"][0]["no"] == 1


def test_code_fence_and_leading_prose():
    raw = '好的，以下是结果：\n```json\n{"chapters":[{"no":1,"summary":"甲"}]}\n```'
    d = parse_json_loose(raw)
    assert d and len(d["chapters"]) == 1


def test_trailing_comma():
    d = parse_json_loose('{"chapters":[{"no":1,"summary":"甲"},]}')
    assert d and len(d["chapters"]) == 1


def test_truncated_missing_array_close():
    """只缺 `]`：`{"chapters":[{..},{..}` → 补 `]}`。"""
    d = parse_json_loose('{"chapters":[{"no":1,"summary":"甲"},{"no":2,"summary":"乙"}')
    assert d and [c["no"] for c in d["chapters"]] == [1, 2]


def test_truncated_extra_trailing_brace():
    """🔴 真实形态：结尾多一个 `}`（其实是缺了数组的 `]`）—— 补括号修不好，必须去掉尾 `}`。

    这是 2026-09-16 试验里 20 条摘要被丢弃时的原样结构。
    """
    raw = '{"chapters":[{"no":1,"summary":"甲"},{"no":2,"summary":"乙"}}'
    d = parse_json_loose(raw)
    assert d and [c["no"] for c in d["chapters"]] == [1, 2]


def test_truncated_mid_string_drops_incomplete_item():
    """截断在字符串中间 → 该条不完整，应回退到上一个逗号（宁可少一条，也不许造半句假摘要）。"""
    raw = '{"chapters":[{"no":1,"summary":"完整的一条概括内容"},{"no":2,"summary":"被截断的半'
    d = parse_json_loose(raw)
    assert d and [c["no"] for c in d["chapters"]] == [1]


def test_valid_json_with_fullwidth_quotes_is_not_mangled():
    """🔴 顺序回归：**合法的** JSON 里含中文全角引号，必须原样返回（不许触发换引号的修复）。"""
    raw = '{"summary":"他冷冷道：“你是谁？”","no":1}'
    d = parse_json_loose(raw)
    assert d and d["summary"] == "他冷冷道：“你是谁？”"


def test_broken_json_with_fullwidth_quotes_still_repaired():
    """坏 JSON + 全角引号：修引号必须放在补括号之后仍能救回。"""
    d = parse_json_loose('{"summary":“全角包裹”,}')
    assert d and d["summary"] == "全角包裹"


def test_non_dict_and_garbage_returns_none():
    assert parse_json_loose("这不是 JSON") is None
    assert parse_json_loose("") is None
    assert parse_json_loose("[1,2,3]") is None      # 顶层是 list → 按契约返回 None
