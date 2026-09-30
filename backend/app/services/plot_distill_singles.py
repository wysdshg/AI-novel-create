# -*- coding: utf-8 -*-
"""批量**单弧**凝练（2026-09-17，用户拍板：能聚合就聚合，聚合不了的单弧也单独成模板）。

与 `plot_distill.distill_all` 的分工：
- 跨书组（≥2 书相似）→ `distill_all`（多变体套路模板）；
- **未聚合的弧**（创新桥段/独本题材，如回明的架空历史）→ 本模块：一次调用把 10~15 条弧
  各自凝练成独立模板（JSON 数组，逐弧输出不合并），全部带 L2 位阶×性格。
入库走 `plot_template_crud.create`（**自动三路索引** —— 修复了跨书路径不自动索引的坑）。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.services import app_config as _app_config
from app.services import plot_import as pi
from app.services import plot_template_crud as tpl_crud
from app.services.plot_distill import (
    CLUSTER_THRESHOLD, _normalize_cast, cluster_arcs, collect_arcs)

logger = logging.getLogger(__name__)


def _cast_rules() -> str:
    """cast/L2 规则段（与 `_distill_prompt` 的同名规则**保持同步**——改动两处都要改）。"""
    return (
        "每个模板的 `cast`：**3~6 个功能槽位**（用于映射到作者自己小说的真实角色）。每个槽位字段：\n"
        "- `slot`：功能名（4~8 字），写功能不写代称（如 引路人师长 / 退婚的未婚妻 / 敌对宗门长老）；\n"
        "- `desc`：15~30 字，写清与主角的关系、承担的作用、大概来头。🔴 不得出现任何源书专有名词，\n"
        "  需要提及用功能词替代（某宗→敌对宗门，物品→关键信物，境界→高阶修为）；\n"
        "- `mode`：助力/阻碍/见证/对手 四选一；\n"
        "- `ranks`：位阶 1~3 个短标签（如 家族高层/长老/散修/皇室）——同一个性格在不同位阶表现完全不同；\n"
        "- `traits`：性格刻度，12 个维度**每个都给整数**，刻度 -10~+10 但只准取 **0/±1/±4/±7/±10**：\n"
        "    利他↔自私 altruism ｜ 信义↔背信 honor ｜ 仁慈↔狠辣 mercy ｜ 坚毅↔易摧 resolve\n"
        "    果决↔犹豫 decisiveness ｜ 自律↔放纵 discipline ｜ 冒险↔稳健 risk（两端无善恶）\n"
        "    理性↔冲动 rationality ｜ 城府↔直率 guile ｜ 理想↔务实 idealism ｜ 热忱↔冷漠 warmth ｜ 强势↔随和 dominance\n"
        "  填法：① 有文本依据的按依据填（不写进 basis，缺省即 text）；② **没体现的不要填 0**，按位阶群体倾向\n"
        "    或顺势一致性推断，取 ±1（最多 ±4）并把来源写进 basis（\"slot\"/\"peer\"）；③ ±7/±10 必须有文本依据；\n"
        "    ④ **矛盾要保留**（对家人好对外人狠=立体）；⑤ **不许按身份标签成套推断**：主角不一定无私/理性/仁慈，\n"
        "    敌人不一定狠辣/背信——**道德三维（利他/信义/仁慈）不许用位阶倾向成套推断**，只准 ±1 顺势小幅；\n"
        "    ⑥ 若槽位是组织/势力而非个人，`traits` 整体省略；\n"
        "- `basis`：只列非文本来源的维度，如 {\"mercy\":\"peer\"}；\n"
        "- `beats`：该槽位出现在哪些节拍（填节拍名）；\n"
        "- `srcs`：**照抄**该弧「角色槽位」里对应功能的代称，格式 [{\"book\":\"书名\",\"alias\":\"代称\"}]。\n"
    )


def _singles_prompt(arcs: list[dict]) -> str:
    blocks = []
    for i, a in enumerate(arcs, 1):
        beats = "\n".join(
            f"  · [{b['label'] or '—'}] 第{b['chapters'][0]}~{b['chapters'][-1]}章：{b['summary']}"
            for b in a["beats"])
        cast = "\n".join(
            f"  · {s['alias']}（{s['kind']}）：{s['desc'] or '—'}"
            for s in (a.get("cast_candidates") or []))
        blocks.append(
            f"【弧 {i}】《{a['book']}》#{a['arc_no']} {a['name']}\n弧概括：{a['summary']}\n"
            f"节拍明细：\n{beats}"
            + (f"\n本弧角色槽位：\n{cast}" if cast else ""))
    return (
        f"下面是 {len(arcs)} 条**互相独立**的故事弧。请为**每一条弧单独**凝练一个可复用的情节模板——\n"
        "**不要合并它们、不要跨弧归纳**：有的弧是某位作者的创新桥段，价值就在它本身。\n"
        "输出一个 JSON **数组**：数组第 i 个元素对应【弧 i】，**顺序与输入严格一致**，共 "
        f"{len(arcs)} 个元素。每个元素字段：\n"
        "- `name`：4~8 字套路名；`logline`：一句话核心张力（30 字内）；`genre_tags`：2~4 个标签；\n"
        "- `structure`：3~5 个 `phases`（每 phase 下若干 `beats`，每个 beat 用 `variants` 记录这条弧的具体处理，\n"
        "  `src` 填书名；🔴 `how` 写 **100~160 字的完整走法**（起因/经过/结果）——\n"
        "  **严禁出现源书的任何人名/功法名/势力名/物品名/地名**（书名只准出现在 `src` 字段），\n"
        "  一律用功能词替代：主角/师长/同门/敌对长老/某宗/关键功法/关键信物/高阶修为；\n"
        "  结构：**谁（功能角色）因何起因 → 具体怎么做（经过，1~2 个动作）→ 结果与影响**；\n"
        "  例：❌「敌对长老指使弟子围攻，主角借丹药破境」→ ✅「敌对长老忌惮主角进步，假借演练之名"
        "指使亲信弟子在演武中轮番下死手；主角表面落于下风，实则借对抗摸清功法缺陷并当场修正；"
        "最终以修正后的招式反压全场，长老当场失态，为主角引来高层注意」；\n"
        "  例：❌「徐广指使弟子围攻，借丹药突破」 → ✅「敌对长老指使弟子围攻，主角借关键丹药破境」）；\n"
        "  `pitfalls`：2~4 条常见翻车点；`rhythm`：各阶段章数配比；\n"
        + _cast_rules() +
        f"\n只输出 JSON 数组（不要 markdown 代码块、不要任何说明文字）。"
        f"🔴 `cast` 是**必填字段**：缺失或为空数组的模板视为不合格——它是映射到作者小说的角色功能位，价值与 structure 相同。"
        f"输出 token 不设上限（宁全勿简），每个模板 800~1200 token：\n"
        '[{"name":"...","logline":"...","genre_tags":["..."],'
        '"structure":{"phases":[{"phase":"...","beats":[{"beat":"...","variants":[{"src":"书名","how":"..."}]}]}]},'
        '"cast":[{"slot":"...","desc":"...","mode":"助力","ranks":["..."],'
        '"traits":{"altruism":0,"honor":0,"mercy":0,"resolve":0,"decisiveness":0,"discipline":0,'
        '"risk":0,"rationality":0,"guile":0,"idealism":0,"warmth":0,"dominance":0},"basis":{},'
        '"beats":["..."],"srcs":[{"book":"书名","alias":"代称"}]}],"pitfalls":["..."],"rhythm":"..."}]\n\n'
        + "\n\n".join(blocks)
    )


def _parse_array_loose(raw: str) -> list:
    """宽松解析 JSON 数组；截断时**抢救已完成的元素**（B19 思路）。"""
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    t = t.strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        pass
    # 截断抢救：扫描**所有顶层 `{...}` 对象**（无论外面包的是数组还是正文）。
    # 🔴 之前只在 depth==0 时记 start —— 顶层数组里的对象在 depth 1，截断的数组一个都救不回来
    #    （实测九星批次 15 弧 → 0 模板）。现在：遇到 `{` 且 depth==0 记 start，配对 `}` 收割。
    depth, start, items, in_str, esc = 0, None, [], False, False
    for idx, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        items.append(json.loads(t[start:idx + 1]))
                    except Exception:  # noqa: BLE001
                        pass
                    start = None
    return items


def distill_singles(db: Session, book_names: list[str] | None = None, *,
                    batch_size: int = 15, threshold: float = CLUSTER_THRESHOLD,
                    status: str = "draft", force: bool = False,
                    max_batches: int | None = None) -> dict:
    """把「未进跨书组的弧」按批凝练成单弧模板（每批 1 次调用出 N 个模板）。

    排除规则：重新按 `threshold` 聚类，**≥2 本书的组**里的弧视为已聚合（跳过）——
    与 `distill_all(min_books=2)` 的口径一致，避免同一弧出两份模板。
    幂等：`source_stats.singles_fp` 记每书的"弧指纹"，未变化则跳过（force 可强制）。
    """
    arcs = collect_arcs(db, book_names)
    if not arcs:
        return {"skipped": True, "reason": "无弧"}
    # 已聚合弧集合（跨书组）
    aggregated: set[tuple[str, int]] = set()
    try:
        for g in cluster_arcs(db, arcs, threshold=threshold):
            books = {a["book"] for a in g["arcs"]}
            if len(books) >= 2:
                for a in g["arcs"]:
                    aggregated.add((a["book"], a["arc_no"]))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[singles] 聚类失败（按\"全部弧均为单弧\"处理）: {type(e).__name__}: {e}")
    singles = [a for a in arcs if (a["book"], a["arc_no"]) not in aggregated]
    # 2026-09-18：去重——已产出过模板的弧不再重复凝练（重跑只补新的）
    _covered = _existing_arc_refs(db)
    singles = [a for a in singles if (a["book"], int(a["arc_no"])) not in _covered]
    if not singles:
        return {"skipped": True, "reason": "所有弧都已进跨书组"}

    by_book = {}
    for a in singles:
        by_book.setdefault(a["book"], []).append(a)
    key = pi.ms_key(db)
    if not key:
        raise RuntimeError("未配置魔搭 Key（app_configs.llm.modelscope_key）")
    rate = pi.RateLimiter(min_interval=2.0, tpm_budget=pi.TPM_BUDGET)

    created, failed = [], []
    for book, barcs in sorted(by_book.items()):
        fp = f"singles_fp.{book}"
        cur_fp = str(hash(tuple((a["arc_no"], a["summary"] or "") for a in barcs)))
        if not force and _app_config.get(db, fp) == cur_fp:
            logger.info(f"[singles] 《{book}》单弧指纹未变，跳过（force 可强制）")
            continue
        nb = 0
        for lo in range(0, len(barcs), batch_size):
            chunk = barcs[lo:lo + batch_size]
            nb += 1
            if max_batches and nb > max_batches:
                break  # 试跑模式：只跑前 N 批
            prompt = _singles_prompt(chunk)
            try:
                try:
                    db.rollback()          # LLM 调用前结束长读事务（记账锁根因，见 distill_template）
                except Exception:  # noqa: BLE001
                    pass
                raw = pi._ms_post(key, prompt, max_tokens=16384, rate=rate,
                                  thinking=False, timeout=900,   # 🔴 必须关思考：实测 2 弧推理就 2 万字（正文 6 倍），
                                                                  # 13 弧批量的 reasoning 会吃光预算 → 服务端空响应,
                                  on_usage=pi.make_usage_cb("ms_distill"))
                items = _parse_array_loose(raw)
                if len(items) != len(chunk):
                    logger.warning(f"[singles] 《{book}》批内弧 {len(chunk)} 个 / 模板 {len(items)} 个"
                                   f"（截断或漏号，按能配对的入库）")
                for a, item in zip(chunk, items):
                    if not isinstance(item, dict) or not item.get("name") or not item.get("structure"):
                        failed.append({"book": book, "arc": a["arc_no"], "reason": "输出不完整"})
                        continue
                    structure = item.get("structure") or {}
                    cast = _normalize_cast(item.get("cast"), [a], db)
                    if cast:
                        structure = {**structure, "cast": cast}
                    o = tpl_crud.create(db, {
                        "name": str(item.get("name"))[:60],
                        "scale": "arc",
                        "genre_tags": item.get("genre_tags") or [],
                        "logline": item.get("logline"),
                        "structure": structure,
                        "pitfalls": item.get("pitfalls") or [],
                        "rhythm": item.get("rhythm"),
                        "source_stats": {
                            "books": 1, "book_names": [book],
                            "arc_refs": [f"{book}#{a['arc_no']}:{a['name']}"],
                            "single": True, "cast_slots": len(cast),
                        },
                        "status": status,
                    })
                    created.append({"name": o.name, "book": book, "arc": a["arc_no"]})
            except Exception as e:  # noqa: BLE001
                failed.append({"book": book, "arcs": [a["arc_no"] for a in chunk],
                               "reason": f"{type(e).__name__}: {e}"})
        _app_config.set_value(db, fp, cur_fp)
        db.commit()
    logger.info(f"[singles] 单弧凝练完成：入库 {len(created)}，失败 {len(failed)}")
    return {"created": len(created), "templates": created, "failed": failed,
            "singles": len(singles)}


def _existing_arc_refs(db) -> set[tuple[str, int]]:
    """已入库模板覆盖到的 (书名, 弧号) —— 用于去重：重跑不会重复产出同一条弧。"""
    import json as _json
    import re as _re
    from app.models.orm import PlotTemplateORM as _T
    out: set[tuple[str, int]] = set()
    for t_ in db.query(_T).all():
        try:
            ss = _json.loads(t_.source_stats) if isinstance(t_.source_stats, str) else (t_.source_stats or {})
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[singles] 解析 source_stats 失败，跳过该模板: {type(_e).__name__}: {_e}")
            continue
        for ref in (ss.get("arc_refs") or []):
            m = _re.match(r"(.+?)#(\d+)", str(ref))   # 实际格式：太荒吞天诀#41:三日认亲救主（无书名号）
            if m:
                out.add((m.group(1), int(m.group(2))))
    return out
