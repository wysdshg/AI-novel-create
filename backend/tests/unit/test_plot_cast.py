"""Phase 7.3 ① cast 单测（全 mock / 离线，不碰网络与生产库）。

背景：模板凝练除结构外还要产出 **cast（人格化角色槽位）**，供篇规划做向量选角。
定稿 B 方案 —— cast 记「功能槽位」+ `srcs` 溯源到各书代称（与 `variants[{src,how}]` 同构）。

本文件钉住四条容易回归的行为：
1. **代称字面命中必须有数字边界**（`友·配角1` 不得命中 `友·配角10`）；
2. **`role_desc` 必须先匿名化**（它是在原文上抽取的，实测直接含源书专名 → 会打破反抄袭门槛）；
3. **LLM 编造的 `srcs` 必须被丢弃**（全非法则整个槽位丢弃，宁可少也不放幽灵槽位进选角）；
4. **cast 要真的落进 `structure`**（否则 7.3 的槽位向量化会找不到输入）。
"""
import json

import pytest

from app.models.orm import BookAliasORM, ChapterSummaryORM
from app.services import plot_distill as pd
from app.services import plot_template_crud as tpl_crud


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _alias(book, original, alias, kind, desc, rel="ally", first=None):
    return BookAliasORM(id=f"{book}|{alias}", book_name=book, original=original,
                        alias=alias, kind=kind, relation=rel,
                        role_desc=desc, first_chapter=first)


@pytest.fixture()
def seeded(test_db):
    """一本「斗破」的映射表：覆盖 protagonist / role / sect / place 四种 kind。"""
    test_db.add_all([
        _alias("斗破", "萧炎", "主角", "protagonist", "萧炎", first=1),
        _alias("斗破", "药老", "友·配角4", "role", "戒指中的神秘老者，称主角为萧炎哥哥", first=3),
        _alias("斗破", "云韵", "敌·配角2", "role", "云岚宗宗主，前来退婚", rel="enemy", first=5),
        # 编号上双：用来验证 "友·配角1" 不得误命中它
        _alias("斗破", "路人甲", "友·配角10", "role", "与主角关系一般", first=30),
        _alias("斗破", "云岚宗", "敌·宗门1", "sect", "云岚宗", rel="enemy"),
        _alias("斗破", "某秘境", "地点1", "place", "不该进 cast 的 kind"),
        _alias("斗破", "丹药", "友·物品1", "item", "普通名词噪声（item 抽取过宽）"),
    ])
    test_db.commit()
    return test_db


def _arc(**kw):
    base = {"book": "斗破", "arc_no": 1, "name": "退婚逆袭",
            "summary": "敌·配角2 前来退婚，主角受辱",
            "beats": [{"label": "开局委托", "summary": "当众退婚", "chapters": [1, 2]}]}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. 代称字面命中的数字边界
# ---------------------------------------------------------------------------
def test_alias_in_text_respects_digit_boundary():
    assert pd._alias_in_text("友·配角1", "友·配角10 出场了") is False
    assert pd._alias_in_text("友·配角1", "友·配角1 出场了") is True
    assert pd._alias_in_text("友·配角1", "友·配角1，还有友·配角10") is True
    assert pd._alias_in_text("敌·配角2", "友·配角10 出场了") is False
    assert pd._alias_in_text("", "任何文本") is False


# ---------------------------------------------------------------------------
# 2. 即时匿名化（公开入口，长名优先）
# ---------------------------------------------------------------------------
def test_anonymize_text_longest_first():
    from app.services.anonymizer import anonymize_text
    mapping = [{"original": "云岚", "alias": "短名"}, {"original": "云岚宗", "alias": "敌·宗门1"}]
    # 长名优先 → "云岚宗" 先被替换，不会被 "云岚" 截成 "短名宗"
    assert anonymize_text("云岚宗宗主", mapping) == "敌·宗门1宗主"
    assert anonymize_text("", mapping) == ""
    assert anonymize_text("无关文本", []) == "无关文本"


def test_book_cast_filters_kinds_and_anonymizes_desc(seeded):
    bc = pd._book_cast(seeded, "斗破")
    by_alias = {s["alias"]: s["desc"] for s in bc["slots"]}

    assert "地点1" not in by_alias            # place 不是角色槽位
    assert "敌·宗门1" in by_alias              # sect 是（如"敌对宗门长老"）
    assert "萧炎" not in by_alias["友·配角4"]   # 源书专名已被替换
    assert "主角" in by_alias["友·配角4"]
    assert "云岚宗" not in by_alias["敌·配角2"]


