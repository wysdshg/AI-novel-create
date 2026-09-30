# -*- coding: utf-8 -*-
"""B18×S3（2026-09-17）：章节摄取抽到的角色变化 → 只写 pending 修订（绝不改卡）。"""
import sys
sys.path.insert(0, r"E:\AI小说创作\backend")

from app.services import ingestion as ing


def test_normalize_extract_parses_char_changes():
    d = ing.normalize_extract({"summary": "s", "char_changes": [
        {"name": "林砚", "change": "境界突破", "current_level": "金丹"},
        {"name": "", "change": "无效条目"},
        {"name": "苏小满", "personality": "更果决"},
    ]})
    cc = d["char_changes"]
    assert len(cc) == 2                       # 无名条目被丢弃
    assert cc[0]["name"] == "林砚" and cc[0]["fields"] == {"current_level": "金丹"}
    assert cc[1]["fields"] == {"personality": "更果决"}
    assert "char_changes" in ing._JSON_KEYS


def test_ingest_prompt_has_char_changes_rule():
    src = open(r"E:\AI小说创作\backend\app\services\ingestion.py", encoding="utf-8").read()
    assert "char_changes" in src
    assert "待作者确认的修订" in src
    assert "不会自动改角色卡" in src
