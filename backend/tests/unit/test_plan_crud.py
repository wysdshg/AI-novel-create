"""Phase 7.2 篇规划：生成 / 防幻觉闸门 / 行级修改 / 拍板 / 无 Key 报错。

全部 mock LLM（不出网）。防幻觉闸门是重点：召回角色必须真实存在于 characters 表。
"""
import json

import pytest

import app.core.database as dbmod
from app.models.orm import (ArticlePlanORM, ArticleORM, VolumeORM, ProjectORM,
                          CharacterORM, PlotTemplateORM)
from app.services import plan_crud


@pytest.fixture()
def proj(test_db):
    p = ProjectORM(id="p1", name="测试作品")
    v = VolumeORM(id="v1", project_id="p1", name="第一卷", summary="卷概要")
    a = ArticleORM(id="a1", project_id="p1", volume_id="v1", name="第一篇", summary="篇概要")
    test_db.add_all([p, v, a])
    test_db.commit()
    return {"pid": p.id, "aid": a.id}


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setattr(plan_crud, "ds_key", lambda db: "ds-key")


def _payload():
    return json.dumps({"lines": [
        {"no": 1, "beat": "退婚立局", "summary": "主角被退婚当众羞辱，立下三年之约",
         "new_chars": ["纳兰嫣然"], "recall_chars": [], "target_words": 3000,
         "hook": "神秘老者现身", "template_ref": "退婚逆袭·开局"},
        {"no": 2, "beat": "密室机缘", "summary": "主角在房中发现老者遗物，开启戒指",
         "new_chars": [], "recall_chars": ["药老"], "target_words": 3000,
         "hook": "戒指里有东西", "template_ref": "退婚逆袭·发展"},
        {"no": 3, "beat": "吸收功力", "summary": "老者传授功法，实力暴涨",
         "new_chars": [], "recall_chars": ["不存在的人"], "target_words": 3000,
         "hook": "约期将至", "template_ref": "退婚逆袭·高潮"},
    ], "notes": "整体说明"}, ensure_ascii=False)


class TestGeneratePlan:
    def test_generate_ok(self, test_db, proj, with_key, monkeypatch):
        monkeypatch.setattr(plan_crud, "_ds_post", lambda k, c, **kw: _payload())
        st = plan_crud.generate_plan(test_db, proj["pid"], proj["aid"], hint="退婚流开局")
        assert st["lines"] == 3
        row = test_db.query(ArticlePlanORM).one()
        assert row.status == "draft" and row.origin in ("template", "free")
        assert row.plan["lines"][0]["beat"] == "退婚立局"

    def test_recall_chars_validated(self, test_db, proj, with_key, monkeypatch):
        """防幻觉闸门：召回角色必须真实存在，查无此人剔除。"""
        test_db.add(CharacterORM(id="c1", project_id="p1", name="药老"))
        test_db.commit()
        monkeypatch.setattr(plan_crud, "_ds_post", lambda k, c, **kw: _payload())
        st = plan_crud.generate_plan(test_db, proj["pid"], proj["aid"])
        assert st["unknown_chars"] == ["不存在的人"]
        row = test_db.query(ArticlePlanORM).one()
        lines = {l["no"]: l for l in row.plan["lines"]}
        assert lines[2]["recall_chars"] == ["药老"]           # 真实存在 → 保留
        assert lines[3]["recall_chars"] == []                 # 查无此人 → 剔除
        assert lines[1]["new_chars"] == ["纳兰嫣然"]          # 新角色不校验（走待确认实体）

    def test_regen_overwrites_draft(self, test_db, proj, with_key, monkeypatch):
        monkeypatch.setattr(plan_crud, "_ds_post", lambda k, c, **kw: _payload())
        plan_crud.generate_plan(test_db, proj["pid"], proj["aid"])
        plan_crud.generate_plan(test_db, proj["pid"], proj["aid"])
        assert test_db.query(ArticlePlanORM).count() == 1     # 同一篇只有一条当前计划

    def test_no_key_raises(self, test_db, proj, monkeypatch):
        monkeypatch.setattr(plan_crud, "ds_key", lambda db: None)
        try:
            plan_crud.generate_plan(test_db, proj["pid"], proj["aid"])
            raised = False
        except RuntimeError as e:
            raised = "Key" in str(e)
        assert raised

    def test_bad_output_raises(self, test_db, proj, with_key, monkeypatch):
        monkeypatch.setattr(plan_crud, "_ds_post", lambda k, c, **kw: "这不是 JSON")
        try:
            plan_crud.generate_plan(test_db, proj["pid"], proj["aid"])
            raised = False
        except RuntimeError as e:
            raised = "计划生成失败" in str(e)
        assert raised


