# -*- coding: utf-8 -*-
"""[DEV-P3a] 检索精确键优先匹配链 + 计划链三小修 的零外部依赖单测。

P3 实测发现（PM 已复核属实）：`plot_template_crud.search` 是纯向量+关键词，
**没有**按 (大类,子事件) 精确键优先的分支 —— v4 拍板的匹配链只存在于组装侧。
本文件覆盖：
  ① 匹配链：词表快路径 / 轻量 LLM 分类 / 三层重排 / **LLM 挂了不阻断主链**（红线）
  ② 计划 prompt 命名示例改成不可误用的占位符（P3 实测「柳青岩」泄漏成真角色）
  ③ 计划行解析失败自动重试 1 次
  ④ 计划链走网关改流式（对齐章节链 SSE 帧解析）

🔴 红线测试（`TestKeyMatchNeverBlocks`）用例会 monkeypatch 让分类器抛异常/超时，
   断言 search 仍返回向量序结果、且 key_match 账里注明失败原因。
"""
from __future__ import annotations

import pytest

from app.services import plan_crud
from app.services import plot_import
from app.services import plot_template_crud as tpl
from app.services import vector_index

DIM = 64


def _vec(kind: int) -> list[float]:
    v = [0.0] * DIM
    v[kind % DIM] = 1.0
    return v


def _enable_vector(monkeypatch, kind_by_text: dict):
    from app.services.vector_store import BruteVectorStore
    monkeypatch.setattr(vector_index, "enabled", lambda db: True)
    monkeypatch.setattr(vector_index, "get_store", lambda db: BruteVectorStore())

    def fake_embed(texts, db=None):
        out = []
        for t in texts:
            kind = 99
            for keyword, probe_kind in kind_by_text.items():
                if keyword in t:
                    kind = probe_kind
                    break
            out.append(_vec(kind))
        return out

    monkeypatch.setattr(vector_index.embedding_client, "embed_texts", fake_embed)


def _stub_structure(beat="比试"):
    return {"phases": [{"phase": "起", "beats": [
        {"beat": beat, "variants": [{"src": "甲书", "how": "某书某弧的走法"}]}]}]}


def _mk(db, name, cls, sub, logline="一部通用情节骨架"):
    # 🔴 status 必须显式给 "active"：`create()` 默认 draft，而检索池与 key_vocab 都只认 active
    return tpl.create(db, {
        "name": name, "scale": "arc", "genre_tags": ["原子骨架"], "status": "active",
        "logline": logline, "structure": _stub_structure(),
        "source_stats": {"origin": "skel_v4", "class": cls, "sub_event": sub,
                         "n_members": 1, "books": 1, "single_arc": True},
    })


@pytest.fixture()
def keyed(test_db):
    """三张同大类不同子事件 + 一张别的大类，模拟真实 616 张库的形态。"""
    a = _mk(test_db, "擂台大比--宗门擂台比试", "擂台大比", "宗门擂台比试", "擂台上一战成名")
    b = _mk(test_db, "擂台大比--夺宝争锋", "擂台大比", "夺宝争锋", "擂台上边打边抢宝")
    c = _mk(test_db, "秘境夺宝--古殿捡漏", "秘境夺宝", "古殿捡漏", "古殿里捡漏")
    test_db.commit()
    return {"a": a, "b": b, "c": c}


def _no_llm(monkeypatch):
    """默认禁掉 LLM 分类（除非用例显式打开）—— 单测不许真调网关。
    返回的账要跟真实实现同形状，否则断言 key_match 字段的用例会假绿/假红。"""
    monkeypatch.setattr(tpl, "_llm_guess_key",
                        lambda db, q, v, **kw: (None, {"source": "off", "ok": False,
                                                        "class": None, "sub_event": None,
                                                        "reason": "未启用 LLM 分类"}))


# ---------------------------------------------------------------------------
# ① 词表
# ---------------------------------------------------------------------------
class TestKeyVocab:
    def test_reads_class_and_sub_from_source_stats(self, keyed, test_db):
        v = tpl.key_vocab(test_db)
        assert "擂台大比" in v and "秘境夺宝" in v
        assert "宗门擂台比试" in v["擂台大比"]
        assert "夺宝争锋" in v["擂台大比"]

    def test_empty_library_is_empty_vocab(self, test_db):
        assert tpl.key_vocab(test_db) == {}

    def test_ignores_non_arc_and_archived(self, test_db):
        a = _mk(test_db, "擂台大比--甲", "擂台大比", "甲")
        _mk(test_db, "秘境夺宝--乙", "秘境夺宝", "乙")
        tpl.update(test_db, a.id, {"status": "archived"})   # list_templates 返回 dict
        test_db.commit()
        v = tpl.key_vocab(test_db)
        assert "甲" not in v.get("擂台大比", [])
        assert "乙" in v.get("秘境夺宝", [])


