"""Phase 7.1 匿名化归一：抽取合并 / 代称分配 / 替换幂等与备份 / 全流程。

重点验证（用户方案 23:44 拍板 + 三修正）：
- 敌友分桶编号（友·配角1 / 敌·配角1 —— 不用 A/a 大小写）；
- 境界绝对阶梯（境界1~N，不用相对字母防漂移）；
- 替换的**幂等与备份**（raw 只在为空时写，重复跑不覆盖备份）；
- 多批抽取的合并（同名实体 relation 敌对优先、desc 取更长；境界按首现顺序）。
"""
import json

from app.models.orm import BookAliasORM, ChapterSummaryORM
from app.services import anonymizer as az


def _mk(test_db, book, chapters, summaries=None):
    for no in chapters:
        test_db.add(ChapterSummaryORM(
            id=f"{book}-{no}", book_name=book, chapter_no=no,
            summary=(summaries or {}).get(no, f"第{no}章概括")))
    test_db.commit()


class TestAssignAliases:
    def test_full_allocation(self):
        cast = {
            "protagonist": "萧炎",
            "entities": [
                {"name": "药老", "kind": "role", "relation": "ally", "desc": "主角的师父"},
                {"name": "云岚宗", "kind": "sect", "relation": "enemy", "desc": "退婚的宗门"},
                {"name": "萧家", "kind": "family", "relation": "ally", "desc": "主角家族"},
                {"name": "纳兰嫣然", "kind": "role", "relation": "enemy", "desc": "退婚者"},
            ],
            "realms": ["斗之气", "斗者", "斗师"],
        }
        rows = az.assign_aliases(cast)
        by = {r["original"]: r for r in rows}
        assert by["萧炎"]["alias"] == "主角"
        assert by["药老"]["alias"] == "友·配角1"
        assert by["纳兰嫣然"]["alias"] == "敌·配角1"      # 敌友分桶独立编号
        assert by["云岚宗"]["alias"] == "敌·宗门1"
        assert by["萧家"]["alias"] == "友·家族1"
        realms = [r["alias"] for r in rows if r["kind"] == "realm"]
        assert realms == ["境界1", "境界2", "境界3"]       # 绝对阶梯
        assert by["药老"]["role_desc"] == "主角的师父"      # 槽位说明（选角用）


class TestExtractCast:
    def test_merge_batches(self, test_db, monkeypatch):
        _mk(test_db, "书", [1, 2])
        monkeypatch.setattr(az, "ds_key", lambda db: "k")
        outs = [
            json.dumps({"protagonist": "萧炎", "entities": [
                {"name": "药老", "kind": "role", "relation": "ally", "desc": "师父"}],
                "realms": ["斗之气", "斗者"]}),
            json.dumps({"protagonist": "", "entities": [
                {"name": "药老", "kind": "role", "relation": "enemy", "desc": "师父（后反目）"},
                {"name": "云岚宗", "kind": "sect", "relation": "enemy", "desc": "宗门"}],
                "realms": ["斗者", "斗师"]}),
        ]
        it = iter(outs)
        monkeypatch.setattr(az, "_ds_post", lambda k, c, **kw: next(it))
        cast = az.extract_cast(test_db, "书", batch_chapters=1)
        assert cast["protagonist"] == "萧炎"
        ent = {e["name"]: e for e in cast["entities"]}
        assert ent["药老"]["relation"] == "enemy"           # 敌对优先（保守合并）
        assert ent["药老"]["desc"] == "师父（后反目）"       # desc 取更长
        assert cast["realms"] == ["斗之气", "斗者", "斗师"]  # 境界按首现顺序合并

    def test_no_key_raises(self, test_db, monkeypatch):
        _mk(test_db, "书", [1])
        monkeypatch.setattr(az, "ds_key", lambda db: None)
        try:
            az.extract_cast(test_db, "书")
            raised = False
        except RuntimeError as e:
            raised = "Key" in str(e)
        assert raised


