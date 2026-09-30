# -*- coding: utf-8 -*-
"""A1 数据模型单测：关系字典种子 / entity_relation 幂等 / 迁移幂等 / 等级标准。"""
import uuid

import pytest

from app.models.orm import (
    EntityRelationORM, FactionORM, LvStandardORM, ProjectORM,
    RelationORM, RelationTypeORM, SkillORM,
)
from app.services import entity_relation_crud as er


@pytest.fixture()
def env(test_db):
    pid = "p1"
    test_db.add(ProjectORM(id=pid, name="数据模型测试"))
    test_db.commit()
    return test_db, pid


def test_seed_relation_types(env):
    db, pid = env
    n = er.seed_relation_types(db)
    assert n >= 15
    # 幂等：再跑一遍不新增
    assert er.seed_relation_types(db) == 0
    st = db.query(RelationTypeORM).filter_by(name="师徒").first()
    assert st.symmetric is False and st.inverse == "弟子"
    en = db.query(RelationTypeORM).filter_by(name="仇敌").first()
    assert en.symmetric is True


def test_upsert_edge_idempotent(env):
    db, pid = env
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    r1, c1 = er.upsert_edge(db, pid, a, "character", b, "character", "师徒", meta={"since": 1})
    assert c1 is True
    r2, c2 = er.upsert_edge(db, pid, a, "character", b, "character", "师徒", meta={"since": 3})
    assert c2 is False and r2.id == r1.id
    assert r2.meta == {"since": 3}
    assert db.query(EntityRelationORM).count() == 1


def test_neighbors_both_directions(env):
    db, pid = env
    me, x, y = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    er.upsert_edge(db, pid, me, "character", x, "skill", "掌握技能")
    er.upsert_edge(db, pid, y, "character", me, "character", "仇敌")
    ids = {e.id for e in er.neighbors(db, pid, me)}
    assert len(ids) == 2


def test_migrate_legacy_idempotent(env):
    """旧 relations / skills.owner_id / factions.leader_id+members → 边；跑两遍结果一致。"""
    db, pid = env
    c1, c2, c3 = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    sk = SkillORM(id=uuid.uuid4().hex, project_id=pid, name="焚天掌", owner_id=c1)
    fa = FactionORM(id=uuid.uuid4().hex, project_id=pid, name="青云宗",
                    leader_id=c2, members=[c1, c3])
    rel = RelationORM(id=uuid.uuid4().hex, project_id=pid, subject_id=c1,
                      object_id=c2, relation_type="仇敌", strength=80)
    db.add_all([sk, fa, rel])
    db.commit()

    er.migrate_legacy(db, pid)
    n1 = db.query(EntityRelationORM).count()
    assert n1 == 5  # 仇敌1 + 掌握技能1 + 领袖1 + 隶属2
    er.migrate_legacy(db, pid)
    assert db.query(EntityRelationORM).count() == n1   # 幂等
    # 领袖边存在
    assert (db.query(EntityRelationORM)
            .filter_by(a_id=c2, b_id=fa.id, relation_type="领袖").count()) == 1


def test_lv_standard_unique(env):
    db, pid = env
    db.add(LvStandardORM(id=uuid.uuid4().hex, project_id=pid, kind="force",
                         lv=2, title="一方豪强", desc="势力最强者为筑基高手"))
    db.commit()
    db.add(LvStandardORM(id=uuid.uuid4().hex, project_id=pid, kind="force",
                         lv=2, title="重复", desc=None))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()