# ---------------------------------------------------------------------------
# ① 词表快路径
# ---------------------------------------------------------------------------
class TestKeywordGuess:
    def test_exact_sub_event_hit(self, keyed, test_db):
        v = tpl.key_vocab(test_db)
        assert tpl._keyword_guess_key("主角在宗门擂台比试上一战成名", v) == \
            ("擂台大比", "宗门擂台比试")

    def test_class_only_hit_has_empty_sub(self, keyed, test_db):
        v = tpl.key_vocab(test_db)
        assert tpl._keyword_guess_key("主角在擂台大比上打", v) == ("擂台大比", "")

    def test_miss_returns_none(self, keyed, test_db):
        v = tpl.key_vocab(test_db)
        assert tpl._keyword_guess_key("主角在雪山疗伤偶遇故人", v) is None

    def test_blank_query_returns_none(self, keyed, test_db):
        assert tpl._keyword_guess_key("", tpl.key_vocab(test_db)) is None

    def test_longest_name_wins(self, test_db):
        """「擂台大比」与「擂台大比--夺宝争锋」都在词表时取更具体的那个键。"""
        _mk(test_db, "擂台大比--夺宝争锋", "擂台大比", "夺宝争锋")
        _mk(test_db, "擂台大比--宗门擂台比试", "擂台大比", "宗门擂台比试")
        test_db.commit()
        v = tpl.key_vocab(test_db)
        assert tpl._keyword_guess_key("主角在夺宝争锋里翻盘", v) == ("擂台大比", "夺宝争锋")


# ---------------------------------------------------------------------------
# ① 三层重排
# ---------------------------------------------------------------------------
class TestRerank:
    def _items(self, *specs):
        return [{"id": i, "name": n, "source_stats": {"class": c, "sub_event": s}}
                for i, n, c, s in specs]

    def test_exact_key_goes_first(self):
        items = self._items(("1", "别的大类", "秘境夺宝", "古殿捡漏"),
                            ("2", "同大类别的子事件", "擂台大比", "夺宝争锋"),
                            ("3", "精确键", "擂台大比", "宗门擂台比试"))
        out = tpl._rerank_by_key(items, ("擂台大比", "宗门擂台比试"))
        assert [i["name"] for i in out] == ["精确键", "同大类别的子事件", "别的大类"]

    def test_class_only_puts_whole_class_ahead(self):
        items = self._items(("1", "别的大类", "秘境夺宝", "古殿捡漏"),
                            ("2", "同大类A", "擂台大比", "夺宝争锋"),
                            ("3", "同大类B", "擂台大比", "宗门擂台比试"))
        out = tpl._rerank_by_key(items, ("擂台大比", ""))
        assert out[0]["source_stats"]["class"] == "擂台大比"
        assert out[-1]["name"] == "别的大类"

    def test_no_key_is_identity(self):
        items = self._items(("1", "a", "擂台大比", "甲"), ("2", "b", "秘境夺宝", "乙"))
        assert tpl._rerank_by_key(items, None) == items

    def test_stable_within_tier(self):
        """同级内保持原向量序（不能被 sort 的稳定性以外的任何因素打乱）。"""
        items = self._items(("1", "a1", "擂台大比", "x"), ("2", "a2", "擂台大比", "x"),
                            ("3", "b1", "秘境夺宝", "y"))
        out = tpl._rerank_by_key(items, ("擂台大比", "x"))
        assert [i["id"] for i in out] == ["1", "2", "3"]

    def test_missing_source_stats_is_tier_two(self):
        items = [{"id": "1", "name": "老模板", "source_stats": {}},
                 {"id": "2", "name": "命中", "source_stats": {"class": "擂台大比",
                                                            "sub_event": "甲"}}]
        out = tpl._rerank_by_key(items, ("擂台大比", "甲"))
        assert out[0]["name"] == "命中"


