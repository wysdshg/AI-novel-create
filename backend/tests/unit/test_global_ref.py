"""全局物品/技能库 CRUD 单测（E2）。

覆盖：幂等 add / brief 截断（硬纪律①）/ 非法 category 拒绝 /
别名归一化查找（G4）/ reference_only+disabled 注入硬过滤（硬纪律②）/
尺度库幂等与 genre 回退（通用池）。
"""
import pytest

from app.services import global_ref_crud as gr


def test_add_item_idempotent(test_db):
    a = gr.add_item(test_db, name="筑基丹", category="丹药", brief="辅助突破筑基", genre="仙侠")
    b = gr.add_item(test_db, name="筑基丹", category="丹药", brief="辅助突破筑基", genre="仙侠")
    assert a.id == b.id
    # 同名不同池 = 两条
    gr.add_item(test_db, name="筑基丹", category="丹药", brief="辅助突破筑基", genre="高武")
    assert test_db.query(gr.GlobalItemORM).count() == 2


def test_brief_truncated(test_db):
    """硬纪律①：brief ≤50 字，超长截断不抛错。"""
    row = gr.add_item(test_db, name="X丹", category="丹药", brief="长" * 80, genre="仙侠")
    assert len(row.brief) <= gr.MAX_BRIEF


def test_invalid_category_rejected(test_db):
    with pytest.raises(ValueError):
        gr.add_item(test_db, name="X", category="不存在的类", brief="x")
    with pytest.raises(ValueError):
        gr.add_skill(test_db, name="X", category="丹药", brief="x")   # 丹药是物品类


def test_normalize_lookup_by_alias(test_db):
    """G4 变体归一：别名命中同一条目。"""
    gr.add_item(test_db, name="储物袋", category="法器法宝", brief="内藏空间的收纳法器",
                genre="仙侠", aliases=["乾坤袋", "须弥戒"])
    hit = gr.normalize_lookup(test_db, "乾坤袋")
    assert hit and hit[0] == "item" and hit[1].name == "储物袋"
    assert gr.normalize_lookup(test_db, "不存在的词") is None


def test_inject_excludes_reference_only_and_disabled(test_db):
    """硬纪律②：reference_only / disabled 的行注入层拿不到。"""
    gr.add_item(test_db, name="青云诀物证", category="法器法宝", brief="研究用",
                genre="仙侠", reference_only=True)
    ok = gr.add_item(test_db, name="灵石", category="灵石货币", brief="通用通货", genre="仙侠")
    gr.add_item(test_db, name="禁用条目", category="丹药", brief="已下线", genre="仙侠")
    test_db.query(gr.GlobalItemORM).filter_by(name="禁用条目").update({"status": "disabled"})
    test_db.commit()
    names = [r.name for r in gr.inject_items(test_db, genre="仙侠")]
    assert "灵石" in names
    assert "青云诀物证" not in names
    assert "禁用条目" not in names


def test_inject_genre_fallback_to_general(test_db):
    """genre 过滤回退：请求高武池时，通用池条目也应可见。"""
    gr.add_item(test_db, name="通用铁剑", category="武器", brief="凡兵", genre="通用")
    gr.add_item(test_db, name="飞剑", category="武器", brief="驭使攻击", genre="仙侠")
    names = [r.name for r in gr.inject_items(test_db, category="武器", genre="高武")]
    assert names == ["通用铁剑"]      # 仙侠池条目不漏进高武


def test_skill_crud_and_scale(test_db):
    s = gr.add_skill(test_db, name="御剑术", category="攻击技", brief="驭使飞剑攻敌", genre="仙侠")
    assert s.name == "御剑术"
    sc = gr.add_skill_scale(test_db, category="攻击技", function_pos="单体爆发/群攻",
                            genre="仙侠", grade_axis="黄→玄→地→天")
    again = gr.add_skill_scale(test_db, category="攻击技", function_pos="单体爆发/群攻",
                               genre="仙侠", grade_axis="黄→玄→地→天")
    assert sc.id == again.id      # 尺度库幂等
    rows = gr.inject_scales(test_db, kind="skill", category="攻击技")
    assert len(rows) == 1


def test_stats(test_db):
    gr.add_item(test_db, name="灵石", category="灵石货币", brief="通货", genre="仙侠")
    st = gr.stats(test_db)
    assert st["items"] == 1 and st["skills"] == 0
