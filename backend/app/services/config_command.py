"""配置对话：把用户的自然语言设定描述，交给 LLM 抽取为结构化实体，
自动 upsert 到资料库（角色 / 势力 / 地点 / 关系网）。

流程：
1. 取默认模型配置；若无可用模型，返回友好提示（不调用 LLM）。
2. 组装 system + user 提示，要求模型只输出约定 JSON。
3. adapter.chat() 同步拿全文，剥离 ```json 围栏后解析。
4. 依次处理 characters / factions / locations（按 name upsert），
   再处理 relations（用角色姓名解析为 character id，跳过重复/无法解析）。
5. 返回 {reply, changes, model_ok}，changes 是四类实体的变更明细。
"""
import logging
import json
import math
import re
import time

from sqlalchemy.orm import Session

from app.models.orm import CharacterORM, EntityRelationORM, FactionORM, LocationORM
from app.schemas.database import (
    CharacterCreate, CharacterUpdate,
    FactionCreate, FactionUpdate,
    LocationCreate, LocationUpdate,
    RelationCreate,
)
from app.services import (
    character_crud, faction_crud, location_crud, relation_crud, model_crud, usage_crud,
)
from app.core.gateway.registry import get_adapter
from app.core.response import ok

SYSTEM_PROMPT = """你是一名小说设定整理助手。用户会用自然语言描述一段剧情或世界观设定，
你需要从中抽取结构化实体，并严格按照下面的 JSON 格式输出（不要输出任何多余文字、不要使用 markdown 代码块）。

{
  "reply": "用一句中文向用户说明你整理并写入了哪些设定",
  "characters": [
    {"name":"必填","role_type":"主角/配角/反派/其他","gender":"男/女（仅填单个字）","age":整数或null,"personality":"性格描述","background":"背景描述","talent":"天赋","current_level":"等级/境界","skills":["技能1","技能2"],"relationship_network":["与XX的关系"],"brief":"一句话简介"}
  ],
  "factions": [
    {"name":"必填","description":"势力简介","status":"活跃/衰落/隐秘/其他"}
  ],
  "locations": [
    {"name":"必填","location_type":"城市/门派/秘境/洞府/其他","region":"所属区域","plane":"凡间/仙界/秘境(可自定义)","description":"描述","notable_features":["特色1","特色2"]}
  ],
  "relations": [
    {"subject":"角色A姓名","object":"角色B姓名","relation_type":"友好/敌对/亲人/师徒/上下级/暧昧/其他","strength":0到100的整数,"note":"特殊恩怨备注"}
  ]
}

字段格式硬性规则（违反将导致数据错误）：
- gender 只能是「男」或「女」或「其他」单个值，禁止塞入描述文字、身份、长句子。
- role_type 只能是「主角」「配角」「反派」「其他」之一。
- age 必须是整数数字（如 18、24），不能是文字。
- personality / background / brief 才是放描述性文字的字段。
- 若某字段无法从文本中推断，填 null，不要编造。

规则：
- 只抽取文本中明确出现或可由上下文可靠推断的实体，不要编造。
- relations 里的 subject / object 必须是 characters 中出现过的角色姓名。
- 若文本没有可抽取的设定，返回 {"reply":"...", "characters":[], "factions":[], "locations":[], "relations":[]}。
"""

_CHAR_FIELDS = ("role_type", "gender", "age", "personality", "background",
                "talent", "current_level", "brief")
_LIST_CHAR_FIELDS = ("skills", "relationship_network")
_LIST_FIELDS = ("skills", "relationship_network", "notable_features", "members")


logger = logging.getLogger(__name__)


def _as_list(v):
    """把模型可能返回的字符串/列表规范成字符串列表。"""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[，,、\n]", v) if s.strip()]
    return [str(v)]


def _extract_json(raw):
    if not raw:
        return None
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except Exception as e:
        # 模型返回的不是合法 JSON → 返回 None，调用方降级为"无变更"。
        # 这是**最常见的模型行为偏差**（多余逗号/中文引号/截断），必须留痕并带上下文，
        # 否则用户看到"指令没生效"时无从判断是模型没输出还是解析失败（Phase 3.5）
        logger.warning(
            f"[config_command] 模型返回的 JSON 解析失败（降级为无变更）: "
            f"{type(e).__name__}: {e}; 片段={s[start:start + 200]!r}"
        )
        return None