# ---------------------------------------------------------------------------
# ① 检索层端到端（红线在这里）
# ---------------------------------------------------------------------------
class TestKeyMatchInSearch:
    def test_exact_key_reranks_vector_order(self, keyed, test_db, monkeypatch):
        """向量把「别的大类」排第一时，精确键必须被提到最前。"""
        _enable_vector(monkeypatch, {"擂台": 0, "秘境": 1})
        for o in (keyed["a"], keyed["b"], keyed["c"]):
            tpl.index_template(test_db, o)
        monkeypatch.setattr(tpl, "_llm_guess_key",
                            lambda db, q, v, **kw: (("擂台大比", "宗门擂台比试"),
                                                    {"source": "llm", "ok": True,
                                                     "class": "擂台大比",
                                                     "sub_event": "宗门擂台比试"}))
        r = tpl.search(test_db, query="主角在擂台上凭真本事夺魁", scale="arc")
        assert r["items"][0]["name"] == "擂台大比--宗门擂台比试"
        assert r["key_match"]["class"] == "擂台大比"
        assert r["key_match"]["sub_event"] == "宗门擂台比试"
        assert r["key_match"]["ok"] is True
        assert r["key_match"]["source"] == "llm", "口述没直接含词表名 → 该走 LLM 分类"
        # 顺序是否真的变了由假向量的原始序决定（这里恰好已正确），「变了会怎样」
        # 由 TestRerank 的纯函数用例覆盖 —— 集成用例只断言**契约**：精确键在 top1 + 账完整。

    def test_keyword_fast_path_needs_no_llm(self, keyed, test_db, monkeypatch):
        _enable_vector(monkeypatch, {"擂台": 0, "秘境": 1})
        for o in (keyed["a"], keyed["b"], keyed["c"]):
            tpl.index_template(test_db, o)

        def _boom(*a, **kw):
            raise AssertionError("词表已命中，不该调 LLM")

        monkeypatch.setattr(tpl, "_llm_guess_key", _boom)
        r = tpl.search(test_db, query="主角在夺宝争锋里翻盘", scale="arc")
        assert r["key_match"]["source"] == "keyword"
        assert r["items"][0]["name"] == "擂台大比--夺宝争锋"

    def test_offtopic_query_does_not_force_top(self, keyed, test_db, monkeypatch):
        """验收 2：反例口述不强行精确置顶。"""
        _enable_vector(monkeypatch, {"疗伤": 0, "擂台": 1, "秘境": 2})
        for o in (keyed["a"], keyed["b"], keyed["c"]):
            tpl.index_template(test_db, o)
        monkeypatch.setattr(tpl, "_llm_guess_key",
                            lambda db, q, v, **kw: (None, {"source": "llm", "ok": False,
                                                            "class": None, "sub_event": None,
                                                            "reason": "无匹配类目"}))
        r = tpl.search(test_db, query="主角在雪山疗伤偶遇故人", scale="arc")
        assert r["items"], "反例口述也必须照常召回"
        assert r["key_match"]["ok"] is False
        assert r["key_match"].get("class") in (None, "")
        assert r["reranked"] is False, "没有键就不许重排"

    def test_key_match_absent_when_llm_disabled(self, keyed, test_db, monkeypatch):
        _enable_vector(monkeypatch, {"擂台": 0})
        tpl.index_template(test_db, keyed["a"])
        _no_llm(monkeypatch)
        r = tpl.search(test_db, query="随便什么口述", scale="arc")
        assert r["key_match"]["ok"] is False
        assert "禁用" in r["key_match"]["reason"] or r["key_match"]["source"] == "off"


    def test_conflicting_guess_does_not_override_vector(self, keyed, test_db, monkeypatch):
        """🔴 实测驱动的守卫：分类器判错类时**不许**把错误类顶上去。

        P3a 实网冒烟里，同一句口述分类器给出过两个不同答案（擂台大比 / 拜师入门），
        后者把 `拜师入门--宗门试炼-2` 顶到 top1、把向量排第一的正确模板压下去 ——
        精确键链把分类错误**放大**成了排序事故。
        守卫：猜测类与向量 top1 的类不一致 → 记冲突、**不重排**，以向量序为准。
        """
        _enable_vector(monkeypatch, {"擂台": 0, "秘境": 1})
        for o in (keyed["a"], keyed["b"], keyed["c"]):
            tpl.index_template(test_db, o)
        monkeypatch.setattr(tpl, "_llm_guess_key",
                            lambda db, q, v, **kw: (("秘境夺宝", "古殿捡漏"),
                                                    {"source": "llm", "ok": True,
                                                     "class": "秘境夺宝",
                                                     "sub_event": "古殿捡漏"}))
        r = tpl.search(test_db, query="主角在擂台上凭真本事夺魁", scale="arc")
        assert r["items"][0]["name"] != "秘境夺宝--古殿捡漏", "错类不许被提升"
        km = r["key_match"]
        assert km["conflict"] is True
        assert km["vector_top_class"] and km["vector_top_class"] != "秘境夺宝"
        assert km["applied"] is False
        assert r["reranked"] is False

    def test_agreeing_guess_is_applied(self, keyed, test_db, monkeypatch):
        """守卫的反面：猜测类与向量 top1 一致 → 正常重排（精确子事件置顶）。"""
        _enable_vector(monkeypatch, {"擂台": 0, "秘境": 1})
        for o in (keyed["a"], keyed["b"], keyed["c"]):
            tpl.index_template(test_db, o)
        monkeypatch.setattr(tpl, "_llm_guess_key",
                            lambda db, q, v, **kw: (("擂台大比", "宗门擂台比试"),
                                                    {"source": "llm", "ok": True,
                                                     "class": "擂台大比",
                                                     "sub_event": "宗门擂台比试"}))
        r = tpl.search(test_db, query="主角在擂台上凭真本事夺魁", scale="arc")
        assert r["items"][0]["name"] == "擂台大比--宗门擂台比试"
        assert r["key_match"]["conflict"] is False
        assert r["key_match"]["applied"] is True

    def test_keyword_fast_path_also_defers_to_vector(self, keyed, test_db, monkeypatch):
        """词表快路径也不许覆盖向量（守卫对两条推断路径一视同仁）。"""
        _enable_vector(monkeypatch, {"擂台": 0, "秘境": 1})
        for o in (keyed["a"], keyed["b"], keyed["c"]):
            tpl.index_template(test_db, o)
        # 口述含「古殿捡漏」（词表命中 秘境夺宝），但向量 top1 是擂台大比 → 应让位
        r = tpl.search(test_db, query="主角在擂台夺魁顺手古殿捡漏", scale="arc")
        assert r["key_match"]["source"] == "keyword"
        assert r["key_match"]["applied"] is False
        assert r["reranked"] is False



