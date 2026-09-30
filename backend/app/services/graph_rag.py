# -*- coding: utf-8 -*-
"""GraphRAG 注入协议（docs/09 §3，阶段 A3）。

    种子（名字/ID）→ 一跳（entity_relations，按 relation_types.weight 排序）
    → 分块组装（人物卡前置 → 技能物品 → 势力地点 → 关系摘要）
    → 死亡实体只注入「名字 + 关系 + 已死亡」。

约定：
- **模板套路参考是独立一条链**（模板库检索），本模块不拼它——两条链在调用方分别注入；
- **全部静默降级**（铁律 15）：任何异常返回空块，绝不阻断生成；
- 确定性排序：weight 降序 → 名称，同输入同输出。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.orm import (
    CharacterORM, EntityRelationORM, FactionORM, ItemORM,
    LocationORM, RelationTypeORM, SkillORM,
)

logger = logging.getLogger(__name__)

# 实体类型 → ORM 与名字字段
_TYPE_MODEL = {
    "character": (CharacterORM, "name"),
    "skill": (SkillORM, "name"),
    "item": (ItemORM, "name"),
    "faction": (FactionORM, "name"),
    "location": (LocationORM, "name"),
}
DEAD_STATUSES = {"死亡", "陨落", "已死亡"}


KEY_GRAPH_RAG = "retrieval.graph_rag"


def enabled(db: Session) -> bool:
    """总开关（默认开）：app_config `retrieval.graph_rag`。读不到配置按开启处理（增强能力）。"""
    try:
        from app.services import app_config
        return bool(app_config.get(db, KEY_GRAPH_RAG, True))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph_rag] 读开关失败按开启: {type(e).__name__}: {e}")
        return True


def _weights(db: Session) -> dict[str, int]:
    try:
        return {r.name: r.weight for r in db.query(RelationTypeORM).all()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph_rag] 读关系权重失败（用默认 50）: {type(e).__name__}: {e}")
        return {}


def resolve_seeds(db: Session, project_id: str, names: list[str]) -> list[dict]:
    """按名字在五张实体表里精确匹配 → [{id, type, name}]（同名的多类型都返回）。"""
    out = []
    for etype, (model, name_field) in _TYPE_MODEL.items():
        if not names:
            continue
        col = getattr(model, name_field)
        for row in db.query(model).filter_by(project_id=project_id).filter(col.in_(names)).all():
            out.append({"id": row.id, "type": etype, "name": (row.name or "").strip()})
    return out


def _entity_line(etype: str, row, meta_note: str | None = None) -> str:
    """单个实体的注入行（紧凑、一行一实体）。"""
    if etype == "character":
        parts = [p for p in (f"身份：{row.identity}" if row.identity else None,
                             f"作用：{row.function}" if row.function else None,
                             f"境界：{row.current_level}" if row.current_level else None) if p]
        head = f"{row.name}（{row.role_type or '角色'}{'｜' + '｜'.join(parts) if parts else ''}）"
        tail = "｜".join(x for x in (row.personality, row.brief) if x)
        return f"- {head}{('：' + tail) if tail else ''}"
    if etype == "skill":
        return f"- {row.name}（{row.skill_type or '功法/技能'}）{('：' + (row.summary or row.effect or '')) if (row.summary or row.effect) else ''}"
    if etype == "item":
        return f"- {row.name}（{row.category or '物品'}{'｜唯一' if row.is_unique else ''}）{('：' + (row.summary or '')) if row.summary else ''}"
    if etype == "faction":
        bits = [b for b in (row.faction_type, f"Lv{row.lv}" if row.lv else None, row.stance) if b]
        return f"- {row.name}（{'｜'.join(bits) if bits else '势力'}）{('：' + (row.summary or row.description or '')) if (row.summary or row.description) else ''}"
    if etype == "location":
        return f"- {row.name}（{row.location_type or '地点'}）{('：' + (row.summary or row.description or '')) if (row.summary or row.description) else ''}"
    return f"- {row.name}"


def assemble(db: Session, project_id: str, seeds: list[dict], *,
             max_edges: int = 80) -> dict:
    """一跳 + 分块组装。`seeds = [{id, type}]`（可用 resolve_seeds 产出）。"""
    blocks: dict[str, list[str]] = {"characters": [], "skills": [], "items": [],
                                    "factions": [], "locations": [], "relations": []}
    dead: list[str] = []
    seen_edges = 0
    try:
        seed_ids = {s["id"] for s in seeds if s.get("id")}
        if not seed_ids:
            return {"blocks": blocks, "injection": "", "edges": 0, "dead": []}

        weights = _weights(db)
        edges = (db.query(EntityRelationORM)
                 .filter_by(project_id=project_id)
                 .filter((EntityRelationORM.a_id.in_(seed_ids)) | (EntityRelationORM.b_id.in_(seed_ids)))
                 .all())
        # 权重排序（确定性：weight 降序 → 创建时间）
        edges.sort(key=lambda e: (-weights.get(e.relation_type, 50), e.created_at or datetime.utcnow()))
        edges = edges[:max_edges]
        seen_edges = len(edges)

        # 收集邻居（种子本身也进人物卡）
        chars = {c.id: c for c in db.query(CharacterORM).filter_by(project_id=project_id)
                 .filter(CharacterORM.id.in_(seed_ids)).all()}
        other_ids: dict[str, set[str]] = {"skill": set(), "item": set(),
                                          "faction": set(), "location": set()}
        rel_lines: list[tuple[int, str]] = []
        char_names = {cid: c.name for cid, c in chars.items()}

        def _name_of(etype: str, eid: str) -> str:
            if etype == "character" and eid in char_names:
                return char_names[eid]
            m = _TYPE_MODEL.get(etype)
            if not m:
                return eid[:8]
            row = db.query(m[0]).filter_by(id=eid).first()
            return (getattr(row, "name", None) or eid[:8]) if row else eid[:8]

        for e in edges:
            w = weights.get(e.relation_type, 50)
            an, bn = _name_of(e.a_type, e.a_id), _name_of(e.b_type, e.b_id)
            rel_lines.append((w, f"- {an} —[{e.relation_type}]→ {bn}"
                                 f"{'（' + e.note + '）' if e.note else ''}"))
            # 邻居归类（种子对端）
            for side_id, side_type in ((e.a_id, e.a_type), (e.b_id, e.b_type)):
                if side_id in seed_ids or side_type == "character":
                    if side_type == "character" and side_id not in chars:
                        row = db.query(CharacterORM).filter_by(id=side_id).first()
                        if row:
                            chars[side_id] = row
                    continue
                if side_type in other_ids:
                    other_ids[side_type].add(side_id)

        # 人物块：存活给卡，死亡只给「名字+关系+已死亡」
        for cid, c in chars.items():
            if (c.status or "") in DEAD_STATUSES:
                rels = [e.relation_type for e in edges
                        if cid in (e.a_id, e.b_id)][:2] or ["相关"]
                dead.append(f"- {c.name}（{'/'.join(dict.fromkeys(rels))}，已死亡）")
                continue
            blocks["characters"].append(_entity_line("character", c))

        # 技能 / 物品 / 势力 / 地点块
        for etype, key in (("skill", "skills"), ("item", "items"),
                           ("faction", "factions"), ("location", "locations")):
            model = _TYPE_MODEL[etype][0]
            ids = other_ids[etype]
            if not ids:
                continue
            rows = db.query(model).filter_by(project_id=project_id).filter(model.id.in_(ids)).all()
            for row in rows:
                blocks[key].append(_entity_line(etype, row))

        blocks["relations"] = [txt for _, txt in sorted(rel_lines, key=lambda x: -x[0])]
        blocks["dead"] = dead  # type: ignore[assignment]
    except Exception as e:  # noqa: BLE001 - 增强失败不阻断
        logger.warning(f"[graph_rag] 组装失败（返回空块）: {type(e).__name__}: {e}")

    injection = _render(blocks)
    return {"blocks": blocks, "injection": injection, "edges": seen_edges, "dead": dead}


def _render(blocks: dict) -> str:
    """分块组装注入文本（出场人物前置）。"""
    parts: list[str] = []
    if blocks.get("characters"):
        parts.append("【出场人物】\n" + "\n".join(blocks["characters"]))
    if blocks.get("dead"):
        parts.append("【已死亡角色（只作提及，不可复活、不可再行动）】\n" + "\n".join(blocks["dead"]))
    if blocks.get("skills"):
        parts.append("【相关技能】\n" + "\n".join(blocks["skills"]))
    if blocks.get("items"):
        parts.append("【相关物品】\n" + "\n".join(blocks["items"]))
    if blocks.get("factions"):
        parts.append("【相关势力】\n" + "\n".join(blocks["factions"]))
    if blocks.get("locations"):
        parts.append("【相关地点】\n" + "\n".join(blocks["locations"]))
    if blocks.get("relations"):
        parts.append("【关系网（一跳）】\n" + "\n".join(blocks["relations"][:40]))
    return "\n\n".join(parts)


def build_injection(db: Session, project_id: str, *, names: list[str] | None = None,
                    seed_ids: list[dict] | None = None, max_edges: int = 80) -> str:
    """便捷入口：按名字或 ID 组装注入文本。任何失败返回空串。"""
    try:
        seeds = seed_ids or resolve_seeds(db, project_id, names or [])
        return assemble(db, project_id, seeds, max_edges=max_edges)["injection"]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph_rag] build_injection 失败: {type(e).__name__}: {e}")
        return ""
