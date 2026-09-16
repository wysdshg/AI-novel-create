"""08-B11：A5 实体图一跳扩展接入**商讨路径**。

此前 `entity_graph.expand` 只挂在 `build_chapter_messages`（builder.py:184），
商讨的 `build_discussion_system` 不接 → 作者问「张三的师父是谁」，师父在角色层
只是一行简写，顾问只能现编。

本组用例（真实 SQL + 真实 expand，无 mock）：
1. query 提到张三 → 关系网一跳把师父李四带进 focus → 李四**全量**渲染（背景出现在 system）
2. 未扩展的无关角色王五保持简写（背景不出现）
3. query_text 为空 → 不提取不扩展（与旧版行为一致）
4. 开关关闭（`retrieval.graph_expand=False`）→ trace 明示关闭、不扩展
"""
import pytest

from app.core.context.builder import build_discussion_system
from app.models.orm import CharacterORM, RelationORM
from app.services import app_config, entity_graph

PID = "p-graph"

BG_LISI = "李四是张三的授业恩师，隐居青云峰二十年，掌握失传的青云剑诀全套心法。"
BG_WANGWU = "王五是个与主线毫无瓜葛的散修，常年在城南摆摊卖符箓和低阶丹药为生。"


@pytest.fixture()
def seeded(test_db):
    test_db.add_all([
        CharacterORM(id="c1", project_id=PID, name="张三", role_type="主角",
                     personality="坚毅", brief="采药少年"),
        CharacterORM(id="c2", project_id=PID, name="李四", role_type="配角",
                     background=BG_LISI, personality="严厉"),
        CharacterORM(id="c3", project_id=PID, name="王五", role_type="配角",
                     background=BG_WANGWU, personality="随和"),
    ])
    test_db.add(RelationORM(id="r1", project_id=PID, subject_id="c1", object_id="c2",
                            relation_type="师徒", strength=8))
    test_db.commit()
    return test_db


def test_discussion_expands_graph(seeded):
    """query 提到张三 → 关系网一跳把李四带进 focus → 李四全量渲染（背景入 system）。"""
    system, meta = build_discussion_system(seeded, PID, query_text="张三的师父是谁？")

    assert BG_LISI in system, "李四被图扩展进 focus 后应全量渲染（背景出现在 system）"
    assert BG_WANGWU not in system, "未扩展的王五应保持简写（背景不出现）"
    added = meta["entity_graph"].get("added", {}).get("characters", [])
    assert any(a.get("name") == "李四" for a in added), f"trace 应记录李四经关系加入: {added}"


def test_discussion_no_query_keeps_old_behavior(seeded):
    """query_text 为空 → 不提取、不扩展（与 08-B11 之前的行为完全一致）。"""
    system, meta = build_discussion_system(seeded, PID)

    assert BG_LISI not in system, "无 query 就没有种子 → 不扩展 → 李四保持简写"
    assert meta["entity_graph"] == {}, f"trace 应为空 dict: {meta.get('entity_graph')}"


def test_discussion_graph_disabled(seeded):
    """开关 `retrieval.graph_expand=False` → 不扩展，trace 明示关闭（与章节路径同语义）。"""
    app_config.set_value(seeded, entity_graph.KEY_GRAPH_EXPAND, False)

    system, meta = build_discussion_system(seeded, PID, query_text="张三的师父是谁？")

    assert meta["entity_graph"] == {"enabled": False, "reason": "配置关闭"}
    assert BG_LISI not in system, "开关关闭 → 李四保持简写"


def test_discussion_query_without_known_entity(seeded):
    """query 全是不认识的词 → 无种子 → expand 走「无种子」早退，不炸。"""
    system, meta = build_discussion_system(seeded, PID, query_text="今天天气怎么样？")

    assert BG_LISI not in system
    assert meta["entity_graph"].get("enabled") is True
    assert meta["entity_graph"].get("reason") == "无种子实体"