class TestKeyMatchNeverBlocks:
    """🔴 红线：分类器挂/超时/返回垃圾，检索主链必须照常返回向量序。"""

    def _prep(self, keyed, test_db, monkeypatch):
        _enable_vector(monkeypatch, {"擂台": 0, "秘境": 1})
        for o in (keyed["a"], keyed["b"], keyed["c"]):
            tpl.index_template(test_db, o)

    @pytest.mark.parametrize("boom", [
        RuntimeError("网关连不上"),
        TimeoutError("timed out"),
        ValueError("模型返回了非 JSON"),
        OSError("connection refused"),
    ])
    def test_exception_falls_back_to_vector_order(self, keyed, test_db, monkeypatch, boom):
        self._prep(keyed, test_db, monkeypatch)

        def _raise(*a, **kw):
            raise boom

        monkeypatch.setattr(tpl, "_llm_guess_key", _raise)
        r = tpl.search(test_db, query="主角在擂台上一战成名", scale="arc")
        assert r["items"], "分类器炸了也必须召回模板"
        assert r["key_match"]["ok"] is False
        assert r["key_match"]["reason"]
        assert r["key_match"]["class"] in (None, "")
        assert r["reranked"] is False

    def test_slow_classifier_does_not_hold_the_chain(self, keyed, test_db, monkeypatch):
        """分类器返回 ok=False（它自己吞掉了超时）也要照常返回。"""
        self._prep(keyed, test_db, monkeypatch)
        monkeypatch.setattr(tpl, "_llm_guess_key",
                            lambda db, q, v, **kw: (None, {"source": "llm", "ok": False,
                                                            "class": None, "sub_event": None,
                                                            "reason": "分类超时(8s)"}))
        r = tpl.search(test_db, query="主角在擂台上一战成名", scale="arc")
        assert r["items"]
        assert r["key_match"]["reason"] == "分类超时(8s)"

    def test_search_never_raises_even_if_rerank_blows_up(self, keyed, test_db, monkeypatch):
        self._prep(keyed, test_db, monkeypatch)
        monkeypatch.setattr(tpl, "_llm_guess_key",
                            lambda db, q, v, **kw: (("擂台大比", "宗门擂台比试"),
                                                    {"source": "llm", "ok": True}))

        def _bad(items, key):
            raise RuntimeError("重排炸了")

        monkeypatch.setattr(tpl, "_rerank_by_key", _bad)
        r = tpl.search(test_db, query="主角在宗门擂台比试上打", scale="arc")
        assert r["items"], "重排炸了也必须返回向量序（不能整条检索失败）"


