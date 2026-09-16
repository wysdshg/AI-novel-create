"""Phase 7.3 ②③ 角色向量选角单测（全 mock / 离线，不碰网络与生产库）。

钉住六条容易回归的行为：
1. **cast 槽位向量化三入口**（create/update/delete + draft 重建）都不能留孤儿 plot_cast 块；
2. **显式余弦**：分数是余弦本身（不是 store 的 1/(1+L2)）—— 阈值标定的前提；
3. **状态门**：已死角色默认不进候选池；蛰伏/离场可入选但要回归理由；
4. **平局判据只改排序不动打分**（共现 → 活跃度）；
5. **手改 manual 不被重算覆盖**（唯一真相源 = plan_castings 表，不碰 plan.json）；
6. **级联删除**：删篇/卷/作品必须清掉 plan_castings。
"""
import json
import uuid

import pytest

import app.core.database as dbmod
from app.models.orm import (
    ArticleORM, ArticlePlanORM, ChapterMemoryORM, CharacterORM,
    PlanCastingORM, PlotTemplateORM, ProjectORM, VectorChunkORM, VolumeORM,
)
from app.services import casting_crud as cc
from app.services import plot_template_crud as tpl
from app.services.vector_store import BruteVectorStore, VectorRow


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _mk_project(db):
    p = ProjectORM(id="p1", name="选角测试")
    v = VolumeORM(id="v1", project_id="p1", name="卷一")
    a = ArticleORM(id="a1", project_id="p1", volume_id="v1", name="篇一")
    db.add_all([p, v, a])
    db.commit()
    return p.id


def _mk_template(db, structure=None, name="退婚逆袭"):
    o = PlotTemplateORM(
        id=uuid.uuid4().hex, name=name, scale="arc",
        genre_tags=["玄幻"], logline="被退婚后的打脸升级",
        structure=structure or {
            "phases": [{"phase": "开局", "beats": [
                {"beat": "当众退婚", "variants": [{"src": "书", "how": "撕毁婚约"}]}]}],
            "cast": [
                {"slot": "引路人师长", "desc": "主角的导师型角色，掌握关键资源",
                 "mode": "助力", "beats": ["异象触发"],
                 "srcs": [{"book": "斗破", "alias": "友·配角4"}]},
                {"slot": "退婚的未婚妻", "desc": "当众退婚的女方，背后有强宗撑腰",
                 "mode": "阻碍", "beats": ["当众退婚"],
                 "srcs": [{"book": "斗破", "alias": "敌·配角2"}]},
            ],
        },
        status="draft",
    )
    db.add(o)
    db.commit()
    return o


def _mk_char(db, pid, name, *, status="alive", personality="", background="",
             talent="", brief="", role_type="配角", last_seen=None, count=0):
    o = CharacterORM(id=uuid.uuid4().hex, project_id=pid, name=name,
                     role_type=role_type, personality=personality,
                     background=background, talent=talent, brief=brief,
                     status=status, last_seen_chapter=last_seen,
                     appearance_count=count)
    db.add(o)
    db.commit()
    return o


@pytest.fixture()
def proj(test_db):
    pid = _mk_project(test_db)
    yield test_db, pid