class TestPlanWorkflow:
    def _gen(self, test_db, proj, with_key, monkeypatch):
        monkeypatch.setattr(plan_crud, "_ds_post", lambda k, c, **kw: _payload())
        plan_crud.generate_plan(test_db, proj["pid"], proj["aid"])

    def test_refine_line_only_changes_target(self, test_db, proj, with_key, monkeypatch):
        self._gen(test_db, proj, with_key, monkeypatch)
        monkeypatch.setattr(plan_crud, "_ds_post", lambda k, c, **kw: json.dumps({
            "no": 2, "beat": "密室机缘（改）", "summary": "改后的概要，信息量足够长一些。",
            "new_chars": [], "recall_chars": ["药老"], "target_words": 3500,
            "hook": "新钩子", "template_ref": "退婚逆袭·发展"}, ensure_ascii=False))
        r = plan_crud.refine_line(test_db, proj["pid"], proj["aid"], line_no=2,
                                  instruction="节奏放缓，加重悬念")
        assert r["line"]["beat"] == "密室机缘（改）"
        plan = plan_crud.get_plan(test_db, proj["pid"], proj["aid"])
        by_no = {l["no"]: l for l in plan["lines"]}
        assert by_no[1]["summary"].startswith("主角被退婚")   # 其他行不动
        assert by_no[2]["target_words"] == 3500
        assert by_no[3]["beat"] == "吸收功力"

    def test_refine_line_missing_no_raises(self, test_db, proj, with_key, monkeypatch):
        self._gen(test_db, proj, with_key, monkeypatch)
        try:
            plan_crud.refine_line(test_db, proj["pid"], proj["aid"], line_no=99,
                                  instruction="改")
            raised = False
        except RuntimeError:
            raised = True
        assert raised

    def test_confirm_then_get(self, test_db, proj, with_key, monkeypatch):
        self._gen(test_db, proj, with_key, monkeypatch)
        r = plan_crud.confirm_plan(test_db, proj["pid"], proj["aid"])
        assert r["status"] == "confirmed"
        plan = plan_crud.get_plan(test_db, proj["pid"], proj["aid"])
        assert plan["status"] == "confirmed"

    def test_save_lines_edit(self, test_db, proj, with_key, monkeypatch):
        self._gen(test_db, proj, with_key, monkeypatch)
        plan = plan_crud.get_plan(test_db, proj["pid"], proj["aid"])
        lines = plan["lines"]
        lines[0]["summary"] = "作者手改后的概要，长度足够通过校验。"
        plan_crud.save_lines(test_db, proj["pid"], proj["aid"], lines=lines, notes="作者批注")
        got = plan_crud.get_plan(test_db, proj["pid"], proj["aid"])
        assert got["lines"][0]["summary"].startswith("作者手改")
        assert got["notes"] == "作者批注"

    def test_no_plan_returns_none(self, test_db, proj):
        assert plan_crud.get_plan(test_db, proj["pid"], proj["aid"]) is None


