# -*- coding: utf-8 -*-
"""区间弧模式（append_mode，2026-09-17 用户拍板 B 方案）的集成回归：

前置：chapters 1~150 已有 15 条弧（每 10 章一条）；151~300 是新摘要。
期望：① 前 14 条弧**原样保留**；② 最后一条旧弧（15，141~150）回炉重切，
     新弧号从 15 续编；③ 区间覆盖 141~300 完整；④ 1~140 一章不动。
"""
import uuid

import pytest

from app.models.orm import ChapterSummaryORM, ProjectORM
from app.services import plot_import as pi


@pytest.fixture()
def book_db(test_db):
    pid = "p1"
    test_db.add(ProjectORM(id=pid, name="区间弧测试"))
    for n in range(1, 301):
        test_db.add(ChapterSummaryORM(
            id=uuid.uuid4().hex, book_name="区间测试",
            chapter_no=n, title=f"第{n}章",
            summary=f"第{n}章的情节概括：主角遭遇了第{n}号事件并解决了它。"))
    test_db.commit()
    return test_db, pid


def _set_arcs(db, pid, lo, hi, arc_size=10, prefix="旧弧"):
    """给 [lo, hi] 的章标弧（每 arc_size 章一条），返回弧号区间。"""
    rows = (db.query(ChapterSummaryORM)
            .filter_by(book_name="区间测试")
            .filter(ChapterSummaryORM.chapter_no >= lo,
                    ChapterSummaryORM.chapter_no <= hi)
            .order_by(ChapterSummaryORM.chapter_no).all())
    first_no = None
    for i, r in enumerate(rows):
        r.arc_no = lo // arc_size if False else (i // arc_size) + 1 + (0 if lo == 1 else 100)
        r.arc_name = f"{prefix}{r.arc_no}"
        if first_no is None:
            first_no = r.arc_no
    db.commit()
    return first_no, rows[0].chapter_no


class _FakePost:
    """按 prompt 类型分流：Pass1 提切点（每窗在 +99 处一刀）；Pass2 出内容。"""
    def __init__(self):
        self.calls = []

    def __call__(self, db, provider, prompt, **kw):
        self.calls.append(prompt)
        if '"cuts"' in prompt:
            import re
            m = re.search(r"第(\d+)~(\d+)章", prompt)
            s, hi = int(m.group(1)), int(m.group(2))
            cuts = [s + 99] if s + 99 < hi else []
            return '{"cuts": [' + ", ".join(str(c) for c in cuts) + ']}'
        import re
        m = re.search(r"第(\d+)章", prompt)
        a0 = int(m.group(1))
        return (f'{{"name": "新弧起于{a0}", "summary": "重切后的弧概括", "beats": []}}')


def test_append_mode_preserves_prefix_and_recuts_tail(test_db, book_db, monkeypatch):
    db, pid = book_db
    # 预置旧弧：1~150 每 10 章一条（15 条）
    rows = (db.query(ChapterSummaryORM)
            .filter_by(book_name="区间测试")
            .filter(ChapterSummaryORM.chapter_no <= 150)
            .order_by(ChapterSummaryORM.chapter_no).all())
    for r in rows:
        r.arc_no = (r.chapter_no - 1) // 10 + 1
        r.arc_name = f"旧弧{r.arc_no}"
    db.commit()
    before = {r.chapter_no: (r.arc_no, r.arc_name)
              for r in rows}

    fake = _FakePost()
    monkeypatch.setattr(pi, "_arc_provider_post", fake)

    st = pi.build_arcs_v3(db, "区间测试", provider="modelscope",
                          core=100, tail=20, force=True, append_mode=True)

    assert st.get("append") is True
    after = {r.chapter_no: (r.arc_no, r.arc_name)
             for r in db.query(ChapterSummaryORM)
             .filter_by(book_name="区间测试").all()}

    # ① 前缀弧原样保留（1~140）
    for n in range(1, 141):
        assert after[n][0] == before[n][0], f"第{n}章的旧弧被动了"
    # ② 最后一条旧弧（141~150，弧15）回炉：不再叫旧弧15
    assert all(after[n][1] != f"旧弧15" for n in range(141, 151))
    # ③ 区间覆盖完整：141~300 每章都有弧
    assert all(after[n][0] is not None for n in range(141, 301))
    # ④ 新弧号从 15 续编（不与保留弧冲突、不重置为 1）
    new_nos = {after[n][0] for n in range(141, 301)}
    assert min(new_nos) == 15
    # ⑤ 保留弧数 14 + 新弧若干 = 全部弧号数
    all_nos = {after[n][0] for n in after}
    assert len(all_nos) == 14 + len([x for x in new_nos if x > 14])