# ---------------------------------------------------------------------------
# 1. cast 向量化：三入口 + draft 重建，不留孤儿
# ---------------------------------------------------------------------------
class TestCastIndexing:
    def test_index_template_indexes_cast_chunks(self, test_db):
        t = _mk_template(test_db)
        # 无 key 时 index 静默失败 → 用 Brute store + 假 embedding 验证
        assert tpl.cast_chunks(t), "cast_chunks 必须产出（每槽位一块）"
        texts = tpl.cast_chunks(t)
        assert "引路人师长" in texts[0]      # slot 名参与向量文本
        assert "导师" in texts[0]            # desc 参与
        # chunk_idx == cast 下标 的顺序契约
        assert "退婚的未婚妻" in texts[1]

    def test_delete_clears_all_source_types(self, test_db, monkeypatch):
        t = _mk_template(test_db)
        pid = "__global__"
        store = BruteVectorStore()
        rows = [
            VectorRow(chunk_id=uuid.uuid4().hex, project_id=pid,
                      source_type=tpl.SOURCE_TYPE, source_id=t.id,
                      chunk_idx=0, chunk_text="beat块", model="fake",
                      vector=[1.0, 0.0]),
            VectorRow(chunk_id=uuid.uuid4().hex, project_id=pid,
                      source_type=tpl.SOURCE_TYPE_CAST, source_id=t.id,
                      chunk_idx=0, chunk_text="槽位块", model="fake",
                      vector=[0.0, 1.0]),
            VectorRow(chunk_id=uuid.uuid4().hex, project_id=pid,
                      source_type=tpl.SOURCE_TYPE_ARCHETYPE, source_id=t.id,
                      chunk_idx=0, chunk_text="原型块", model="fake",
                      vector=[1.0, 1.0]),
        ]
        store.upsert(test_db, rows)
        assert tpl.delete(test_db, t.id) is True
        left = test_db.query(VectorChunkORM).filter_by(source_id=t.id).count()
        assert left == 0, "删模板必须把 plot_template / plot_cast / char_archetype 三套向量都清掉"

    def test_archetype_chunks_exclude_srcs(self, test_db):
        """原型块文本 = slot+desc+mode，**不含 srcs**（与 cast 块的形态区分，2026-09-15）。"""
        t = _mk_template(test_db)
        casts = tpl.structure_casts(t)
        arch = tpl.archetype_chunks(t)
        cast = tpl.cast_chunks(t)
        assert len(arch) == len(casts) == len(cast)   # 顺序契约：下标一一对应
        for i, c in enumerate(casts):
            assert c["slot"] in arch[i] and f"定位：{c['mode']}" in arch[i]
            for s in c.get("srcs") or []:
                if s.get("desc"):
                    assert s["desc"] not in arch[i], "原型块不得掺 srcs 具体实现"
                    assert s["desc"] in cast[i], "cast 块必须掺 srcs（两路形态必须不同）"

    def test_distill_replace_drafts_clears_vectors(self, proj, monkeypatch):
        """回归：distill_all 的批量 delete 绕过钩子 → 必须显式清向量（2026-09-13 修）。"""
        from app.services import plot_distill as pd
        db, _pid = proj
        t = _mk_template(db)
        tid = t.id          # 先取 id：distill_all 会删掉这行 ORM 对象，之后再访问会 ObjectDeletedError
        VCO = VectorChunkORM
        db.add(VCO(id=uuid.uuid4().hex, project_id="__global__",
                   source_type=tpl.SOURCE_TYPE, source_id=tid, chunk_idx=0,
                   chunk_text="x", model="fake", dim=2, embedding_json="[1,0]"))
        db.add(VCO(id=uuid.uuid4().hex, project_id="__global__",
                   source_type=tpl.SOURCE_TYPE_CAST, source_id=tid, chunk_idx=0,
                   chunk_text="x", model="fake", dim=2, embedding_json="[0,1]"))
        db.add(VCO(id=uuid.uuid4().hex, project_id="__global__",
                   source_type=tpl.SOURCE_TYPE_ARCHETYPE, source_id=tid, chunk_idx=0,
                   chunk_text="x", model="fake", dim=2, embedding_json="[1,1]"))
        db.commit()
        assert db.query(VCO).filter_by(source_id=tid).count() == 3
        # collect_arcs 没数据 → groups 为空 → 只触发清理路径
        r = pd.distill_all(db)
        assert r["groups"] == 0
        assert db.query(VCO).filter_by(source_id=tid).count() == 0, \
            "replace_drafts 清 draft 模板时必须连带清掉三套向量块"


# ---------------------------------------------------------------------------
# 2. 显式余弦 + 角色人设文本
# ---------------------------------------------------------------------------
class TestCosine:
    def test_character_profile_excludes_name(self, proj):
        db, pid = proj
        c = _mk_char(db, pid, "林尘", personality="坚毅", background="宗门弟子")
        txt = cc.character_profile_text(c)
        assert "林尘" not in txt, "名字是无信息噪声，不得进入人设向量文本"
        assert "坚毅" in txt and "宗门弟子" in txt

    def test_explicit_cosine_is_real_cosine(self, proj, monkeypatch):
        """分数必须是余弦本身 —— sqlite-vec 的 1/(1+L2) 会让同一相似度显示成 ≈0.56，
        照 0.65 直接标定会过严（docs/03 §7.3 决策 3）。"""
        db, pid = proj
        a, b = [1.0, 0.0], [1.0, 0.0]
        assert cc._cosine(a, b) == pytest.approx(1.0)
        assert cc._cosine(a, [0.0, 1.0]) == pytest.approx(0.0)
        assert cc._cosine(a, [0.7071, 0.7071]) == pytest.approx(0.7071, abs=1e-3)
        assert cc._cosine(a, [0.0, 0.0]) == 0.0        # 零向量防御