class TestLlmGuessKey:
    def test_classifier_does_not_retry(self, monkeypatch):
        """🔴 分类只试 1 次：网关拒连时作者要立刻拿到向量序，不该白等 38s。

        实测（outputs/p3_inject/P3a_红线实测.txt）：`_sf_post` 默认重试 5 次 +
        指数退避，网关挂掉时 key_match 耗时 38.3s 才降级 —— 对「锦上添花」的
        分类层来说就是把主链堵住了。
        """
        seen = {}

        def _fake_sf_chat(db, p, **kw):
            seen.update(kw)
            return '{"class":"擂台大比","sub_event":"甲"}'

        monkeypatch.setattr(plot_import, "sf_chat", _fake_sf_chat)
        tpl._llm_guess_key(None, "q", {"擂台大比": ["甲"]})
        assert seen.get("attempts") == 1, "分类层必须只试一次"
        assert seen.get("timeout") == tpl.KEY_MATCH_TIMEOUT
        assert seen.get("temperature") == 0.0, "分类要确定性，temp 必须 0"
        assert seen.get("max_tokens") == tpl.KEY_MATCH_MAX_TOKENS

    def test_parses_plain_json(self, monkeypatch):
        monkeypatch.setattr(plot_import, "sf_chat",
                            lambda db, p, **kw: '{"class": "擂台大比", "sub_event": "宗门擂台比试"}')
        key, acct = tpl._llm_guess_key(None, "打一场大比", {"擂台大比": ["宗门擂台比试"]})
        assert key == ("擂台大比", "宗门擂台比试")
        assert acct["ok"] is True and acct["source"] == "llm"

    def test_strips_code_fence(self, monkeypatch):
        monkeypatch.setattr(plot_import, "sf_chat",
                            lambda db, p, **kw: '好的：\n```json\n{"class":"擂台大比","sub_event":"甲"}\n```')
        key, _ = tpl._llm_guess_key(None, "q", {"擂台大比": ["甲"]})
        assert key == ("擂台大比", "甲")

    def test_rejects_class_outside_vocab(self, monkeypatch):
        """LLM 编了个不存在的类 → 当无命中（防幻觉进重排）。"""
        monkeypatch.setattr(plot_import, "sf_chat",
                            lambda db, p, **kw: '{"class": "不存在的类", "sub_event": "x"}')
        key, acct = tpl._llm_guess_key(None, "q", {"擂台大比": ["甲"]})
        assert key is None
        assert acct["ok"] is False, "没拿到可用键就算失败（账里要能看出来）"
        assert "不在词表内" in acct["reason"]

    def test_rejects_sub_outside_class(self, monkeypatch):
        monkeypatch.setattr(plot_import, "sf_chat",
                            lambda db, p, **kw: '{"class": "擂台大比", "sub_event": "瞎编的"}')
        key, acct = tpl._llm_guess_key(None, "q", {"擂台大比": ["宗门擂台比试"]})
        assert key == ("擂台大比", ""), "子事件不在该类下 → 只保留大类层"

    def test_empty_vocab_skips_llm_entirely(self, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("词表为空不该调 LLM")

        monkeypatch.setattr(plot_import, "sf_chat", _boom)
        key, acct = tpl._llm_guess_key(None, "q", {})
        assert key is None and acct["ok"] is False

    def test_never_raises_on_bad_json(self, monkeypatch):
        monkeypatch.setattr(plot_import, "sf_chat", lambda db, p, **kw: "这不是 JSON")
        key, acct = tpl._llm_guess_key(None, "q", {"擂台大比": ["甲"]})
        assert key is None and acct["ok"] is False and acct["reason"]

    def test_never_raises_on_model_exception(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("网关挂")

        monkeypatch.setattr(plot_import, "sf_chat", _boom)
        key, acct = tpl._llm_guess_key(None, "q", {"擂台大比": ["甲"]})
        assert key is None and acct["ok"] is False and "网关挂" in acct["reason"]


# ---------------------------------------------------------------------------
# ② 计划 prompt 命名示例占位符
# ---------------------------------------------------------------------------
class TestPlanPromptPlaceholder:
    def test_no_leakable_real_name_in_prompt(self):
        """P3 实测：示例名「柳青岩」原样变成了正文角色。"""
        from app.services import plan_crud
        p = plan_crud._plan_prompt(
            {"article_title": "第一篇", "volume_summary": "", "characters": [],
             "foreshadows": [], "prev_arc": ""},
            [], "主角去打一场大比", 3, None, mode="free")
        assert "柳青岩" not in p
        assert "示例人名甲" in p
        assert "禁止直接使用" in p

    def test_placeholder_is_not_a_plausible_name(self):
        from app.services import plan_crud
        p = plan_crud._plan_prompt({}, [], "x", 3, None, mode="free")
        assert "示例人名甲" in p and "待揭晓" in p, "占位符与既有例外后缀并存"


# ---------------------------------------------------------------------------
# ③ 计划行解析失败重试
# ---------------------------------------------------------------------------
class TestPlanRetry:
    def _ctx(self):
        return {"article_title": "第一篇", "volume_summary": "", "characters": [],
                "foreshadows": [], "prev_arc": ""}

    def test_retries_once_then_succeeds(self, monkeypatch):
        from app.services import plan_crud
        calls = {"n": 0}
        good = '{"lines":[{"no":1,"beat":"a","summary":"s"}]}'

        def _fake(*a, **kw):
            calls["n"] += 1
            return "garbage" if calls["n"] == 1 else good

        monkeypatch.setattr(plan_crud, "_ds_post", _fake)
        out = plan_crud._plan_llm_with_retry(self._ctx(), [], "hint", 3, None, "hint", key="k")
        assert calls["n"] == 2, "首次解析失败必须自动重试"
        assert out["lines"][0]["no"] == 1
        assert out.get("_attempts") == 2

    def test_gives_up_after_second_failure(self, monkeypatch):
        from app.services import plan_crud
        calls = {"n": 0}

        def _fake(*a, **kw):
            calls["n"] += 1
            return "garbage"

        monkeypatch.setattr(plan_crud, "_ds_post", _fake)
        with pytest.raises(RuntimeError, match="重试"):
            plan_crud._plan_llm_with_retry(self._ctx(), [], "hint", 3, None, "hint", key="k")
        assert calls["n"] == 2, "只重试 1 次，不许无限重试"

    def test_first_call_success_does_not_retry(self, monkeypatch):
        from app.services import plan_crud
        calls = {"n": 0}

        def _fake(*a, **kw):
            calls["n"] += 1
            return '{"lines":[{"no":1,"beat":"a","summary":"s"}]}'

        monkeypatch.setattr(plan_crud, "_ds_post", _fake)
        plan_crud._plan_llm_with_retry(self._ctx(), [], "hint", 3, None, "hint", key="k")
        assert calls["n"] == 1


def _fake_sse_resp(lines: list[str]):
    """造一个逐行吐 SSE 帧的假响应（**按 utf-8 编码**：bytes 字面量不能带非 ASCII）。"""

    class _Resp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for ln in lines:
                yield ln.encode("utf-8")

    return _Resp()


def _patch_opener(monkeypatch, resp=None, raises=None):
    """`_stream_text` 走 `build_opener(ProxyHandler({}))` 绕系统代理（网关在 localhost，
    走代理必坏），所以单测要 patch **build_opener** 而不是 urlopen。"""
    if raises is not None:
        def _boom(*a, **kw):
            raise raises
        monkeypatch.setattr(plot_import.urllib.request, "build_opener",
                            lambda *a, **kw: type("_O", (), {"open": staticmethod(_boom)}))
        return

    class _Opener:
        def open(self, req, timeout=None):
            return resp

    monkeypatch.setattr(plot_import.urllib.request, "build_opener",
                        lambda *a, **kw: _Opener())


# ---------------------------------------------------------------------------
# ④ 计划链流式
# ---------------------------------------------------------------------------
class TestPlanStreaming:
    def test_gateway_call_sets_stream_true(self, monkeypatch):
        """真跑 `_ds_post` → `_chat_post_stream` → `_stream_text`，只在 **HTTP 边界**
        （build_opener）拦，断言真正发到网关的 JSON 里带 stream:true。"""
        import json as _json
        sent = {}

        class _Opener:
            def open(self, req, timeout=None):
                sent["url"] = req.full_url
                sent["body"] = _json.loads(req.data.decode("utf-8"))
                return _fake_sse_resp(['data: {"choices":[{"delta":{"content":"全文"}}]}\n',
                                       "\n", "data: [DONE]\n"])

        monkeypatch.setattr(plot_import.urllib.request, "build_opener",
                            lambda *a, **kw: _Opener())
        monkeypatch.setattr(plot_import, "_chat_post",
                            lambda *a, **kw: pytest.fail("网关模式不许走非流式"))
        monkeypatch.setattr(plot_import, "GW_ACTIVE", True)
        out = plot_import._ds_post("k", "prompt", max_tokens=100)
        assert out == "全文"
        assert sent["body"]["stream"] is True
        assert sent["url"].startswith("http://127.0.0.1:9377"), "应走网关 base"

    def test_direct_connect_stays_non_streaming(self, monkeypatch):
        monkeypatch.setattr(plot_import, "GW_ACTIVE", False)
        monkeypatch.setattr(plot_import, "_chat_post_stream",
                            lambda *a, **kw: pytest.fail("直连模式不强制流式"))
        monkeypatch.setattr(plot_import, "_chat_post", lambda *a, **kw: "直连全文")
        assert plot_import._ds_post("k", "prompt", max_tokens=100) == "直连全文"

    def test_sf_post_also_streams_through_gateway(self, monkeypatch):
        """铁律是全局的：检索层新加的分类器走 sf_chat，网关模式下也必须流式。"""
        monkeypatch.setattr(plot_import, "GW_ACTIVE", True)
        monkeypatch.setattr(plot_import, "_chat_post_stream",
                            lambda url, key, body, **kw: "流式全文")
        monkeypatch.setattr(plot_import, "_chat_post",
                            lambda *a, **kw: pytest.fail("网关模式不许走非流式"))
        assert plot_import._sf_post("k", "p", max_tokens=50) == "流式全文"

    def test_sse_frames_are_assembled(self, monkeypatch):
        _patch_opener(monkeypatch, resp=_fake_sse_resp([
            'data: {"choices":[{"delta":{"content":"甲"}}]}\n', "\n",
            'data: {"choices":[{"delta":{"content":"乙"}}]}\n', "\n",
            "data: [DONE]\n"]))
        assert plot_import._stream_text("http://x/v1/chat/completions", "k",
                                        {"model": "m", "messages": []})[0] == "甲乙"

    def test_stream_tolerates_bad_frame(self, monkeypatch):
        """坏帧必须跳过但不能整条断掉（对齐 openai_compat.stream 的容错）。"""
        _patch_opener(monkeypatch, resp=_fake_sse_resp([
            "data: {坏 json\n\n",
            'data: {"choices":[{"delta":{"content":"好"}}]}\n', "\n",
            "data: [DONE]\n"]))
        assert plot_import._stream_text("http://x", "k", {})[0] == "好"

    def test_usage_captured_from_last_frame(self, monkeypatch):
        _patch_opener(monkeypatch, resp=_fake_sse_resp([
            'data: {"choices":[{"delta":{"content":"甲"}}]}\n', "\n",
            'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n', "\n",
            "data: [DONE]\n"]))
        text, usage = plot_import._stream_text("http://x", "k", {})
        assert text == "甲"
        assert usage["prompt_tokens"] == 7

    def test_gateway_disconnect_raises_loudly(self, monkeypatch):
        """铁律：网关断连要有明确报错，不能静默返回空串。"""
        _patch_opener(monkeypatch, raises=ConnectionRefusedError("gateway down"))
        with pytest.raises(RuntimeError, match="网关"):
            plot_import._stream_text("http://x", "k", {})

    def test_empty_stream_is_an_error_not_silent_empty(self, monkeypatch):
        _patch_opener(monkeypatch, resp=_fake_sse_resp(["data: [DONE]\n"]))
        with pytest.raises(RuntimeError):
            plot_import._stream_text("http://x", "k", {})

    def test_attempts_one_does_not_sleep(self, monkeypatch):
        """attempts=1 时失败要**立刻**上抛，不能再 sleep 退避。"""
        slept = []
        monkeypatch.setattr(plot_import.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(plot_import.RateLimiter, "acquire", lambda self, est: None)
        n = {"i": 0}

        def _fail(url, key, body, **kw):
            n["i"] += 1
            raise RuntimeError("网关流式调用失败: URLError: 拒绝连接")

        monkeypatch.setattr(plot_import, "_stream_text", _fail)
        with pytest.raises(RuntimeError, match="重试 1 次"):
            plot_import._chat_post_stream("http://x", "k", {}, attempts=1)
        assert n["i"] == 1, "只该调 1 次"
        assert slept == [], "attempts=1 不许再退避 sleep"

    def test_attempts_none_keeps_default_retry(self, monkeypatch):
        """不传 attempts 时保持原有 MAX_RETRY 行为（不越界改动其他调用方）。"""
        import app.services.plot_import as pi
        # RateLimiter.acquire 自己也会 sleep（限速），只量「退避 sleep」：把 acquire 桩掉
        slept = []
        monkeypatch.setattr(pi.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(pi.RateLimiter, "acquire", lambda self, est: None)
        n = {"i": 0}

        def _fail(url, key, body, **kw):
            n["i"] += 1
            raise RuntimeError("网关流式调用失败: URLError: x")

        monkeypatch.setattr(pi, "_stream_text", _fail)
        with pytest.raises(RuntimeError):
            pi._chat_post_stream("http://x", "k", {})
        assert n["i"] == pi.MAX_RETRY, "默认仍应重试满次数"
        assert len(slept) == pi.MAX_RETRY - 1, "只在非最后一次之间退避 sleep"

    def test_http_error_raises_with_status(self, monkeypatch):
        class _E(Exception):
            def __init__(self):
                super().__init__("boom")

            def read(self):
                return b"upstream exploded"

        import urllib.error as _ue
        err = _ue.HTTPError("http://x", 502, "Bad Gateway", {}, None)
        err.read = lambda: b"upstream exploded"      # noqa: E731
        _patch_opener(monkeypatch, raises=err)
        with pytest.raises(RuntimeError, match="502"):
            plot_import._stream_text("http://x", "k", {})


# ---------------------------------------------------------------------------
# 账目落地：key_match 进 raw_ai
# ---------------------------------------------------------------------------
class TestKeyMatchLedger:
    def test_hint_branch_keeps_arc_scale_filter(self, test_db, monkeypatch):
        """🔴 回归保护：口述分支**必须**保留 F9a 加的 `scale=arc`。

        我第一版把 `_templates_for_plan` 内联掉、直接调 `tpl_crud.search(db, query=hint)`，
        结果漏了 scale —— F8 的 265 张 `scale=character` 角色模板会混进篇规划参考池
        （角色模板没有可注入的情节节拍），且检索抛错会炸掉整个计划生成。
        """
        seen = {}

        def _fake_search(db, **kw):
            seen.update(kw)
            return {"mode": "vector", "queries": [], "items": [], "key_match": {}}

        monkeypatch.setattr(plan_crud.tpl_crud, "search", _fake_search)
        plan_crud._pick_templates(test_db, "来一场大比", {})
        assert seen.get("scale") == plan_crud.tpl_crud.SCALE_ARC, "口述分支丢了 scale=arc"
        assert seen.get("top_k") == 4

    def test_hint_branch_survives_search_exception(self, test_db, monkeypatch):
        """检索抛错必须退化成空模板（自由规划），不许炸掉计划生成。"""

        def _boom(db, **kw):
            raise RuntimeError("向量库炸了")

        monkeypatch.setattr(plan_crud.tpl_crud, "search", _boom)
        items, ids, mode, acct = plan_crud._pick_templates(test_db, "口述", {})
        assert (items, ids, mode) == ([], [], "hint")
        assert acct == {}

    def test_pick_templates_carries_key_match(self, test_db, monkeypatch):
        from app.services import plan_crud
        monkeypatch.setattr(plan_crud.tpl_crud, "search", lambda db, **kw: {
            "mode": "vector", "queries": [], "items": [{"id": "t1", "name": "擂台大比--甲"}],
            "key_match": {"class": "擂台大比", "sub_event": "甲", "ok": True,
                          "source": "llm"}})
        items, ids, mode, acct = plan_crud._pick_templates(
            test_db, "主角在擂台大比上打", {})
        assert mode == "hint" and ids == ["t1"]
        assert acct["key_match"]["class"] == "擂台大比"

    def test_no_key_match_leaves_account_empty(self, test_db, monkeypatch):
        from app.services import plan_crud
        monkeypatch.setattr(plan_crud.tpl_crud, "search", lambda db, **kw: {
            "mode": "vector", "queries": [], "items": [{"id": "t1", "name": "x"}]})
        _, _, _, acct = plan_crud._pick_templates(test_db, "口述", {})
        assert acct == {}