def test_book_cast_mapping_covers_all_kinds(seeded):
    """回归：`mapping` 必须是**全 kind**。

    首版只把 `CAST_KINDS` 的映射拿去替换 → item/place/realm 类专名在 `role_desc` 里全部漏网
    （实测 18 处：清风观 / 紫晶源 / 斗之气 / 魔兽山脉 / 炼药师公会），照样带进模板 cast。
    `mapping_strong` 再剔除 `item`（该类把「丹药」「长剑」这类普通名词也当专名，替换反而污染语义）。
    """
    bc = pd._book_cast(seeded, "斗破")
    assert "某秘境" in {m["original"] for m in bc["mapping"]}        # place 在完整映射里
    assert "丹药" in {m["original"] for m in bc["mapping"]}          # item 也在
    strong = {m["original"] for m in bc["mapping_strong"]}
    assert "某秘境" in strong        # place 要替换（书级专名）
    assert "丹药" not in strong      # item 不替换（普通名词噪声）


def test_normalize_cast_anonymizes_llm_written_desc(seeded):
    """LLM 自己写的 `desc` 会带出源书专名（实测 18 处）→ 必须确定性兜底，不能只靠 prompt。"""
    raw = [{"slot": "宗门长老", "desc": "云岚宗宗主，与药老相熟",
            "srcs": [{"book": "斗破", "alias": "友·配角4"}]}]
    out = pd._normalize_cast(raw, [{"book": "斗破"}], seeded)
    assert len(out) == 1
    d = out[0]["desc"]
    assert "云岚宗" not in d and "药老" not in d
    assert "敌·宗门1" in d


# ---------------------------------------------------------------------------
# 3. 弧级候选槽位预筛
# ---------------------------------------------------------------------------
def test_pick_cast_keeps_protagonist_and_only_hits(seeded):
    bc = pd._book_cast(seeded, "斗破")
    got = {s["alias"] for s in pd._pick_cast_for_arc(_arc(), bc)}
    assert "主角" in got            # 主角恒在（每个套路都有主角）
    assert "敌·配角2" in got         # 弧文本里出现 → 命中
    assert "友·配角10" not in got    # 没出现，且不得被 "友·配角1" 前缀误伤
    assert "地点1" not in got


def test_pick_cast_falls_back_when_nothing_hits(seeded):
    bc = pd._book_cast(seeded, "斗破")
    arc = _arc(summary="一段没有任何代称的概括", beats=[])
    got = pd._pick_cast_for_arc(arc, bc)
    assert got, "命不中时必须回退到整本书槽位，不能返回空（否则凝练拿不到 cast）"


# ---------------------------------------------------------------------------
# 4. LLM 产出的 cast 校验归一
# ---------------------------------------------------------------------------
def test_normalize_cast_keeps_slot_when_srcs_invalid(seeded):
    """2026-09-18 行为变更：srcs 全非法时**保留槽位、srcs 置空**（不再整槽丢弃）。

    原因：单弧凝练里模型常编造代称，旧规则会把 cast 清空（实测太荒 68 条模板 cast 全 0）；
    槽位价值=功能位+位阶+12 维性格，与能否溯源无关。
    """
    raw = [
        {"slot": "引路人师长", "desc": "主角的导师", "mode": "助力", "beats": ["异象触发"],
         "srcs": [{"book": "斗破", "alias": "友·配角4"},
                  {"book": "不存在的书", "alias": "友·配角1"}]},
        {"slot": "幽灵槽位", "srcs": [{"book": "斗破", "alias": "根本没这个人"}]},  # 全非法 → 保留、srcs 空
        {"slot": "引路人师长", "srcs": [{"book": "斗破", "alias": "主角"}]},        # 重名 → 丢弃
    ]
    out = pd._normalize_cast(raw, [{"book": "斗破"}], seeded)
    assert len(out) == 2
    assert out[0]["slot"] == "引路人师长"
    assert out[0]["srcs"] == [{"book": "斗破", "alias": "友·配角4"}]
    assert out[1]["slot"] == "幽灵槽位" and out[1]["srcs"] == []


