# -*- coding: utf-8 -*-
"""幂等生成保护（2026-09-17）：同篇内同章号 / 同标题 → find_existing_chapter 命中，
生成落库走覆盖而非新建（治"废脉异变×2"）。跨篇同名不算重复。
"""
import uuid

import pytest

from app.models.orm import ArticleORM, ChapterORM, ProjectORM, VolumeORM
from app.services import chapter_crud


@pytest.fixture()
def env(test_db):
    pid = "p1"
    test_db.add_all([
        ProjectORM(id=pid, name="幂等测试"),
        VolumeORM(id="v1", project_id=pid, name="卷一"),
        ArticleORM(id="a1", project_id=pid, volume_id="v1", name="第一篇"),
        ArticleORM(id="a2", project_id=pid, volume_id="v1", name="第二篇"),
    ])
    test_db.commit()
    return test_db, pid


def _mk_chapter(db, pid, aid, no, title, content="正文" * 100):
    o = ChapterORM(id=uuid.uuid4().hex, project_id=pid, article_id=aid,
                   chapter_no=no, title=title, content=content,
                   word_count=len(content))
    db.add(o)
    db.commit()
    return o


def test_hit_by_same_title_in_same_article(env):
    db, pid = env
    o = _mk_chapter(db, pid, "a1", 3, "废脉异变")
    hit = chapter_crud.find_existing_chapter(db, pid, "a1", None, "废脉异变")
    assert hit is not None and hit.id == o.id


def test_hit_by_same_chapter_no(env):
    db, pid = env
    o = _mk_chapter(db, pid, "a1", 3, "废脉异变")
    hit = chapter_crud.find_existing_chapter(db, pid, "a1", 3, None)
    assert hit is not None and hit.id == o.id


def test_miss_when_no_duplicate(env):
    db, pid = env
    _mk_chapter(db, pid, "a1", 3, "废脉异变")
    assert chapter_crud.find_existing_chapter(db, pid, "a1", 9, "暗流涌动") is None


def test_cross_article_same_title_is_not_duplicate(env):
    """跨篇同名是正常现象（两卷各有一章「夜谈」）——只在同一篇内查。"""
    db, pid = env
    _mk_chapter(db, pid, "a1", 3, "夜谈")
    assert chapter_crud.find_existing_chapter(db, pid, "a2", 3, "夜谈") is None