# ---------------------------------------------------------------------------
# 3. 状态门
# ---------------------------------------------------------------------------
class TestStatusGate:
    def test_dead_excluded_alive_included(self, proj):
        db, pid = proj
        _mk_char(db, pid, "活人", personality="热情")
        _mk_char(db, pid, "死人", personality="热情", status="dead")
        pool = cc.character_pool(db, pid)
        names = {c["character"].name for c in pool}
        assert "活人" in names and "死人" not in names, "已死角色默认不进候选池"

    def test_dead_included_when_explicit(self, proj):
        db, pid = proj
        _mk_char(db, pid, "死人", personality="热情", status="dead")
        pool = cc.character_pool(db, pid, include_dead=True)
        assert {c["character"].name for c in pool} == {"死人"}

    def test_departed_flags_reentry_note(self, proj):
        db, pid = proj
        _mk_char(db, pid, "远走者", personality="沉默", status="departed")
        pool = cc.character_pool(db, pid)
        assert pool[0]["needs_reentry_note"] is True, "离场角色可入选但必须交回归理由"

    def test_dormant_by_gap(self, proj):
        db, pid = proj
        # last_seen=10，当前章=100 → 差 90 > 30 → 按蛰伏处理
        _mk_char(db, pid, "隐居者", personality="淡泊", last_seen=10, count=5)
        pool = cc.character_pool(db, pid, current_chapter=100)
        assert pool[0]["status"] == "dormant"
        assert pool[0]["needs_reentry_note"] is True

    def test_invalid_status_treated_as_alive(self, proj):
        db, pid = proj
        c = _mk_char(db, pid, "脏值者", personality="神秘", status="什么是状态")
        pool = cc.character_pool(db, pid)
        assert pool[0]["status"] == "alive", "脏状态值必须按在世处理（留痕但功能不哑）"

    def test_refresh_appearances_derives_from_memories(self, proj):
        """last_seen / appearance_count 必须纯派生（LLM 只会引入漂移）。"""
        db, pid = proj
        _mk_char(db, pid, "甲", personality="x")
        db.add(ChapterMemoryORM(id="m1", project_id=pid, chapter_id="c1",
                                chapter_no=3, summary="s", characters=["甲"]))
        db.add(ChapterMemoryORM(id="m2", project_id=pid, chapter_id="c2",
                                chapter_no=7, summary="s", characters=["甲", "乙"]))
        db.commit()
        r = cc.refresh_character_appearances(db, pid)
        assert r["updated"] >= 1
        row = db.query(CharacterORM).filter_by(name="甲").first()
        assert row.last_seen_chapter == 7 and row.appearance_count == 2


# ---------------------------------------------------------------------------
# 4. 平局判据（只改排序不动打分）
# ---------------------------------------------------------------------------
class TestTieBreak:
    def test_rank_prefers_cooccurrence_then_recent(self, proj):
        db, pid = proj
        a = _mk_char(db, pid, "甲", personality="坚毅少年", last_seen=5)
        b = _mk_char(db, pid, "乙", personality="坚毅少年", last_seen=2)
        pair = {("甲", "锚点"): 3, ("乙", "锚点"): 1}
        ranked = cc._rank([{"character": a, "score": 0.8, "last_seen": 5},
                           {"character": b, "score": 0.8, "last_seen": 2}],
                          pair, ["锚点"])
        assert ranked[0]["character"].name == "甲"    # 共现多者先

        # 共现相同 → 活跃度（last_seen 更近）优先
        pair2 = {("甲", "锚点"): 1, ("乙", "锚点"): 1}
        ranked2 = cc._rank([{"character": b, "score": 0.8, "last_seen": 2},
                            {"character": a, "score": 0.8, "last_seen": 5}],
                           pair2, ["锚点"])
        assert ranked2[0]["character"].name == "甲"

    def test_cooccurrence_counts_chapters(self, proj):
        db, pid = proj
        for i in range(3):
            db.add(ChapterMemoryORM(id=f"m{i}", project_id=pid, chapter_id=f"c{i}",
                                    chapter_no=i + 1, summary="s",
                                    characters=["甲", "乙", "丙"]))
        db.commit()
        pair = cc._cooccurrence(db, pid)
        assert pair[tuple(sorted(("甲", "乙")))] == 3
        assert pair[tuple(sorted(("乙", "丙")))] == 3