def _empty_changes():
    return {"characters": [], "factions": [], "locations": [], "relations": []}


def _sanitize_characters(data: dict) -> None:
    """纠正 LLM 常犯的字段值混淆错误（如把整段描述塞入 gender）。

    对 data["characters"] 就地修改，不返回新对象。
    """
    _GENDER_VALUES = {"男", "女", "其他"}
    _ROLE_TYPE_VALUES = {"主角", "配角", "反派", "其他"}

    for c in (data.get("characters") or []):
        # gender 超过 2 字符 → 模型塞了描述文字，清空让用户/brief 承担
        g = (c.get("gender") or "").strip()
        if g and g not in _GENDER_VALUES:
            logger.warning(f"[config_command] gender 字段异常({g!r})，已清除")
            c.pop("gender", None)

        # role_type 不在白名单 → 清除
        rt = (c.get("role_type") or "").strip()
        if rt and rt not in _ROLE_TYPE_VALUES:
            logger.warning(f"[config_command] role_type 字段异常({rt!r})，已清除")
            c.pop("role_type", None)

        # age 不是整数 → 尝试提取数字，失败则清除
        a = c.get("age")
        if a is not None:
            try:
                c["age"] =int(str(a).strip())
            except (ValueError, TypeError):
                logger.warning(f"[config_command] age 字段异常({a!r})，已清除")
                c.pop("age", None)


def _archetype_hints(db: Session, text: str, top_k: int = 3) -> str:
    """**角色立体化 S1**（2026-09-17 实现）：建卡时从角色原型库召回 top-k 原型注入 prompt。

    原型库 = 已完成小说模板的 cast 槽位（`char_archetype` 三路索引之一），
    每块形如「槽位｜位阶：…｜性格：利他+4·信义+7·…｜功能描述｜定位」。
    给 AI 的是**参考**：功能定位 / 位阶组合 / 性格强度怎么落，**不是让它照抄某个具体角色**。

    🔴 任何失败都返回 "" —— 原型库是增强项，**绝不能阻断建卡主流程**（与向量检索同款降级约定）。
    """
    try:
        from app.services import vector_index
        from app.services import plot_template_crud as tpl_crud
        if not vector_index.enabled(db):
            return ""
        hits = vector_index.search_similar(
            db, tpl_crud.GLOBAL, tpl_crud.SOURCE_TYPE_ARCHETYPE, text, top_k=top_k)
        lines = []
        for h in hits[:top_k]:
            t = str(getattr(h, "chunk_text", "") or (h.get("chunk_text") if isinstance(h, dict) else "") or "")
            score = getattr(h, "score", None) or (h.get("score") if isinstance(h, dict) else None)
            if t:
                lines.append(f"  · {t}" + (f"（相似度 {score:.2f}）" if score else ""))
        if not lines:
            return ""
        return ("\n\n【角色原型参考（来自已完成小说的套路库，仅供定位与性格粒度参考）】\n"
                + "\n".join(lines)
                + "\n要求：可参考其**功能定位、位阶组合、性格刻度强度**来补全角色字段；"
                  "**不得照抄姓名 / 门派 / 具体身份**——本书已有设定优先，原型只是尺度参照。\n")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[config_command] 原型库召回失败（不影响建卡）: "
                       f"{type(e).__name__}: {str(e)[:120]}")
        return ""


