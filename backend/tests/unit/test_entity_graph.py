"""A5 实体图一跳扩展单测。

不触网、不依赖模型：纯表驱动。关键契约：
- 角色 --关系--> 角色（双向、按强度降序、上限截断）；
- 角色 --成员/掌门--> 势力；势力 --掌门--> 角色；
- 角色/势力 --related_ids--> 地点；
- 幂等（种子不重复计入）、无种子/异常/开关关闭 均静默返回原样。
"""
import pytest

from app.models.orm import CharacterORM, EntityRelationORM, FactionORM, LocationORM
from app.services import entity_graph


def _char(db, pid, cid, name, role="配角"):
    db.add(CharacterORM(id=cid, project_id=pid, name=name, role_type=role))
    db.commit()
    return cid


def _faction(db, pid, fid, name, members=None, leader_id=None, territory=None):
    db.add(FactionORM(id=fid, project_id=pid, name=name,
                      members=members or [], leader_id=leader_id, territory=territory))
    db.commit()
    return fid


def _loc(db, pid, lid, name, related_ids=None):
    db.add(LocationORM(id=lid, project_id=pid, name=name, related_ids=related_ids or []))
    db.commit()
    return lid


def _rel(db, pid, rid, s, o, rtype="师徒", strength=50):
    # A6 遗留清理：一跳扩展已改读新表 entity_relations
    db.add(EntityRelationORM(id=rid, project_id=pid, a_id=s, a_type="character",
                             b_id=o, b_type="character", relation_type=rtype,
                             meta={"strength": strength}))
    db.commit()


def _mentions(p="p1", chars=None, factions=None, locations=None):
    return {"characters": set(chars or []), "factions": set(factions or []),
            "locations": set(locations or [])}


def test_no_seed_returns_unchanged(test_db):
    _char(test_db, "p1", "c1", "陈默")
    out, trace = entity_graph.expand(test_db, "p1", _mentions())
    assert out == {"characters": set(), "factions": set(), "locations": set()}
    assert trace["added"] == {"characters": [], "factions": [], "locations": []}


def test_character_relation_one_hop(test_db):
    _char(test_db, "p1", "c1", "陈默", role="主角")
    _char(test_db, "p1", "c2", "苏清月")
    _char(test_db, "p1", "c3", "陆离")
    _rel(test_db, "p1", "r1", "c1", "c2", "道侣", 90)
    _rel(test_db, "p1", "r2", "c1", "c3", "宿敌", 60)

    out, trace = entity_graph.expand(test_db, "p1", _mentions(chars=["陈默"]))
    assert out["characters"] == {"陈默", "苏清月", "陆离"}
    # 强度降序：道侣(90) 在前
    added = [a["name"] for a in trace["added"]["characters"]]
    assert added == ["苏清月", "陆离"]
    assert trace["added"]["characters"][0]["strength"] == 90


def test_relation_is_bidirectional(test_db):
    """种子是关系的 object 端时，source 端也要被带进来。"""
    _char(test_db, "p1", "c1", "陈默")
    _char(test_db, "p1", "c2", "苏清月")
    _rel(test_db, "p1", "r1", "c1", "c2", "师徒", 80)
    out, _ = entity_graph.expand(test_db, "p1", _mentions(chars=["苏清月"]))
    assert "陈默" in out["characters"]


def test_faction_by_member_name_and_leader_id(test_db):
    """成员写名字、或成员是角色 id、或是掌门 —— 三种都能带出势力。"""
    _char(test_db, "p1", "c1", "陈默")
    _char(test_db, "p1", "c2", "苏清月")
    _char(test_db, "p1", "c3", "玄真子")
    _faction(test_db, "p1", "f1", "青云门", members=["陈默"])          # 名字
    _faction(test_db, "p1", "f2", "太玄宗", members=["c2"])            # id
    _faction(test_db, "p1", "f3", "天剑阁", leader_id="c3")            # 掌门

    out, trace = entity_graph.expand(
        test_db, "p1", _mentions(chars=["陈默", "苏清月", "玄真子"]))
    assert out["factions"] == {"青云门", "太玄宗", "天剑阁"}


def test_faction_leader_promoted_to_character(test_db):
    """种子势力 → 掌门被提升为重点角色。"""
    _char(test_db, "p1", "c9", "玄真子")
    _faction(test_db, "p1", "f1", "青云门", leader_id="c9")
    out, trace = entity_graph.expand(test_db, "p1", _mentions(factions=["青云门"]))
    assert "玄真子" in out["characters"]
    assert trace["added"]["characters"][0]["via"] == "势力《青云门》掌门"


def test_location_via_related_ids(test_db):
    _char(test_db, "p1", "c1", "陈默")
    _faction(test_db, "p1", "f1", "青云门", members=["陈默"])
    _loc(test_db, "p1", "l1", "青云山", related_ids=["c1"])
    _loc(test_db, "p1", "l2", "太玄秘境", related_ids=["f1"])
    _loc(test_db, "p1", "l3", "无关城", related_ids=["zzz"])

    out, _ = entity_graph.expand(test_db, "p1", _mentions(chars=["陈默"]))
    assert out["locations"] == {"青云山", "太玄秘境"}


def test_caps_are_enforced(test_db):
    for i in range(15):
        _char(test_db, "p1", f"c{i}", f"角色{i}")
    for i in range(15):
        _rel(test_db, "p1", f"r{i}", "c0", f"c{i}", "同门", 50 - i)

    out, _ = entity_graph.expand(test_db, "p1", _mentions(chars=["角色0"]),
                                 max_chars=5)
    # 种子 1 + 最多 5 个邻居
    assert len(out["characters"]) == 6


def test_seed_not_duplicated(test_db):
    _char(test_db, "p1", "c1", "陈默", role="主角")
    _char(test_db, "p1", "c2", "苏清月")
    _rel(test_db, "p1", "r1", "c1", "c2", "道侣", 90)
    out, trace = entity_graph.expand(test_db, "p1",
                                     _mentions(chars=["陈默", "苏清月"]))
    assert out["characters"] == {"陈默", "苏清月"}
    assert trace["added"]["characters"] == []  # 都已是种子


def test_single_char_name_skipped(test_db):
    """单字名在 extract_mentions 阶段就被跳过，扩展也不应引入异常。"""
    _char(test_db, "p1", "c1", "陈默")
    _char(test_db, "p1", "c2", "王")
    _rel(test_db, "p1", "r1", "c1", "c2", "同门", 50)
    out, _ = entity_graph.expand(test_db, "p1", _mentions(chars=["陈默"]))
    assert out["characters"] == {"陈默", "王"}  # 关系存在则照常带入，不额外过滤


def test_disabled_by_config(test_db, monkeypatch):
    from app.services import app_config
    monkeypatch.setattr(entity_graph, "enabled", lambda db: False)
    _char(test_db, "p1", "c1", "陈默")
    _char(test_db, "p1", "c2", "苏清月")
    _rel(test_db, "p1", "r1", "c1", "c2", "道侣", 90)
    # enabled 为 False 时由调用方（builder）短路，expand 本身不做开关判断
    assert entity_graph.enabled(test_db) is False


def test_db_error_silently_returns_original(test_db, monkeypatch):
    """读表异常 → 返回原种子，不抛。"""
    def boom(*a, **k):
        raise RuntimeError("模拟 DB 异常")
    monkeypatch.setattr(test_db, "query", boom)
    out, trace = entity_graph.expand(test_db, "p1", _mentions(chars=["陈默"]))
    assert out["characters"] == {"陈默"}
    assert "error" in trace