# ---------------------------------------------------------------------------
# 5. 选角主流程（mock embedding）+ 手改 + 级联
# ---------------------------------------------------------------------------
class TestCastFlow:
    def _enable_embed(self, monkeypatch, slot_by_text, char_by_text):
        """可控假 embedding：按文本关键词给方向向量（维度 8）。"""
        from app.services import embedding_client

        def fake_embed(texts, db=None, **kw):
            out = []
            for t in texts:
                v = [0.0] * 8
                for kw_text, idx in {**slot_by_text, **char_by_text}.items():
                    if kw_text in t:
                        v[idx] = 1.0
                        break
                if not any(v):
                    v[7] = 1.0     # 缺省方向
                out.append(v)
            return out

        monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)

    def _mk_plan(self, db, pid, template, *, recall=None):
        o = ArticlePlanORM(
            id=uuid.uuid4().hex, project_id=pid, article_id="a1",
            template_ids=[template.id], template_names=[template.name],
            plan={"lines": [{"no": 1, "beat": "当众退婚", "summary": "主角被退婚",
                             "recall_chars": recall or [], "new_chars": []}]},
            origin="template", status="draft",
        )
        db.add(o)
        db.commit()
        return o

    def test_cast_slots_for_plan_matches_and_persists(self, proj, monkeypatch):
        db, pid = proj
        t = _mk_template(db)
        self._mk_plan(db, pid, t)
        # 师长槽位(1) ↔ 老者角色(1)；未婚妻槽位(2) ↔ 女方角色(2)
        self._enable_embed(monkeypatch,
                           {"导师型": 1, "女方": 2},
                           {"导师型": 1, "女方": 2})
        _mk_char(db, pid, "药老", personality="神秘的导师型老者，掌握关键资源")
        _mk_char(db, pid, "纳兰", personality="当众退婚的女方，背后有强宗撑腰")
        r = cc.cast_slots_for_plan(db, pid, "a1")
        assert r["reason"] is None
        by_slot = {c["slot"]: c for c in r["castings"]}
        assert by_slot["引路人师长"]["character_name"] == "药老"
        assert by_slot["退婚的未婚妻"]["character_name"] == "纳兰"
        # 分数是余弦（=1.0），不是 1/(1+L2)
        assert by_slot["引路人师长"]["score"] == pytest.approx(1.0)
        db_rows = db.query(PlanCastingORM).all()
        assert len(db_rows) == 2
        assert all(row.source == "auto" for row in db_rows)

    def test_low_score_leaves_slot_empty(self, proj, monkeypatch):
        db, pid = proj
        t = _mk_template(db)
        self._mk_plan(db, pid, t)
        # 角色与槽位方向完全不同（都落 7）
        self._enable_embed(monkeypatch, {"导师型": 1, "女方": 2}, {"其他": 7})
        _mk_char(db, pid, "路人", personality="卖烧饼的")
        r = cc.cast_slots_for_plan(db, pid, "a1")
        by_slot = {c["slot"]: c for c in r["castings"]}
        assert all(c["character_id"] is None for c in by_slot.values()), \
            "低于阈值不得硬凑（走 new_chars）"
        assert all(c["reason"] for c in by_slot.values())

    def test_one_role_one_slot(self, proj, monkeypatch):
        """同一角色不能演两个功能位（两槽位抢同一人是立项动机）。"""
        db, pid = proj
        t = _mk_template(db)
        self._mk_plan(db, pid, t)
        # 两个槽位都最强匹配同一个角色
        self._enable_embed(monkeypatch, {"导师型": 1, "女方": 2}, {"导师型": 1, "女方": 1})
        _mk_char(db, pid, "全能者", personality="导师型老者，也像退婚女方")
        r = cc.cast_slots_for_plan(db, pid, "a1")
        by_slot = {c["slot"]: c for c in r["castings"]}
        got = [c["character_name"] for c in by_slot.values() if c["character_name"]]
        assert len(got) <= 1, "一个角色只能被一个槽位占用"
        losers = [c for c in by_slot.values() if c["character_name"] is None]
        assert losers and losers[0]["reason"], "落选槽位必须写明原因"

    def test_dead_never_cast(self, proj, monkeypatch):
        db, pid = proj
        t = _mk_template(db)
        self._mk_plan(db, pid, t)
        self._enable_embed(monkeypatch, {"导师型": 1, "女方": 2},
                           {"导师型": 1, "女方": 1})
        _mk_char(db, pid, "亡者", personality="导师型老者，掌握关键资源", status="dead")
        _mk_char(db, pid, "路人", personality="卖烧饼的")   # 保证池子非空（不会提前降级）
        r = cc.cast_slots_for_plan(db, pid, "a1")
        by_slot = {c["slot"]: c for c in r["castings"]}
        assert by_slot["引路人师长"]["character_id"] is None, "死人不得被选角"

    def test_manual_survives_recompute(self, proj, monkeypatch):
        db, pid = proj
        t = _mk_template(db)
        self._mk_plan(db, pid, t)
        self._enable_embed(monkeypatch, {"导师型": 1, "女方": 2},
                           {"导师型": 1, "女方": 2})
        c1 = _mk_char(db, pid, "药老", personality="导师型老者")
        c2 = _mk_char(db, pid, "纳兰", personality="退婚女方")
        cc.cast_slots_for_plan(db, pid, "a1")
        # 作者把「引路人师长」改成纳兰
        cc.set_casting(db, pid, "a1", slot="引路人师长", character_id=c2.id)
        row = db.query(PlanCastingORM).filter_by(slot="引路人师长").first()
        assert row.source == "manual" and row.character_name == "纳兰"
        # 重算 → manual 保留
        cc.cast_slots_for_plan(db, pid, "a1")
        row2 = db.query(PlanCastingORM).filter_by(slot="引路人师长").first()
        assert row2.character_name == "纳兰" and row2.source == "manual"
        row3 = db.query(PlanCastingORM).filter_by(slot="退婚的未婚妻").first()
        assert row3.source == "auto", "auto 行正常重算"

    def test_set_casting_rejects_dead(self, proj):
        db, pid = proj
        t = _mk_template(db)
        plan = self._mk_plan(db, pid, t)
        db.add(PlanCastingORM(id=uuid.uuid4().hex, plan_id=plan.id,
                              project_id=pid, article_id="a1",
                              template_id=t.id, slot="引路人师长", source="auto"))
        db.commit()
        dead = _mk_char(db, pid, "亡者", status="dead")
        with pytest.raises(RuntimeError, match="已死"):
            cc.set_casting(db, pid, "a1", slot="引路人师长", character_id=dead.id)

    def test_check_appearances_blocks_dead(self, proj):
        db, pid = proj
        _mk_char(db, pid, "亡者", status="dead")
        _mk_char(db, pid, "远走者", status="departed")
        lines = [{"no": 1, "recall_chars": ["亡者", "远走者"]}]
        r = cc.check_appearances(db, pid, lines)
        assert r["blocked_dead"] == ["第1章：亡者"]
        assert lines[0]["recall_chars"] == ["远走者"], "已死角色必须被剔除"
        assert r["needs_reentry_note"] == [{"line": 1, "name": "远走者", "status": "departed"}]

    def test_embed_failure_degrades_gracefully(self, proj, monkeypatch):
        db, pid = proj
        t = _mk_template(db)
        self._mk_plan(db, pid, t)
        _mk_char(db, pid, "药老", personality="导师型")
        from app.services import embedding_client

        def boom(texts, db=None, **kw):
            raise embedding_client.EmbeddingError("无 key")
        monkeypatch.setattr(embedding_client, "embed_texts", boom)
        r = cc.cast_slots_for_plan(db, pid, "a1")
        assert r["castings"] == []
        assert r["reason"] and "embedding" in r["reason"], "降级必须写明原因"


