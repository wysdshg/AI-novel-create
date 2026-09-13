"""Phase 7.3.5 角色连续性单测（全 mock / 离线，不碰网络与生产库）。

钉住四块容易回归的行为（docs/03 §7.3.5 B 档）：
1. **新角色引入单**（plan_chars）：new_chars 落地 pending、限额 ≤3、
   first_appearance 自动填、重生成只清 pending（confirmed/dismissed 不蒸发）、
   确认建卡回链 character_id、同名不重复建卡；
2. **篇间交接差集**：上次写作位置最后 3 章的角色并集 − 本篇引用 = 警告名单
   （警告非报错；本篇自己的记忆不算"上一篇"；无记忆时静默为空）；
3. **回归理由材料包**（确定性预取）：未回收伏笔优先（收伏笔优于凭空编）→
   缺席期世界线事件（实体命中）→ fallback 如实标注；resolved 伏笔不算；
4. **级联删除**：删篇/卷/作品必须清掉 plan_chars。
"""
import json
import uuid

import pytest

from app.models.orm import (
    ArticleORM, ArticlePlanORM, ChapterMemoryORM, CharacterORM,
    ForeshadowORM, PlannedCharORM, ProjectORM, VolumeORM,
)
from app.services import casting_crud as cc
from app.services import plan_crud as pc


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _mk_project(db):
    p = ProjectORM(id="p1", name="连续性测试")
    v = VolumeORM(id="v1", project_id="p1", name="卷一")
    a = ArticleORM(id="a1", project_id="p1", volume_id="v1", name="篇一")
    a2 = ArticleORM(id="a2", project_id="p1", volume_id="v1", name="篇二")
    db.add_all([p, v, a, a2])
    db.commit()
    return p.id


def _mk_plan(db, pid, aid="a1") -> ArticlePlanORM:
    o = ArticlePlanORM(
        id=uuid.uuid4().hex, project_id=pid, article_id=aid,
        plan={"lines": [], "notes": ""}, origin="free", status="draft",
    )
    db.add(o)
    db.commit()
    return o


def _mk_char(db, pid, name, *, status="alive", last_seen=None, count=0):
    o = CharacterORM(id=uuid.uuid4().hex, project_id=pid, name=name,
                     role_type="配角", status=status,
                     last_seen_chapter=last_seen, appearance_count=count)
    db.add(o)
    db.commit()
    return o


def _mk_memory(db, pid, chapter_no, *, characters=None, article_id=None,
               summary=""):
    o = ChapterMemoryORM(
        id=uuid.uuid4().hex, project_id=pid, chapter_id=uuid.uuid4().hex,
        article_id=article_id, chapter_no=chapter_no,
        title=f"第{chapter_no}章", summary=summary,
        characters=characters or [], plot_points=[], new_entities=[],
    )
    db.add(o)
    db.commit()
    return o


def _mk_foreshadow(db, pid, desc, *, buried=None, status="pending",
                   related=None, enabled=True):
    o = ForeshadowORM(id=uuid.uuid4().hex, project_id=pid, description=desc,
                      buried_chapter=buried, status=status,
                      related_ids=related or [], enabled=enabled)
    db.add(o)
    db.commit()
    return o


def _lines(*new_chars_per_line):
    """[{no: i, new_chars: [...]}] 的最小计划行。"""
    return [{"no": i + 1, "beat": "b", "summary": "s",
             "new_chars": list(names), "recall_chars": [],
             "target_words": 100, "hook": "h", "template_ref": ""}
            for i, names in enumerate(new_chars_per_line)]


@pytest.fixture()
def proj(test_db):
    pid = _mk_project(test_db)
    yield test_db, pid


