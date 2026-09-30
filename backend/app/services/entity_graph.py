"""实体图一跳扩展（A5 GraphRAG）。

解决的问题（实测现状）：`layer_characters` 只把「主角 + 本章要点点名的角色」
渲染成全量设定，`layer_entities` 只把「点名的势力/地点」展开描述——
于是「本章要点提到配角 B，B 的师父 C、B 所属宗门 F」这些**沿关系图一步之遥**
的实体全部被降级成一行简写。作者写「B 回宗门搬救兵」时，AI 手里没有 F 和 C 的设定，
只能现编 → 人设漂移。

做法：不做向量化的社区摘要（那要 LLM 预生成、成本高且本机单人工具没必要），
而是**复用已有四张表当作异构图**，从种子实体走**一跳**，把邻居实体提升为
「本章重点实体」，交给现有 layers 按全量渲染：

    角色 --relations(subject/object)--> 角色        （关系网，带 strength/type）
    角色 --factions.members/leader_id--> 势力       （成员/掌门）
    角色/势力 --locations.related_ids--> 地点        （关联场所）
    势力 --locations.related_ids--> 地点 / 角色      （领地与掌门）

设计约束：
- **一跳，不递归**：两跳会让上下文爆炸且引入无关人物（图会连通到全作）；
- **全部静默降级**：缺表/异常/空数据一律返回原样，绝不阻断生成；
- **确定性排序**：关系强度降序 + 名称，保证同输入同输出（可测试）；
- **可关**：app_config `retrieval.graph_expand`（默认开）。
"""
import logging
from sqlalchemy.orm import Session

from app.models.orm import CharacterORM, EntityRelationORM, FactionORM, LocationORM
from app.services import app_config

KEY_GRAPH_EXPAND = "retrieval.graph_expand"

# 扩展上限：一跳邻居再多也不能把窗口吃光（这些实体要按全量渲染）
MAX_EXTRA_CHARS = 10
MAX_EXTRA_FACTIONS = 8
MAX_EXTRA_LOCATIONS = 8


logger = logging.getLogger(__name__)


def enabled(db: Session) -> bool:
    """总开关（默认开）：app_config `retrieval.graph_expand`。"""
    try:
        return bool(app_config.get(db, KEY_GRAPH_EXPAND, True))
    except Exception as e:  # noqa: BLE001
        # 读配置失败 → 保守开（图谱扩展是增强能力，读不到开关时保持原行为）。
        # 留痕，便于区分「开关被关」与「读配置失败」（Phase 3.5）
        logger.warning(f"[entity_graph] 读取开关 {KEY_GRAPH_EXPAND} 失败，按开启处理: {type(e).__name__}: {e}")
        return True