class TestTemplateSearchScaleArc:
    """DEV-F9a（2026-10-03）：篇规划只吃情节骨架（scale=arc）。

    背景：F8 换血把 265 条 scale='character' 的角色模板放进**同一张** plot_templates 表
    （同表靠 scale 区分是设计，不是存错）。`_templates_for_plan` 不过滤 scale 时角色模板
    会混进篇规划的参考池——它们没有可注入的情节节拍，注入不出东西。角色模板归建卡链。
    """

    def test_search_called_with_scale_arc(self, test_db, monkeypatch):
        """钉住调用契约：search 必须带 scale='arc'（漏了角色模板就回来了）。"""
        seen = {}

        def _fake_search(db, **kw):
            seen.update(kw)
            return {"mode": "stub", "queries": [], "items": []}

        monkeypatch.setattr(plan_crud.tpl_crud, "search", _fake_search)
        plan_crud._templates_for_plan(test_db, "主角进入秘境夺宝")
        assert seen["scale"] == plan_crud.tpl_crud.SCALE_ARC
        assert seen["scale"] == "arc"

    def test_character_templates_excluded(self, test_db, monkeypatch):
        """同名同口述的 arc 骨架与 character 角色模板，只有骨架进得了结果。

        强制走关键词兜底路径（`vector_index.enabled` → False），让断言不依赖
        当前环境装没装 sqlite-vec —— scale 过滤在两条路径上都生效，但这里只钉兜底这条。
        """
        monkeypatch.setattr(plan_crud.tpl_crud.vector_index, "enabled", lambda db: False)
        test_db.add_all([
            PlotTemplateORM(id="tpl_arc", name="秘境夺宝·骨架", scale="arc", status="active",
                            logline="主角进入秘境夺宝", structure={"phases": []}),
            PlotTemplateORM(id="tpl_char", name="秘境夺宝·角色", scale="character", status="active",
                            logline="主角进入秘境夺宝", structure={"cast": []}),
        ])
        test_db.commit()

        items, ids = plan_crud._templates_for_plan(test_db, "秘境夺宝")

        assert "tpl_arc" in ids, "同名的情节骨架应被召回"
        assert "tpl_char" not in ids, "角色模板不得混进篇规划参考池"
        assert all(i["scale"] == "arc" for i in items)


def _arc_tpl(tid, name, status="active", scale="arc"):
    return PlotTemplateORM(id=tid, name=name, scale=scale, status=status,
                           logline=f"{name}的梗概", structure={"phases": []})


class TestFallbackQueries:
    """DEV-F9b 兜底查询串构造（无口述时靠卷概要 / 篇名 / 活跃角色三路）。"""

    def test_uses_volume_summary_title_and_chars(self):
        main, extra = plan_crud._fallback_queries({
            "volume_summary": "玄" * 300,
            "article_title": "秘境夺宝",
            "characters": [f"角色{i}" for i in range(12)],
        })
        assert main == "玄" * 200, "卷概要必须截 200 字"
        assert extra[0] == "秘境夺宝"
        assert extra[1] == "、".join(f"角色{i}" for i in range(8)), "活跃角色取前 8 名"
        assert len(extra) == 2

    def test_title_fallback_when_no_summary(self):
        main, extra = plan_crud._fallback_queries({
            "volume_summary": "", "article_title": "第一篇", "characters": ["甲", "乙"],
        })
        assert main == "第一篇", "无卷概要时主查询回退篇名"
        assert extra == ["甲、乙"], "篇名回退后不再重复占一路"

    def test_empty_ctx(self):
        assert plan_crud._fallback_queries({}) == ("", [])