# ---------------------------------------------------------------------------
# 6. 级联删除
# ---------------------------------------------------------------------------
class TestCascade:
    def test_delete_article_clears_plan_and_casting(self, test_db):
        pid = _mk_project(test_db)
        t = _mk_template(test_db)
        plan = ArticlePlanORM(id=uuid.uuid4().hex, project_id=pid, article_id="a1",
                              template_ids=[t.id], plan={"lines": []}, status="draft")
        test_db.add(plan)
        test_db.add(PlanCastingORM(id=uuid.uuid4().hex, plan_id=plan.id,
                                   project_id=pid, article_id="a1",
                                   template_id=t.id, slot="引路人师长"))
        test_db.commit()
        from app.services import article_crud
        assert article_crud.delete_article(test_db, pid, "a1") is True
        assert test_db.query(ArticlePlanORM).count() == 0
        assert test_db.query(PlanCastingORM).count() == 0, "删篇必须级联清 casting"

    def test_delete_volume_clears_plan_and_casting(self, test_db):
        pid = _mk_project(test_db)
        t = _mk_template(test_db)
        plan = ArticlePlanORM(id=uuid.uuid4().hex, project_id=pid, article_id="a1",
                              template_ids=[t.id], plan={"lines": []}, status="draft")
        test_db.add(plan)
        test_db.add(PlanCastingORM(id=uuid.uuid4().hex, plan_id=plan.id,
                                   project_id=pid, article_id="a1",
                                   template_id=t.id, slot="引路人师长"))
        test_db.commit()
        from app.services import volume_crud
        assert volume_crud.delete_volume(test_db, pid, "v1") is True
        assert test_db.query(ArticlePlanORM).count() == 0
        assert test_db.query(PlanCastingORM).count() == 0

    def test_delete_project_clears_casting(self, test_db):
        pid = _mk_project(test_db)
        t = _mk_template(test_db)
        plan = ArticlePlanORM(id=uuid.uuid4().hex, project_id=pid, article_id="a1",
                              template_ids=[t.id], plan={"lines": []}, status="draft")
        test_db.add(plan)
        test_db.add(PlanCastingORM(id=uuid.uuid4().hex, plan_id=plan.id,
                                   project_id=pid, article_id="a1",
                                   template_id=t.id, slot="引路人师长"))
        test_db.commit()
        from app.services import project_crud
        assert project_crud.delete_project(test_db, pid) is True
        assert test_db.query(PlanCastingORM).count() == 0

    def test_unique_plan_slot(self, test_db):
        """UNIQUE(plan_id, slot)：同一篇同一槽位只能一条（门槛 6 的数据库级保证）。"""
        import sqlalchemy
        pid = _mk_project(test_db)
        t = _mk_template(test_db)
        plan = ArticlePlanORM(id=uuid.uuid4().hex, project_id=pid, article_id="a1",
                              template_ids=[t.id], plan={"lines": []}, status="draft")
        test_db.add(plan)
        test_db.commit()
        test_db.add(PlanCastingORM(id=uuid.uuid4().hex, plan_id=plan.id,
                                   project_id=pid, article_id="a1",
                                   template_id=t.id, slot="引路人师长"))
        test_db.commit()
        test_db.add(PlanCastingORM(id=uuid.uuid4().hex, plan_id=plan.id,
                                   project_id=pid, article_id="a1",
                                   template_id=t.id, slot="引路人师长"))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            test_db.commit()
        test_db.rollback()


