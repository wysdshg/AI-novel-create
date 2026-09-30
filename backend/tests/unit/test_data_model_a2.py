# -*- coding: utf-8 -*-
"""A2 双写一致性：relation/skill/faction 的写路径同步 entity_relations。"""
import uuid

import pytest

from app.models.orm import CharacterORM, EntityRelationORM, ProjectORM
from app.schemas.database import (
    FactionCreate, FactionUpdate, RelationCreate, RelationUpdate,
    SkillCreate, SkillUpdate,
)
from app.services import (
    character_crud, faction_crud, relation_crud, skill_crud,
)
from app.services.entity_relation_crud import remove_edges_of


@pytest.fixture()
def env(test_db):
    pid = "p1"
    test_db.add(ProjectORM(id=pid, name="A2 双写测试"))
    test_db.commit()
    return test_db, pid


def _edges(db, **kw):
    q = db.query(EntityRelationORM).filter_by(project_id="p1")
    if kw:
        q = q.filter_by(**kw)
    return q.all()


def test_relation_create_update_delete_dual_write(env):
    db, pid = env
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    r = relation_crud.create_relation(db, pid, RelationCreate(
        subject_id=a, object_id=b, relation_type="师徒", strength=90, note="x"))
    edges = _edges(db, a_id=a, b_id=b)
    assert len(edges) == 1 and edges[0].relation_type == "师徒" and edges[0].meta == {"strength": 90}

    # 改类型 → 旧边删、新边建
    relation_crud.update_relation(db, pid, r.id, RelationUpdate(relation_type="仇敌"))
    assert _edges(db, a_id=a, b_id=b, relation_type="师徒") == []
    assert len(_edges(db, a_id=a, b_id=b, relation_type="仇敌")) == 1

    # 删除 → 边也没了
    relation_crud.delete_relation(db, pid, r.id)
    assert _edges(db, a_id=a) == []


def test_skill_owner_edge_follows(env):
    db, pid = env
    owner, new_owner = uuid.uuid4().hex, uuid.uuid4().hex
    s = skill_crud.create_skill(db, pid, SkillCreate(
        name="焚天掌", level="黄阶", effect="喷火", owner_id=owner))
    assert len(_edges(db, a_id=owner, b_id=s.id, relation_type="掌握技能")) == 1
    skill_crud.update_skill(db, pid, s.id, SkillUpdate(owner_id=new_owner))
    assert _edges(db, a_id=owner, b_id=s.id) == []
    assert len(_edges(db, a_id=new_owner, b_id=s.id, relation_type="掌握技能")) == 1
    skill_crud.delete_skill(db, pid, s.id)
    assert _edges(db, b_id=s.id) == []


def test_faction_leader_member_edges(env):
    db, pid = env
    leader, m1, m2 = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    f = faction_crud.create_faction(db, pid, FactionCreate(
        name="青云宗", leader_id=leader, members=[m1]))
    assert len(_edges(db, a_id=leader, b_id=f.id, relation_type="领袖")) == 1
    assert len(_edges(db, a_id=m1, b_id=f.id, relation_type="隶属势力")) == 1
    faction_crud.update_faction(db, pid, f.id, FactionUpdate(members=[m2]))
    assert _edges(db, a_id=m1, b_id=f.id) == []           # 旧成员边被重写
    assert len(_edges(db, a_id=m2, b_id=f.id, relation_type="隶属势力")) == 1
    faction_crud.delete_faction(db, pid, f.id)
    assert _edges(db, b_id=f.id) == []


def test_character_delete_cleans_edges(env):
    db, pid = env
    me, other = uuid.uuid4().hex, uuid.uuid4().hex
    er = EntityRelationORM(id=uuid.uuid4().hex, project_id=pid,
                           a_id=me, a_type="character", b_id=other, b_type="character",
                           relation_type="仇敌")
    db.add(er)
    db.add(CharacterORM(id=me, project_id=pid, name="待删角色"))
    db.commit()
    character_crud.delete_character(db, pid, me)
    assert _edges(db, a_id=me) == []