class TestFourBranchPick:
    """DEV-F9b 四分支：作者指定 > 口述检索 > 无口述兜底 > 随机灵感（+ force_free 全跳过）。"""

    def _spy_search(self, monkeypatch, items=None, raises=False):
        calls = []

        def _fake(db, **kw):
            calls.append(kw)
            if raises:
                raise RuntimeError("检索挂了")
            return {"mode": "stub", "queries": [], "items": list(items or [])}

        monkeypatch.setattr(plan_crud.tpl_crud, "search", _fake)
        return calls

    def test_author_pick_skips_search(self, test_db, monkeypatch):
        test_db.add(_arc_tpl("t1", "秘境夺宝"))
        test_db.commit()
        calls = self._spy_search(monkeypatch, items=[{"id": "t9", "name": "别的骨架"}])
        items, ids, mode, fb = plan_crud._pick_templates(test_db, "随便一句口述", {},
                                                         template_id="t1")
        assert mode == "author"
        assert ids == ["t1"], "锁定的必须是作者选的那一条"
        assert calls == [], "显式选模板要跳过检索，检索结果不得覆盖作者选择"
        assert fb == {}

    def test_unusable_template_id_falls_back_to_search(self, test_db, monkeypatch):
        """指定模板不合规（角色模板 / 已归档）→ 按无选处理，不许把不可用模板注入规划。"""
        test_db.add(_arc_tpl("tc", "反派师父", scale="character"))
        test_db.add(_arc_tpl("ta", "闭关突破", status="archived"))
        test_db.commit()
        calls = self._spy_search(monkeypatch, items=[{"id": "t9", "name": "秘境夺宝"}])
        for bad in ("tc", "ta", "不存在的id"):
            items, ids, mode, _ = plan_crud._pick_templates(test_db, "主角夺宝", {},
                                                             template_id=bad)
            assert mode == "hint", f"{bad} 应回落到口述检索"
            assert ids == ["t9"]
        assert len(calls) == 3, "三种不可用取值都要回到检索"

    def test_no_hint_uses_multi_route_search(self, test_db, monkeypatch):
        calls = self._spy_search(monkeypatch, items=[{"id": "t1", "name": "秘境夺宝"}])
        ctx = {"volume_summary": "主角一行进入秘境争夺异宝", "article_title": "第一篇",
               "characters": ["甲", "乙"]}
        items, ids, mode, fb = plan_crud._pick_templates(test_db, "", ctx)
        assert mode == "fallback" and ids == ["t1"]
        assert calls[0]["query"] == "主角一行进入秘境争夺异宝"
        assert calls[0]["queries"] == ["第一篇", "甲、乙"]
        assert calls[0]["scale"] == plan_crud.tpl_crud.SCALE_ARC
        assert calls[0]["top_k"] == 4
        assert fb["queries"] == ["主角一行进入秘境争夺异宝", "第一篇", "甲、乙"]
        assert fb["random_ids"] == []

    def test_empty_fallback_randomizes(self, test_db, monkeypatch):
        test_db.add_all([_arc_tpl(f"r{i}", f"骨架{i}") for i in range(8)])
        test_db.add(_arc_tpl("c1", "角色模板", scale="character"))
        test_db.add(_arc_tpl("a1", "归档骨架", status="archived"))
        test_db.commit()
        self._spy_search(monkeypatch, items=[])
        items, ids, mode, fb = plan_crud._pick_templates(test_db, "", {})
        assert mode == "random"
        assert len(ids) in (4, 5), "随机抽 4~5 条"
        assert all(i.startswith("r") for i in ids), "只抽现役 arc 骨架"
        assert fb["random_ids"] == ids

    def test_search_crash_randomizes(self, test_db, monkeypatch):
        """检索异常不得阻断规划（网关挂掉也要能出计划）。"""
        test_db.add_all([_arc_tpl(f"r{i}", f"骨架{i}") for i in range(6)])
        test_db.commit()
        self._spy_search(monkeypatch, raises=True)
        items, ids, mode, fb = plan_crud._pick_templates(test_db, "", {})
        assert mode == "random" and len(ids) in (4, 5)

    def test_random_pool_empty_stays_free(self, test_db, monkeypatch):
        self._spy_search(monkeypatch, items=[])
        items, ids, mode, fb = plan_crud._pick_templates(test_db, "", {})
        assert (items, ids, mode) == ([], [], "free")

    def test_force_free_skips_all(self, test_db, monkeypatch):
        test_db.add(_arc_tpl("t1", "秘境夺宝"))
        test_db.commit()
        calls = self._spy_search(monkeypatch, items=[{"id": "t1", "name": "秘境夺宝"}])
        items, ids, mode, fb = plan_crud._pick_templates(
            test_db, "口述", {}, template_id="t1", force_free=True)
        assert (items, ids, mode, fb) == ([], [], "free", {})
        assert calls == [], "force_free 是全部跳过（含作者指定）"