# ---------------------------------------------------------------------------
# 7. 阈值标定（离线：假 embedding 下跑通即可，真标定在真机临时项目做）
# ---------------------------------------------------------------------------
class TestCalibration:
    def test_calibrate_returns_pos_neg_and_suggestion(self, test_db, monkeypatch):
        pid = _mk_project(test_db)
        _mk_char(test_db, pid, "药老", personality="导师型老者，掌握关键资源")
        _mk_char(test_db, pid, "路人", personality="卖烧饼的")
        from app.services import embedding_client

        def fake_embed(texts, db=None, **kw):
            out = []
            for txt in texts:
                v = [0.0] * 8
                v[1 if ("导师" in txt or "老者" in txt) else 7] = 1.0
                out.append(v)
            return out
        monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
        slots = [{"slot": "引路人师长", "desc": "导师型", "text": "引路人师长｜导师型"}]
        r = cc.calibrate_thresholds(
            test_db, pid, slots,
            positives={"引路人师长": ["药老"]},
            negatives={"引路人师长": ["路人"]})
        assert r["per_slot"][0]["pos_min"] == pytest.approx(1.0)
        assert r["per_slot"][0]["neg_max"] == pytest.approx(0.0)
        assert r["suggestion"]["min_score"] is not None

    def test_plan_context_excludes_dead_and_orders_by_recent(self, proj):
        """回归：_book_context 原来是裸 limit(40) —— 死人跟主角平等进上下文。"""
        from app.services import plan_crud
        db, pid = proj
        _mk_char(db, pid, "死人", personality="x", status="dead")
        _mk_char(db, pid, "新人", personality="x", last_seen=None)
        _mk_char(db, pid, "老熟人", personality="x", last_seen=50)
        ctx = plan_crud._book_context(db, pid, "a1")
        names = ctx["characters"]
        assert "死人" not in names, "已死角色不得进规划上下文"
        assert names.index("老熟人") < names.index("新人"), "按最近出场排序（活跃优先）"