def test_normalize_cast_caps_and_tolerates_garbage(seeded):
    arcs = [{"book": "斗破"}]
    many = [{"slot": f"槽位{i}", "srcs": [{"book": "斗破", "alias": "主角"}]} for i in range(10)]
    assert len(pd._normalize_cast(many, arcs, seeded)) == pd.CAST_MAX
    # LLM 返回非 list / 空 / 缺字段都不能炸
    assert pd._normalize_cast(None, arcs, seeded) == []
    assert pd._normalize_cast("不是列表", arcs, seeded) == []
    assert pd._normalize_cast([{"desc": "没有 slot"}], arcs, seeded) == []


# ---------------------------------------------------------------------------
# 5. 凝练 prompt 要带上 cast 要求与候选块
# ---------------------------------------------------------------------------
def test_distill_prompt_includes_cast(seeded):
    bc = pd._book_cast(seeded, "斗破")
    arc = _arc()
    arc["cast_candidates"] = pd._pick_cast_for_arc(arc, bc)
    p = pd._distill_prompt([arc])
    assert "功能槽位" in p                     # 要求 7 存在
    assert '"cast"' in p                      # 输出 schema 含 cast
    assert "srcs" in p
    assert "合并成一条" in p                   # 跨书同一功能位必须合并
    assert "敌·配角2" in p                     # 候选槽位块真的拼进去了


# ---------------------------------------------------------------------------
# 6. 端到端：collect_arcs 带出 cast → distill_template 写进 structure
# ---------------------------------------------------------------------------
def test_collect_arcs_attaches_cast_candidates(seeded):
    seeded.add(ChapterSummaryORM(
        id="c1", book_name="斗破", chapter_no=1, summary="主角被羞辱",
        segment_no=1, segment_summary="敌·配角2 前来退婚",
        plot_label="退婚", arc_no=1, arc_name="退婚逆袭", arc_summary="主角立志"))
    seeded.commit()

    arcs = pd.collect_arcs(seeded, ["斗破"])
    assert len(arcs) == 1
    got = {s["alias"] for s in arcs[0]["cast_candidates"]}
    assert "主角" in got and "敌·配角2" in got
    assert "友·配角10" not in got


def test_distill_template_writes_cast_into_structure(seeded, monkeypatch):
    fake_payload = {
        "name": "退婚逆袭", "logline": "被退婚后的打脸升级", "genre_tags": ["玄幻"],
        "structure": {"phases": [{"phase": "开局", "beats": [{
            "beat": "当众退婚", "variants": [{"src": "斗破", "how": "当众撕毁婚约"}]}]}]},
        "cast": [{"slot": "退婚的未婚妻", "desc": "当众退婚的女方，背后有强宗撑腰",
                  "mode": "阻碍", "beats": ["当众退婚"],
                  "srcs": [{"book": "斗破", "alias": "敌·配角2"},
                           {"book": "斗破", "alias": "编的吧"}]}],
        "pitfalls": ["缺失前期伏笔"], "rhythm": "2-3-3",
    }
    monkeypatch.setattr(pd, "_ds_post", lambda *a, **k: json.dumps(fake_payload, ensure_ascii=False))
    monkeypatch.setattr(pd, "ds_key", lambda db: "fake-key")
    # 向量化要联网（无 key 会走异常分支）→ 本测试只关心落库结构，直接短路
    monkeypatch.setattr(tpl_crud, "index_template", lambda db, t: 0)

    o, _raw = pd.distill_template(seeded, [_arc()])
    assert o is not None
    assert o.structure["cast"][0]["slot"] == "退婚的未婚妻"
    # 编造的 src 已被剔除，合法的保留
    assert o.structure["cast"][0]["srcs"] == [{"book": "斗破", "alias": "敌·配角2"}]
    assert o.source_stats["cast_slots"] == 1
    # 结构本体不受影响（cast 是旁挂键，不能破坏 phases）
    assert o.structure["phases"][0]["phase"] == "开局"


def test_distill_template_keeps_cast_even_if_srcs_invalid(seeded, monkeypatch):
    """2026-09-18 行为变更：槽位保留（srcs 置空），故 cast 键保留且 cast_slots=1。"""
    payload = {"name": "无角色模板", "structure": {"phases": []},
               "cast": [{"slot": "幽灵", "srcs": [{"book": "斗破", "alias": "不存在"}]}]}
    monkeypatch.setattr(pd, "_ds_post", lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(pd, "ds_key", lambda db: "fake-key")
    monkeypatch.setattr(tpl_crud, "index_template", lambda db, t: 0)

    o, _raw = pd.distill_template(seeded, [_arc()])
    assert o is not None
    assert len((o.structure or {}).get("cast") or []) == 1
    assert o.source_stats["cast_slots"] == 1
