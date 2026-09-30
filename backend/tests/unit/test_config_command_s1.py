# -*- coding: utf-8 -*-
"""角色立体化 **S1**：建卡 prompt 注入 top-3 原型（2026-09-17）。

测的是 `config_command._archetype_hints` 的四条硬约定：
1. 向量开关关 → 返回空（原型库是增强项）；
2. 检索用 **char_archetype** 这个 source_type + 全局池 + top_k=3；
3. 命中时把原型文本与「不得照抄」的要求拼进 prompt；
4. **任何异常都不许冒泡**（原型库召回失败绝不能阻断建卡）。
"""
import sys
sys.path.insert(0, r"E:\AI小说创作\backend")

from app.services import config_command as cc
from app.services import vector_index as vi
from app.services import plot_template_crud as tpl


class _Hit:
    def __init__(self, text, score):
        self.chunk_text = text
        self.score = score


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(vi, "enabled", lambda db: False)
    assert cc._archetype_hints(None, "帮我建个护道长老") == ""


def test_uses_archetype_source_type_and_topk(monkeypatch):
    seen = {}

    def _search(db, project_id, source_type, query_text, top_k=5):
        seen.update(project_id=project_id, source_type=source_type, top_k=top_k, query=query_text)
        return []

    monkeypatch.setattr(vi, "enabled", lambda db: True)
    monkeypatch.setattr(vi, "search_similar", _search)
    cc._archetype_hints(None, "主角", top_k=3)
    assert seen["source_type"] == tpl.SOURCE_TYPE_ARCHETYPE
    assert seen["project_id"] == tpl.GLOBAL
    assert seen["top_k"] == 3


def test_formats_hits_with_no_copy_rule(monkeypatch):
    hits = [
        _Hit("引路同伴｜位阶：散修/同辈｜性格：利他+4·信义+7｜与主角同行的修士｜定位：助力", 0.81),
        _Hit("秉公长老｜位阶：长老/宗门高层｜性格：自律+7·理性+7｜主持考核的长老｜定位：见证", 0.76),
    ]
    monkeypatch.setattr(vi, "enabled", lambda db: True)
    monkeypatch.setattr(vi, "search_similar", lambda *a, **k: hits)
    out = cc._archetype_hints(None, "建个宗门长老")
    assert "引路同伴" in out and "秉公长老" in out
    assert "相似度 0.81" in out
    assert "不得照抄" in out                  # 反抄袭约束必须在 prompt 里出现
    assert out.startswith("\n")               # 可安全拼在 SYSTEM_PROMPT 后面


def test_no_hits_returns_empty(monkeypatch):
    monkeypatch.setattr(vi, "enabled", lambda db: True)
    monkeypatch.setattr(vi, "search_similar", lambda *a, **k: [])
    assert cc._archetype_hints(None, "建个角色") == ""


def test_exception_never_propagates(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("embedding 挂了")

    monkeypatch.setattr(vi, "enabled", lambda db: True)
    monkeypatch.setattr(vi, "search_similar", _boom)
    assert cc._archetype_hints(None, "建个角色") == ""


def test_dict_hits_supported(monkeypatch):
    """检索结果也可能是 dict（不同 store 实现）→ 两种形状都要能吃。"""
    monkeypatch.setattr(vi, "enabled", lambda db: True)
    monkeypatch.setattr(vi, "search_similar",
                        lambda *a, **k: [{"chunk_text": "敌对同辈｜位阶：同辈｜定位：对手", "score": 0.7}])
    out = cc._archetype_hints(None, "建个对手")
    assert "敌对同辈" in out and "相似度 0.70" in out