def run(db: Session, project_id: str, text: str, dry_run: bool = False,
        model_id: str | None = None, entity_type: str | None = None):
    # 模型选择统一走 model_crud.resolve_model（Phase 3.4）：指定且 active 才用，否则回退默认。
    # 原先此处自写了一遍「get_model / get_default + 二次 status 校验」，与 router 层重复。
    default = model_crud.resolve_model(db, model_id)
    use_model = default is not None
    if not use_model:
        return ok({
            "reply": "未配置可用的默认模型。请先在左侧「模型配置」中添加模型并设为默认，才能自动整理资料库。",
            "changes": _empty_changes(),
            "model_ok": False,
        })

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + _archetype_hints(db, text)},
        {"role": "user", "content": text},
    ]
    config = {
        "api_base": default.api_base,
        "api_key": default.api_key,
        "model_name": default.model_name,
        "temperature": 0.3,
        "top_p": 0.9,
        "max_tokens": 4000,
        "enable_thinking": default.enable_thinking,
    }
    _t0 = time.monotonic()
    adapter = None
    try:
        adapter = get_adapter(default.vendor, config)
        raw = adapter.chat(messages, temperature=0.3)
        # ── 用量计量（08-B5）：指令解析此前不记账 → 观测页统计偏低 ──
        usage_crud.record_usage(
            db,
            scene="command",
            vendor=getattr(default, "vendor", None),
            model_name=getattr(default, "model_name", None),
            usage=getattr(adapter, "last_usage", None),
            project_id=project_id,
            duration_ms=int((time.monotonic() - _t0) * 1000),
            ok=True,
            prompt_text=text,
            completion_text=raw or "",
        )
    except Exception as e:  # noqa: BLE001
        # 结果里 model_ok=False 会告诉前端"模型没跑通"，服务端补堆栈（Phase 3.5）
        logger.exception(f"[config_command] 模型调用失败 vendor={default.vendor!r}")
        # 失败也记（prompt 已发出同样烧 token，08-B5）
        usage_crud.record_usage(
            db,
            scene="command",
            vendor=getattr(default, "vendor", None),
            model_name=getattr(default, "model_name", None),
            project_id=project_id,
            duration_ms=int((time.monotonic() - _t0) * 1000),
            ok=False,
            prompt_text=text,
        )
        return ok({
            "reply": f"模型调用失败：{str(e)[:200]}",
            "changes": _empty_changes(),
            "model_ok": False,
        })

    data = _extract_json(raw)
    if data is None:
        return ok({
            "reply": "AI 返回的内容无法解析为结构化设定，请换种说法或补充更多细节。",
            "changes": _empty_changes(),
            "model_ok": True,
        })

    # 单次实体指令（添加角色/地点/势力）：只抽该类型，避免顺带脑补其他实体。
    # /设定 或不指定类型时保持原行为（四类都抽）。
    if entity_type in ("角色", "地点", "势力"):
        for _k in ("characters", "factions", "locations", "relations"):
            data.setdefault(_k, [])
        if entity_type == "角色":
            data["factions"] = data["locations"] = data["relations"] = []
        elif entity_type == "地点":
            data["characters"] = data["factions"] = data["relations"] = []
        elif entity_type == "势力":
            data["characters"] = data["locations"] = data["relations"] = []

    # ── 字段值后处理安全网：纠正模型常犯的字段混淆错误 ──
    _sanitize_characters(data)

    if dry_run:
        # 预览：保留 LLM 抽到的完整字段（供对话实体建议复用），不只 name
        preview = _empty_changes()
        for c in (data.get("characters") or []):
            entry = {"action": "preview", "name": c.get("name", "")}
            # 透传 LLM 抽到的角色属性（personality/talent/skills/等）
            for _f in ("role_type", "age", "gender", "personality", "background",
                       "talent", "current_level", "skills", "relationship_network",
                       "brief"):
                if _f in c and c[_f] not in (None, ""):
                    entry[_f] = c[_f]
            preview["characters"].append(entry)
        for f_item in (data.get("factions") or []):
            entry = {"action": "preview", "name": f_item.get("name", "")}
            for _f in ("description", "status", "territory"):
                if _f in f_item and f_item[_f] not in (None, ""):
                    entry[_f] = f_item[_f]
            preview["factions"].append(entry)
        for l_item in (data.get("locations") or []):
            entry = {"action": "preview", "name": l_item.get("name", "")}
            for _f in ("location_type", "region", "description"):
                if _f in l_item and l_item[_f] not in (None, ""):
                    entry[_f] = l_item[_f]
            preview["locations"].append(entry)
        preview["relations"] = [
            {"subject": r.get("subject"), "object": r.get("object"),
             "relation_type": r.get("relation_type"), "action": "preview"}
            for r in (data.get("relations") or [])
        ]
        return ok({
            "reply": data.get("reply", "（预览）以下设定将被写入资料库："),
            "changes": preview,
            "dry_run": True,
            "model_ok": True,
        })

    changes = _apply(db, project_id, data)
    return ok({
        "reply": data.get("reply", "已为你整理资料库。"),
        "changes": changes,
        "model_ok": True,
    })