def expand(
    db: Session,
    project_id: str,
    mentions: dict[str, set[str]],
    *,
    max_chars: int = MAX_EXTRA_CHARS,
    max_factions: int = MAX_EXTRA_FACTIONS,
    max_locations: int = MAX_EXTRA_LOCATIONS,
) -> tuple[dict[str, set[str]], dict]:
    """从种子实体走一跳，返回 (扩展后的 mentions, trace)。

    mentions 形如 {"characters": {...}, "factions": {...}, "locations": {...}}；
    返回同结构（含原种子），trace 记录「谁因为谁被带进来」便于观测与前端口径。
    """
    seed = {
        "characters": set(mentions.get("characters") or set()),
        "factions": set(mentions.get("factions") or set()),
        "locations": set(mentions.get("locations") or set()),
    }
    out = {k: set(v) for k, v in seed.items()}
    trace: dict = {"enabled": True, "seeds": {k: sorted(v) for k, v in seed.items()},
                   "added": {"characters": [], "factions": [], "locations": []}}

    if not any(seed.values()):
        return out, {"enabled": True, "reason": "无种子实体",
                     "added": trace["added"]}

    try:
        chars = db.query(CharacterORM).filter_by(project_id=project_id).all()
        factions = db.query(FactionORM).filter_by(project_id=project_id).all()
        locations = db.query(LocationORM).filter_by(project_id=project_id).all()
        # A6 遗留清理（2026-10-01）：读新表 entity_relations 的角色对边（旧表 relations 已退役）
        relations = (
            db.query(EntityRelationORM)
            .filter_by(project_id=project_id, a_type="character", b_type="character")
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[entity_graph] 读表失败，跳过扩展: {type(e).__name__}: {e}")
        return out, {"enabled": True, "error": f"{type(e).__name__}: {str(e)[:80]}",
                     "added": trace["added"]}

    char_by_id = {c.id: c for c in chars}
    char_by_name = {c.name: c for c in chars}
    seed_char_ids = {char_by_name[n].id for n in seed["characters"] if n in char_by_name}
    seed_char_names = set(seed["characters"])

    # ---------- 1. 角色 → 角色（关系网，双向往返）----------
    # 关系强度高的先纳入：师徒/血亲比「点头之交」更该知道
    neighbor: dict[str, tuple[int, str]] = {}  # id -> (best strength, relation_type)
    for r in relations:
        a, b = r.a_id, r.b_id
        strength = (r.meta or {}).get("strength") or 0
        rtype = r.relation_type or ""
        if a in seed_char_ids and b not in seed_char_ids:
            cur = neighbor.get(b)
            if cur is None or strength > cur[0]:
                neighbor[b] = (strength, rtype)
        elif b in seed_char_ids and a not in seed_char_ids:
            cur = neighbor.get(a)
            if cur is None or strength > cur[0]:
                neighbor[a] = (strength, rtype)

    ranked = sorted(
        neighbor.items(),
        key=lambda kv: (-kv[1][0], char_by_id.get(kv[0]).name if char_by_id.get(kv[0]) else kv[0]),
    )
    for cid, (strength, rtype) in ranked:
        if len(out["characters"]) - len(seed["characters"]) >= max_chars:
            break
        c = char_by_id.get(cid)
        if c is None or not c.name:
            continue
        out["characters"].add(c.name)
        trace["added"]["characters"].append(
            {"name": c.name, "via": "关系", "strength": strength, "relation": rtype})

    # 角色 → 势力（成员 / 掌门）
    def _faction_has_member(f: FactionORM) -> bool:
        for m in (f.members or []):
            s = str(m).strip()
            if not s:
                continue
            if s in seed_char_names:
                return True
            c = char_by_name.get(s) or char_by_id.get(s)
            if c is not None and c.id in seed_char_ids:
                return True
        return bool(f.leader_id and f.leader_id in seed_char_ids)

    added_faction_ids: set[str] = set()
    for f in factions:
        if len(out["factions"]) - len(seed["factions"]) >= max_factions:
            break
        if not f.name or f.name in out["factions"]:
            continue
        if _faction_has_member(f):
            out["factions"].add(f.name)
            added_faction_ids.add(f.id)
            trace["added"]["factions"].append({"name": f.name, "via": "角色所属势力"})

    # 势力 → 角色（掌门）：种子势力/刚纳入势力的掌门也是重点人物
    for f in factions:
        if f.name not in out["factions"] or not f.leader_id:
            continue
        lead = char_by_id.get(f.leader_id)
        if lead is None or not lead.name or lead.name in out["characters"]:
            continue
        if len(out["characters"]) - len(seed["characters"]) >= max_chars:
            break
        out["characters"].add(lead.name)
        trace["added"]["characters"].append(
            {"name": lead.name, "via": f"势力《{f.name}》掌门"})

    # 角色/势力 → 地点（related_ids）
    focus_ids = (seed_char_ids
                 | {char_by_name[n].id for n in out["characters"] if n in char_by_name}
                 | {f.id for f in factions if f.name in out["factions"]})
    for l in locations:
        if len(out["locations"]) - len(seed["locations"]) >= max_locations:
            break
        if not l.name or l.name in out["locations"]:
            continue
        if set(l.related_ids or []) & focus_ids:
            out["locations"].add(l.name)
            trace["added"]["locations"].append({"name": l.name, "via": "实体关联地点"})

    return out, trace