class TestFourBranchGenerateAccounting:
    """四分支落库侧的可观测性：prompt 标注 + raw_ai 兜底账。"""

    def _capture(self, monkeypatch):
        prompts = []

        def _fake_post(key, content, **kw):
            prompts.append(content)
            return _payload()

        monkeypatch.setattr(plan_crud, "_ds_post", _fake_post)
        return prompts

    def test_author_pick_annotated_in_prompt(self, test_db, proj, with_key, monkeypatch):
        test_db.add(_arc_tpl("t1", "秘境夺宝"))
        test_db.commit()
        prompts = self._capture(monkeypatch)
        plan_crud.generate_plan(test_db, proj["pid"], proj["aid"], template_id="t1")
        row = test_db.query(ArticlePlanORM).one()
        assert row.template_ids == ["t1"] and row.origin == "template"
        assert "作者指定的参考模板" in prompts[0]
        assert "《秘境夺宝》（作者指定）" in prompts[0]

    def test_random_fallback_recorded(self, test_db, proj, with_key, monkeypatch):
        """兜底查询串 + 随机名单 + 模型实际借鉴的那条骨架，三样都进 raw_ai 账。

        抽样本身的数量/池子范围在 `TestFourBranchPick` 里钉；这里把 `_random_skeletons`
        钉成固定名单，让 picked 归因可断言（随机不可复现是定稿刻意保留的）。
        """
        pool = [{"id": "t_pick", "name": "退婚逆袭", "structure": {"phases": []}},
                {"id": "r0", "name": "骨架0", "structure": {"phases": []}}]
        monkeypatch.setattr(plan_crud.tpl_crud, "search",
                            lambda db, **kw: {"mode": "stub", "queries": [], "items": []})
        monkeypatch.setattr(plan_crud, "_random_skeletons", lambda db: list(pool))
        prompts = self._capture(monkeypatch)   # _payload 的 template_ref = 退婚逆袭·开局
        plan_crud.generate_plan(test_db, proj["pid"], proj["aid"])
        row = test_db.query(ArticlePlanORM).one()
        fb = row.raw_ai["fallback"]
        assert fb["queries"] == ["卷概要", "第一篇"], "兜底实际用的查询串要留账"
        assert fb["random_ids"] == ["t_pick", "r0"]
        assert row.origin == "template"
        assert fb["picked"] == "退婚逆袭", "模型实际借鉴的那条骨架要记名"
        assert "随机候选骨架" in prompts[0] and "最贴合的 1 个" in prompts[0]

    def test_hint_branch_unchanged(self, test_db, proj, with_key, monkeypatch):
        """回归保护：有口述时行为与现状一致，不写兜底账。"""
        monkeypatch.setattr(plan_crud.tpl_crud, "search",
                            lambda db, **kw: {"mode": "stub", "queries": [],
                                              "items": [{"id": "t1", "name": "秘境夺宝"}]})
        self._capture(monkeypatch)
        plan_crud.generate_plan(test_db, proj["pid"], proj["aid"], hint="主角夺宝")
        row = test_db.query(ArticlePlanORM).one()
        assert row.template_ids == ["t1"]
        assert "fallback" not in row.raw_ai


class TestRefineHint:
    """8B 口述增强/帮写：只回文本，不落库、不触发生成（定稿取舍：产物必须可见可改）。"""

    def _spy(self, monkeypatch, reply="「主角在秘境夺宝，与宗门弟子结死仇」"):
        seen = {}

        def _fake(db, content, **kw):
            seen["prompt"] = content
            seen["kw"] = kw
            return reply

        monkeypatch.setattr(plan_crud, "sf_chat", _fake)
        return seen

    def test_enhance_hands_text_back_unwrapped(self, test_db, proj, monkeypatch):
        test_db.add(_arc_tpl("t1", "秘境夺宝"))
        test_db.commit()
        seen = self._spy(monkeypatch)
        r = plan_crud.refine_hint(test_db, proj["pid"], proj["aid"],
                                  template_id="t1", hint="主角夺宝", mode="enhance")
        assert r["hint"] == "主角在秘境夺宝，与宗门弟子结死仇", "首尾引号要剥掉再填框"
        assert r["template_id"] == "t1"
        assert test_db.query(ArticlePlanORM).count() == 0, "增强口述不得写库、不得生成计划"
        assert seen["kw"]["max_tokens"] <= 300 and seen["kw"]["temperature"] == 0.4
        assert "扩写" in seen["prompt"] and "《秘境夺宝》" in seen["prompt"]
        assert "不要" not in seen["prompt"], "提示词纪律 04-B6：少负面措辞"

    def test_draft_mode_without_template(self, test_db, proj, monkeypatch):
        seen = self._spy(monkeypatch)
        r = plan_crud.refine_hint(test_db, proj["pid"], proj["aid"], mode="draft")
        assert r["hint"] and r["template_id"] is None
        assert "还没写口述" in seen["prompt"]
        assert "未指定" in seen["prompt"]

    def test_empty_reply_raises(self, test_db, proj, monkeypatch):
        self._spy(monkeypatch, reply="   ")
        with pytest.raises(RuntimeError):
            plan_crud.refine_hint(test_db, proj["pid"], proj["aid"])