# ---------------------------------------------------------------------------
# 1. 新角色引入单
# ---------------------------------------------------------------------------
class TestPlannedChars:
    def test_landed_with_first_appearance(self, proj):
        """new_chars 落成 pending 单，first_appearance = 首次出现的行号，同名去重。"""
        db, pid = proj
        plan = _mk_plan(db, pid)
        r = pc._run_planned_chars(db, plan, _lines(["林小天", "赵铁柱"], ["林小天"]))
        assert r["planned_chars_reason"] is None
        rows = {o["name"]: o for o in r["planned_chars"]}
        assert set(rows) == {"林小天", "赵铁柱"}, "同名跨行必须去重"
        assert rows["林小天"]["first_appearance"] == 1
        assert rows["赵铁柱"]["first_appearance"] == 1
        assert all(o["status"] == "pending" for o in rows.values())
        # 落库确认
        assert db.query(PlannedCharORM).filter_by(plan_id=plan.id).count() == 2

    def test_limit_three_with_drop_report(self, proj):
        """限额 ≤3：按首次出现顺序保留，超出剔除并告警（不硬塞）。"""
        db, pid = proj
        plan = _mk_plan(db, pid)
        r = pc._run_planned_chars(db, plan, _lines(
            ["甲", "乙"], ["丙"], ["丁"], ["戊"]))
        names = [o["name"] for o in r["planned_chars"]]
        assert names == ["甲", "乙", "丙"], "只留首次出现的前 3 个"
        assert r["new_chars_dropped"] == ["丁", "戊"]

    def test_regen_keeps_confirmed_and_dismissed(self, proj):
        """重落只清 pending —— 作者已确认/已忽略的意志不因重算蒸发。"""
        db, pid = proj
        plan = _mk_plan(db, pid)
        pc._run_planned_chars(db, plan, _lines(["甲", "乙", "丙"]))
        rows = {o.name: o for o in
                db.query(PlannedCharORM).filter_by(plan_id=plan.id).all()}
        rows["甲"].status = "confirmed"
        rows["甲"].character_id = "ch_x"
        rows["乙"].status = "dismissed"
        db.commit()
        # 重生成：丙消失、甲乙仍在计划里，还新增丁
        r = pc._run_planned_chars(db, plan, _lines(["甲", "丁"], ["乙"]))
        names = {o["name"]: o for o in r["planned_chars"]}
        assert names["甲"]["status"] == "confirmed"
        assert names["甲"]["character_id"] == "ch_x", "确认行原样保留"
        assert names["乙"]["status"] == "dismissed"
        assert names["丁"]["status"] == "pending"
        assert "丙" not in names, "旧 pending 行已被清"
        assert db.query(PlannedCharORM).filter_by(plan_id=plan.id).count() == 3

    def test_confirm_creates_character_and_links(self, proj):
        """确认 → 建卡进 characters（兜底 brief 带槽位语义）→ 回链 confirmed。"""
        db, pid = proj
        plan = _mk_plan(db, pid)
        r = pc._run_planned_chars(db, plan, _lines(["林小天"]))
        pc_id = r["planned_chars"][0]["id"]
        # 先绑槽位
        pc.update_planned_char(db, pid, "a1", pc_id, slot="引路人师长",
                               slot_desc="导师型角色")
        out = pc.confirm_planned_char(db, pid, "a1", pc_id,
                                      attrs={"personality": "沉稳", "role_type": "配角"})
        assert out["status"] == "confirmed" and out["character_id"]
        ch = db.query(CharacterORM).filter_by(id=out["character_id"]).first()
        assert ch is not None and ch.name == "林小天"
        assert ch.personality == "沉稳"
        row = db.query(PlannedCharORM).filter_by(id=pc_id).first()
        assert row.status == "confirmed" and row.character_id == ch.id

    def test_confirm_same_name_links_not_duplicate(self, proj):
        """库里已有同名角色 → 不重复建卡，直接回链确认。"""
        db, pid = proj
        existing = _mk_char(db, pid, "林小天")
        plan = _mk_plan(db, pid)
        r = pc._run_planned_chars(db, plan, _lines(["林小天"]))
        pc_id = r["planned_chars"][0]["id"]
        out = pc.confirm_planned_char(db, pid, "a1", pc_id)
        assert out["character_id"] == existing.id
        assert "同名" in (out.get("skipped") or "")
        assert db.query(CharacterORM).filter_by(project_id=pid, name="林小天").count() == 1

    def test_confirm_idempotent(self, proj):
        db, pid = proj
        plan = _mk_plan(db, pid)
        r = pc._run_planned_chars(db, plan, _lines(["林小天"]))
        pc_id = r["planned_chars"][0]["id"]
        first = pc.confirm_planned_char(db, pid, "a1", pc_id)
        again = pc.confirm_planned_char(db, pid, "a1", pc_id)
        assert again["character_id"] == first["character_id"]
        assert "幂等" in (again.get("skipped") or "")
        assert db.query(CharacterORM).filter_by(project_id=pid, name="林小天").count() == 1

    def test_update_dismiss_and_validation(self, proj):
        db, pid = proj
        plan = _mk_plan(db, pid)
        r = pc._run_planned_chars(db, plan, _lines(["林小天"]))
        pc_id = r["planned_chars"][0]["id"]
        out = pc.update_planned_char(db, pid, "a1", pc_id, status="dismissed")
        assert out["status"] == "dismissed"
        with pytest.raises(RuntimeError):
            pc.update_planned_char(db, pid, "a1", pc_id, status="什么鬼")
        with pytest.raises(RuntimeError):
            pc.update_planned_char(db, pid, "a1", "不存在的id")

    def test_unmatched_slots_reported_not_auto_bound(self, proj):
        """casting 没配上的槽位 → 报告给计划页让作者关联，不自动猜绑定。"""
        db, pid = proj
        plan = _mk_plan(db, pid)
        castings = [
            {"slot": "引路人师长", "character_id": "ch1", "character_name": "药老"},
            {"slot": "骄横的对手", "character_id": None, "character_name": None,
             "slot_desc": "世家少爷"},
        ]
        r = pc._run_planned_chars(db, plan, _lines(["林小天"]), castings)
        assert r["unmatched_slots"] == [
            {"slot": "骄横的对手", "slot_desc": "世家少爷"}]
        row = db.query(PlannedCharORM).filter_by(plan_id=plan.id).first()
        assert row.slot is None, "绝不自动猜绑定"