def _apply(db: Session, project_id: str, data: dict):
    changes = _empty_changes()
    char_name_map = {}

    # ---- 角色 ----
    for it in (data.get("characters") or []):
        name = (it.get("name") or "").strip()
        if not name:
            continue
        existing = db.query(CharacterORM).filter_by(project_id=project_id, name=name).first()
        fields = {k: it[k] for k in _CHAR_FIELDS if k in it and it[k] not in (None, "")}
        for lf in ("skills", "relationship_network"):
            if lf in it and it[lf] is not None:
                fields[lf] = _as_list(it[lf])
        if existing:
            character_crud.update_character(db, project_id, existing.id, CharacterUpdate(**fields))
            changes["characters"].append({"id": existing.id, "name": name, "action": "updated"})
            char_name_map[name] = existing.id
        else:
            obj = character_crud.create_character(db, project_id, CharacterCreate(name=name, **fields))
            changes["characters"].append({"id": obj.id, "name": name, "action": "created"})
            char_name_map[name] = obj.id

    # ---- 势力 ----
    for it in (data.get("factions") or []):
        name = (it.get("name") or "").strip()
        if not name:
            continue
        existing = db.query(FactionORM).filter_by(project_id=project_id, name=name).first()
        fields = {}
        for k in ("description", "status"):
            if k in it and it[k] not in (None, ""):
                fields[k] = it[k]
        # 成员姓名 → 角色 id
        members = [char_name_map[m] for m in _as_list(it.get("members")) if m in char_name_map]
        if members:
            fields["members"] = members
        if existing:
            faction_crud.update_faction(db, project_id, existing.id, FactionUpdate(**fields))
            changes["factions"].append({"id": existing.id, "name": name, "action": "updated"})
        else:
            obj = faction_crud.create_faction(db, project_id, FactionCreate(name=name, **fields))
            changes["factions"].append({"id": obj.id, "name": name, "action": "created"})

    # ---- 地点（无坐标则自动散布，保证世界地图可见）----
    existing_locs = db.query(LocationORM).filter_by(project_id=project_id).all()
    base_idx = len(existing_locs)
    loc_idx = 0
    for it in (data.get("locations") or []):
        name = (it.get("name") or "").strip()
        if not name:
            continue
        existing = db.query(LocationORM).filter_by(project_id=project_id, name=name).first()
        fields = {}
        for k in ("location_type", "region", "plane", "description"):
            if k in it and it[k] not in (None, ""):
                fields[k] = it[k]
        if "notable_features" in it and it["notable_features"] is not None:
            fields["notable_features"] = _as_list(it["notable_features"])
        # 坐标：用户给就用，否则螺旋散布
        cx = it.get("center_x")
        cy = it.get("center_y")
        if cx is None or cy is None:
            ang = (base_idx + loc_idx) * 2.399963  # 黄金角，分布均匀
            r = 60 * math.sqrt(base_idx + loc_idx + 1)
            cx = round(r * math.cos(ang), 2)
            cy = round(r * math.sin(ang), 2)
            loc_idx += 1
        fields["center_x"] = cx
        fields["center_y"] = cy
        if existing:
            location_crud.update_location(db, project_id, existing.id, LocationUpdate(**fields))
            changes["locations"].append({"id": existing.id, "name": name, "action": "updated"})
        else:
            obj = location_crud.create_location(db, project_id, LocationCreate(name=name, **fields))
            changes["locations"].append({"id": obj.id, "name": name, "action": "created"})

    # ---- 关系（角色姓名 → id，跳过重复/无法解析）----
    for it in (data.get("relations") or []):
        subj = (it.get("subject") or "").strip()
        obj = (it.get("object") or "").strip()
        rtype = (it.get("relation_type") or "").strip()
        if not subj or not obj or not rtype:
            continue
        subj_id = char_name_map.get(subj)
        obj_id = char_name_map.get(obj)
        if not subj_id or not obj_id:
            changes["relations"].append({"subject": subj, "object": obj, "action": "skipped", "reason": "未找到对应角色"})
            continue
        dup = db.query(EntityRelationORM).filter_by(
            project_id=project_id, a_id=subj_id, b_id=obj_id
        ).first()
        if dup:
            changes["relations"].append({"subject": subj, "object": obj, "action": "skipped", "reason": "关系已存在"})
            continue
        rel = relation_crud.create_relation(db, project_id, RelationCreate(
            subject_id=subj_id,
            object_id=obj_id,
            relation_type=rtype,
            strength=int(it.get("strength", 50) or 50),
            note=it.get("note"),
        ))
        changes["relations"].append({"subject": subj, "object": obj, "id": rel.id, "action": "created"})

    return changes
