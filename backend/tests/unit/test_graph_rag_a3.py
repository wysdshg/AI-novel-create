# -*- coding: utf-8 -*-
"""A3 GraphRAG 注入协议单测：一跳组装 / 权重排序 / 死亡实体降级 / 名字解析。"""
import uuid

import pytest

from app.models.orm import (
    CharacterORM, EntityRelationORM, FactionORM, ItemORM,
    ProjectORM, RelationTypeORM, SkillORM,
)
from app.services import graph_rag


@pytest.fixture()
def env(test_db):
    db, pid = test_db, "p1"
    db.add(ProjectORM(id=pid, name="GraphRAG 测试"))
    db.commit()

    def ch(name, status=None, role="主角"):
        o = CharacterORM(id=uuid.uuid4().hex, project_id=pid, name=name,
                         role_type=role, status=status or "存活",
                         personality="沉稳", brief="一句话简介",
                         identity="雷家嫡子", function="主角", current_level="筑基")
        db.add(o)
        db.commit()
        return o

    ld = ch("雷动")                                    # 主角（存活）
    ls = ch("雷霄", role="配角")                        # 存活配角
    dead = ch("小四", status="死亡", role="配角")        # 死亡
    sk = SkillORM(id=uuid.uuid4().hex, project_id=pid, name="焚天掌",
                  skill_type="拳法", summary="喷火的掌法", owner_id=ld.id)
    it = ItemORM(id=uuid.uuid4().hex, project_id=pid, name="淬体丹",
                 category="丹药", summary="淬炼肉身")
    fa = FactionORM(id=uuid.uuid4().hex, project_id=pid, name="青云宗",
                    faction_type="宗门", lv=3, stance="正道", summary="正道大宗")
    db.add_all([sk, it, fa])
    db.commit()

    def edge(a, at, b, bt, rtype):
        db.add(EntityRelationORM(id=uuid.uuid4().hex, project_id=pid,
                                 a_id=a, a_type=at, b_id=b, b_type=bt,
                                 relation_type=rtype, created_at=None if False else __import__("datetime").datetime.utcnow()))
    edge(ld.id, "character", sk.id, "skill", "掌握技能")          # w76
    edge(ld.id, "character", it.id, "item", "持有物品")           # w72
    edge(ld.id, "character", fa.id, "faction", "隶属势力")        # w70
    edge(ls.id, "character", fa.id, "faction", "领袖")            # w82
    edge(dead.id, "character", ld.id, "character", "亲属")        # w90
    db.commit()
    return db, pid, {"ld": ld, "ls": ls, "dead": dead, "sk": sk, "it": it, "fa": fa}


def test_resolve_seeds_by_name(env):
    db, pid, o = env
    seeds = graph_rag.resolve_seeds(db, pid, ["雷动", "不存在的名字"])
    assert len(seeds) == 1 and seeds[0]["type"] == "character" and seeds[0]["name"] == "雷动"


def test_assemble_blocks_and_dead_downgrade(env):
    db, pid, o = env
    res = graph_rag.assemble(db, pid, [{"id": o["ld"].id, "type": "character"}])
    inj = res["injection"]
    assert "【出场人物】" in inj and "【相关技能】" in inj
    assert "焚天掌" in inj and "淬体丹" in inj and "青云宗" in inj
    # 死亡实体降级：只有名字+关系+已死亡，不出现人物卡字段
    assert "已死亡" in inj
    assert "小四（亲属，已死亡）" in inj
    assert "沉稳" not in inj.split("【已死亡角色")[1].split("【")[-2] if "【已死亡角色" in inj else True
    # 关系网
    assert "—[" in inj


def test_weight_ordering_deterministic(env):
    db, pid, o = env
    res = graph_rag.assemble(db, pid, [{"id": o["ld"].id, "type": "character"}])
    rels = res["blocks"]["relations"]
    assert rels, "关系块为空"
    ws = []
    for line in rels:
        rtype = line.split("[")[1].split("]")[0]
        row = db.query(RelationTypeORM).filter_by(name=rtype).first()
        ws.append(row.weight if row else 50)
    assert ws == sorted(ws, reverse=True)   # 权重降序


def test_build_injection_silent_on_bad_input(env):
    db, pid, o = env
    assert graph_rag.build_injection(db, pid, names=["不存在的名字"]) == ""
    assert isinstance(graph_rag.build_injection(db, pid, names=["雷动"]), str)