# ---------------------------------------------------------------------------
# 2. 篇间交接差集（警告非报错）
# ---------------------------------------------------------------------------
class TestCarryover:
    def test_unaddressed_warned(self, proj):
        """上一篇末尾在场的角色，本篇既不召回也不引新 → 警告名单。"""
        db, pid = proj
        _mk_memory(db, pid, 10, characters=["王大锤", "刘二丫"], article_id="a2")
        _mk_memory(db, pid, 9, characters=["王大锤"], article_id="a2")
        _mk_memory(db, pid, 8, characters=["路人甲"], article_id="a2")
        r = pc.carryover_check(db, pid, "a1", _lines([]), [])
        assert set(r["carryover_names"]) == {"王大锤", "刘二丫", "路人甲"}
        assert r["last_chapters"] == [10, 9, 8]

    def test_referenced_not_flagged(self, proj):
        db, pid = proj
        _mk_memory(db, pid, 10, characters=["王大锤", "刘二丫"], article_id="a2")
        lines = [{"no": 1, "beat": "b", "summary": "s", "new_chars": [],
                  "recall_chars": ["王大锤"], "target_words": 1, "hook": "",
                  "template_ref": ""},
                 {"no": 2, "beat": "b", "summary": "s", "new_chars": ["新差事人"],
                  "recall_chars": [], "target_words": 1, "hook": "",
                  "template_ref": ""}]
        r2 = pc.carryover_check(db, pid, "a1", lines,
                                casting_names=["刘二丫"])
        assert r2["carryover_names"] == [], "召回+选角+引新都算'有交代'"

    def test_current_article_memories_excluded(self, proj):
        """本篇自己的章节记忆不是"遗留"（防自指）。"""
        db, pid = proj
        _mk_memory(db, pid, 50, characters=["本篇角色"], article_id="a1")
        r = pc.carryover_check(db, pid, "a1", _lines([]), [])
        assert r["carryover_names"] == []
        assert r["last_chapters"] == []

    def test_no_memories_silent_empty(self, proj):
        db, pid = proj
        r = pc.carryover_check(db, pid, "a1", _lines([]), [])
        assert r == {"carryover_names": [], "last_chapters": [], "from_article_id": None}

    def test_save_lines_keeps_carryover_and_get_plan_exposes_it(self, proj, monkeypatch):
        """7.5 修（2026-09-13）：save_lines/refine_line 重写 plan JSON 时不得洗掉
        carryover/reentry；get_plan 要把它们吐给前端。"""
        db, pid = proj
        plan = _mk_plan(db, pid)
        carry = {"carryover_names": ["王大锤"], "last_chapters": [{"name": "王大锤", "chapter_no": 10}],
                 "from_article_id": "a2"}
        reentry = [{"character_id": "c1", "name": "药老", "priority": "foreshadow",
                    "foreshadows": [], "world_events": [], "arc_digest": []}]
        plan.plan = {"lines": _lines([]), "notes": "", "carryover": carry, "reentry_materials": reentry}
        db.commit()

        pc.save_lines(db, pid, "a1", lines=_lines(["林小满"]), notes="改过")
        got = pc.get_plan(db, pid, "a1")
        assert got["carryover"] == carry, "行级保存不得洗掉交接差集"
        assert got["reentry_materials"] == reentry, "行级保存不得洗掉回归材料"
        assert got["notes"] == "改过"
        assert [l["new_chars"] for l in got["lines"]] == [["林小满"]]

        # refine_line 同样保留（mock 掉 LLM 调用）
        monkeypatch.setattr(pc, "ds_key", lambda db: "fake-key")
        monkeypatch.setattr(pc, "_ds_post", lambda key, prompt, **kw: json.dumps(
            {"no": 1, "beat": "b", "summary": "冲突加大后", "new_chars": ["林小满"],
             "recall_chars": [], "target_words": 100, "hook": "h", "template_ref": ""},
            ensure_ascii=False))
        pc.refine_line(db, pid, "a1", line_no=1, instruction="把冲突加大")
        got2 = pc.get_plan(db, pid, "a1")
        assert got2["carryover"] == carry
        assert got2["reentry_materials"] == reentry