class TestReplacement:
    def test_replace_and_backup(self, test_db):
        _mk(test_db, "书", [1], {1: "萧炎在云岚宗受辱"})
        az.apply_replacement(test_db, "书", [
            {"original": "萧炎", "alias": "主角"},
            {"original": "云岚宗", "alias": "敌·宗门1"}])
        r = test_db.query(ChapterSummaryORM).one()
        assert r.summary == "主角在敌·宗门1受辱"
        assert r.summary_raw == "萧炎在云岚宗受辱"          # 原文备份

    def test_long_name_first(self, test_db):
        """长名优先：『云岚宗使者』不能被『云岚宗』先截断。"""
        _mk(test_db, "书", [1], {1: "云岚宗使者登门"})
        az.apply_replacement(test_db, "书", [
            {"original": "云岚宗", "alias": "敌·宗门1"},
            {"original": "云岚宗使者", "alias": "敌·宗门1的使者"}])
        r = test_db.query(ChapterSummaryORM).one()
        assert r.summary == "敌·宗门1的使者登门"

    def test_idempotent_raw_kept(self, test_db):
        _mk(test_db, "书", [1], {1: "萧炎在云岚宗受辱"})
        mapping = [{"original": "萧炎", "alias": "主角"}]
        az.apply_replacement(test_db, "书", mapping)
        first_raw = test_db.query(ChapterSummaryORM).one().summary_raw
        az.apply_replacement(test_db, "书", mapping)        # 再跑一遍
        r2 = test_db.query(ChapterSummaryORM).one()
        assert r2.summary_raw == first_raw                  # raw 不被已匿名文本覆盖

    def test_arc_reignite_rescued_by_replacement(self, test_db):
        """弧阶段复燃的补扫（2026-09-14 实测场景，北派「孙家兄弟」/蛊真人「花酒行者」）。

        弧是最后一个 LLM 阶段：它产出的 arc_summary 是**新文本**，从未经过匿名化，
        且 LLM 会在匿名输入上「认出」原著把真名写回。此测试钉住：用**现有映射**
        再跑一次 apply_replacement 必须能清掉 arc_summary 里的真名（零 LLM 成本）。
        """
        _mk(test_db, "书", [1], {1: "主角登场"})
        r = test_db.query(ChapterSummaryORM).one()
        r.arc_summary = "萧炎与云岚宗谈判，要求交出孙家兄弟"      # 复燃文本（含真名）
        test_db.commit()
        rep = az.apply_replacement(test_db, "书", [
            {"original": "萧炎", "alias": "主角"},
            {"original": "云岚宗", "alias": "敌·宗门1"},
            {"original": "孙家兄弟", "alias": "友·配角34"}])
        r2 = test_db.query(ChapterSummaryORM).one()
        assert r2.arc_summary == "主角与敌·宗门1谈判，要求交出友·配角34"
        assert rep["touched"] == 1
        assert r2.arc_summary_raw == "萧炎与云岚宗谈判，要求交出孙家兄弟"  # raw 备份


class TestAnonymizeBook:
    def test_full_pipeline(self, test_db, monkeypatch):
        _mk(test_db, "书", [1, 2], {1: "萧炎与药老", 2: "云岚宗来袭"})
        monkeypatch.setattr(az, "extract_cast", lambda db, book, **kw: {
            "protagonist": "萧炎",
            "entities": [
                {"name": "药老", "kind": "role", "relation": "ally",
                 "desc": "师父", "first_chapter": 1},
                {"name": "云岚宗", "kind": "sect", "relation": "enemy",
                 "desc": "宗门", "first_chapter": 2},
            ],
            "realms": ["斗之气", "斗者"], "chapters": 2})
        st = az.anonymize_book(test_db, "书")
        assert st["aliases"] == 5 and st["touched"] == 2   # 主角1 + 实体2 + 境界2
        rows = test_db.query(ChapterSummaryORM).order_by(
            ChapterSummaryORM.chapter_no).all()
        assert rows[0].summary == "主角与友·配角1"
        assert rows[1].summary == "敌·宗门1来袭"
        assert test_db.query(BookAliasORM).count() == 5

    def test_empty_book_raises(self, test_db, monkeypatch):
        monkeypatch.setattr(az, "extract_cast", lambda db, book, **kw: {
            "protagonist": "", "entities": [], "realms": [], "chapters": 0})
        try:
            az.anonymize_book(test_db, "空书")
            raised = False
        except RuntimeError:
            raised = True
        assert raised, "无概括数据应报错"