# ---------------------------------------------------------------------------
# 3. 回归理由材料包（确定性预取）
# ---------------------------------------------------------------------------
class TestReentry:
    def test_foreshadow_top_priority(self, proj):
        """未回收伏笔挂了本角色 → priority=foreshadow（收伏笔优于凭空编）。"""
        db, pid = proj
        ch = _mk_char(db, pid, "云梦瑶", last_seen=10)
        _mk_foreshadow(db, pid, "云梦瑶留下的玉佩之谜", buried=8, related=[ch.id])
        _mk_foreshadow(db, pid, " unrelated 无关伏笔", buried=8)
        m = cc.reentry_material(db, pid, ch.id)
        assert m["priority"] == "foreshadow"
        assert len(m["foreshadows"]) == 1
        assert m["foreshadows"][0]["buried_chapter"] == 8

    def test_resolved_foreshadow_excluded(self, proj):
        db, pid = proj
        ch = _mk_char(db, pid, "云梦瑶", last_seen=10)
        _mk_foreshadow(db, pid, "云梦瑶的旧事", buried=5, status="resolved",
                       related=[ch.id])
        m = cc.reentry_material(db, pid, ch.id)
        assert m["foreshadows"] == [], "已回收伏笔不再是回归理由"
        assert m["priority"] != "foreshadow"

    def test_world_events_hit_by_name(self, proj):
        """缺席期（last_seen 之后）点名本角色的章 → 世界线事件。"""
        db, pid = proj
        ch = _mk_char(db, pid, "云梦瑶", last_seen=10)
        _mk_memory(db, pid, 12, characters=["别人"], summary="平静的一章")
        _mk_memory(db, pid, 15, characters=["云梦瑶"], summary="有人提及云梦瑶的旧宅被烧")
        m = cc.reentry_material(db, pid, ch.id)
        assert m["priority"] == "world_events"
        assert len(m["world_events"]) == 1
        assert m["world_events"][0]["chapter_no"] == 15
        assert [d["chapter_no"] for d in m["arc_digest"]] == [12, 15], "缺席期逐章梗概"

    def test_fallback_when_nothing(self, proj):
        db, pid = proj
        ch = _mk_char(db, pid, "无名氏", last_seen=10)
        _mk_memory(db, pid, 12, characters=["别人"], summary="与该角色无关")
        m = cc.reentry_material(db, pid, ch.id)
        assert m["priority"] == "fallback"
        assert m["foreshadows"] == [] and m["world_events"] == []
        assert m["arc_digest"], "兜底也要给缺席期梗概"

    def test_missing_character_empty(self, proj):
        db, pid = proj
        assert cc.reentry_material(db, pid, "不存在的id") == {}


# ---------------------------------------------------------------------------
# 4. 级联删除
# ---------------------------------------------------------------------------
class TestCascade:
    def test_delete_article_clears_planned_chars(self, proj):
        from app.services import article_crud
        db, pid = proj
        plan = _mk_plan(db, pid, "a1")
        pc._run_planned_chars(db, plan, _lines(["甲", "乙"]))
        assert db.query(PlannedCharORM).filter_by(article_id="a1").count() == 2
        article_crud.delete_article(db, pid, "a1")
        assert db.query(PlannedCharORM).filter_by(article_id="a1").count() == 0
        assert db.query(ArticlePlanORM).filter_by(article_id="a1").count() == 0

    def test_delete_volume_clears_planned_chars(self, proj):
        from app.services import volume_crud
        db, pid = proj
        plan = _mk_plan(db, pid, "a1")
        pc._run_planned_chars(db, plan, _lines(["甲"]))
        volume_crud.delete_volume(db, pid, "v1")
        assert db.query(PlannedCharORM).count() == 0

    def test_delete_project_clears_planned_chars(self, proj):
        from app.services import project_crud
        db, pid = proj
        plan = _mk_plan(db, pid, "a1")
        pc._run_planned_chars(db, plan, _lines(["甲"]))
        project_crud.delete_project(db, pid)
        assert db.query(PlannedCharORM).count() == 0

    def test_unique_plan_name_constraint(self, proj):
        """UNIQUE(plan_id, name)：同一篇同一新角色只能有一条引入单。"""
        import sqlalchemy as sa
        db, pid = proj
        plan = _mk_plan(db, pid)
        db.add(PlannedCharORM(id=uuid.uuid4().hex, plan_id=plan.id,
                              project_id=pid, article_id="a1",
                              name="甲", first_appearance=1))
        db.add(PlannedCharORM(id=uuid.uuid4().hex, plan_id=plan.id,
                              project_id=pid, article_id="a1",
                              name="甲", first_appearance=2))
        with pytest.raises(sa.exc.IntegrityError):
            db.commit()
        db.rollback()
