# -*- coding: utf-8 -*-
"""弧级原子标注（docs/10 P1）：把一个弧的所有去重段标成原子事件 + 走法文本。

🔴🔴 **本脚本的输入层已作废（2026-09-18 事后查证，见 docs/10 §5.0）—— 复跑请先改 `fetch_segments`**：
它读 `chapter_summaries.segment_summary`，而这一层已被 v3 Pass2 **复用为「弧内节拍说明」
（规格 20~40 字，`plot_import.py:1359-1364`）**，另有 3 本书（北派盗墓/道诡异仙/修真四万年）
的段数据来自**旧路径残留**（1 章/段、140~184 字，违反 v3 规格）→ **整层是混源层，不可作输入**。
正确输入层 = **逐章概括 `chapter_summaries.summary`**（覆盖 100%、中位 112~156 字、已匿名化），
按 `(book_name, arc_no)` 取该弧**全部章**的逐章概括（而不是去重段摘要）。
→ 本脚本保留作 P1 的历史产物与判据参考（校验/入库/幂等逻辑可复用），**但不要原样复跑**。

流程（docs/10 §5）：取弧段 → 造 prompt（段摘要 + 原子表）→ 一次 LLM 调用
→ 校验 atomic_id 必须在表内（不在则记「待新增原子候选」，**不自动新增原子**）
→ 追加 atomic_variants（按 (arc_ref, segment_no) 幂等）。

模型调用**复用** `app/services/plot_import.py` 的 `ms_key(db)` + `_ms_post(...)`，
不自建 HTTP 客户端。🔴 `max_tokens` 上限 16384（32000 会被魔搭拒，且错误体非 JSON
→ 重试打转，2026-09-18 踩过）。

用法（在 backend/ 下执行）：

    ..\\.venv\\Scripts\\python.exe scripts/label_atomic_variants.py --dry-run
    ..\\.venv\\Scripts\\python.exe scripts/label_atomic_variants.py --arc "太荒吞天诀#22"
    ..\\.venv\\Scripts\\python.exe scripts/label_atomic_variants.py            # 3 个 POC 弧全跑
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import create_engine, text                        # noqa: E402
from sqlalchemy.orm import sessionmaker                           # noqa: E402
from sqlalchemy.pool import StaticPool                            # noqa: E402

# POC 三个弧（用户已核实存在，共 11 段）
POC_ARCS = [
    ("太荒吞天诀", 22, "外门大比夺魁", "A02 擂台比试"),
    ("九星霸体诀", 5, "拍卖会争锋", "B01 拍卖竞价"),
    ("北派盗墓笔记", 25, "西夏秘闻与玛瑙原石", "B02 赌石切宝"),
]

# 🔴 魔搭 max_tokens 硬上限（docs/10 P1 明确：不得超过 16384）
MAX_OUT_TOKENS = 16384
# 验收线是 100~160（docs/10 §8 P1）；prompt 里给 105~150 留余量 ——
# 实测（2026-09-18 首轮）让模型"写到 160"会出现 162 字，收 10 字余量后稳定落线内。
TEXT_MIN, TEXT_MAX = 100, 160
TEXT_ASK_MIN, TEXT_ASK_MAX = 105, 150

# 🔴 原子表边界裁决规则（2026-09-18 用户拍板，来自 P1 首轮人工抽查的 3 条边界项）。
# 这类规则**只解决「两个原子都成立时选哪个」**，不改原子本身的定义 ——
# 之所以放进 prompt 而不是硬编码进判定逻辑：同一套规则在 P2/P3 组装与 P4 回标时都要生效，
# 单一出处才不会漂移。新增边界项时往这里追加一行。
BOUNDARY_RULES = [
    "【边界裁决 1·比试场内一律 A02】若该段发生在**有规则、有名次排名的公开比试场内**"
    "（宗门大比、公开选拔、擂台赛、擂台轮次），无论对手境界高低、是否越阶死战、是否临阵突破，"
    "**一律标 A02 擂台比试**。「越阶/以弱胜强」只写进 tags（如「越阶硬撼」），不选 A05。"
    "A05 只用于**非比试场**的一对一硬撼（野外截杀、私斗、堵门寻仇）。",
]


def _variant_id(arc_ref: str, segment_no: int) -> str:
    """确定性主键（不用内置 hash：进程级随机化 → 不可复现）。"""
    import hashlib
    h = hashlib.md5(f"{arc_ref}#{segment_no}".encode("utf-8")).hexdigest()
    return f"av-{h[:20]}"


def parse_items(raw: str) -> list[dict]:
    """从模型输出里抠出 JSON 数组 —— 容错：代码围栏 / 前后废话 / 尾逗号 / 全角引号 / 截断。"""
    if not raw:
        return []
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)

    def _try(txt: str):
        for fix in (lambda x: x,
                    lambda x: re.sub(r",\s*([}\]])", r"\1", x),
                    lambda x: x.replace("“", '"').replace("”", '"')):
            try:
                return json.loads(fix(txt))
            except (json.JSONDecodeError, ValueError):
                continue
        return None

    # 1) 先按数组整体解析
    i, j = s.find("["), s.rfind("]")
    if i != -1 and j > i:
        data = _try(s[i:j + 1])
        if data is not None:
            return data if isinstance(data, list) else []

    # 2) 退化：模型包成 {"items": [...]} / {"data": [...]}
    i2, j2 = s.find("{"), s.rfind("}")
    if i2 != -1 and j2 > i2:
        data = _try(s[i2:j2 + 1])
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v

    # 3) 最后手段：逐个平衡大括号抠对象（应对截断/夹杂解释文字）
    items, depth, start = [], 0, None
    for k, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = k
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                obj = _try(s[start:k + 1])
                if isinstance(obj, dict):
                    items.append(obj)
                start = None
    return items


def build_atom_table(db) -> str:
    """拼「原子事件词表」文本（含 candidate 标注），供 prompt 使用。"""
    from app.models.orm import AtomicCategoryORM, AtomicEventORM
    cats = db.query(AtomicCategoryORM).order_by(AtomicCategoryORM.sort_order).all()
    lines = []
    for c in cats:
        rows = (db.query(AtomicEventORM)
                .filter(AtomicEventORM.category_id == c.id)
                .order_by(AtomicEventORM.id).all())
        lines.append(f"■ {c.id} {c.name}（{c.note or ''}）")
        for a in rows:
            flag = "" if a.status == "active" else "（待定，可选用）"
            lines.append(f"{a.id} {a.name}{flag}：{a.definition}")
            lines.append(f"    起：{a.beat_start}｜承：{a.beat_mid}｜"
                         f"转：{a.beat_turn}｜合：{a.beat_end}")
    return "\n".join(lines)


def load_originals(db, book: str) -> list[str]:
    """取该书「原名表」（book_aliases，len≥2）——专名泄漏检测的判据。"""
    rows = db.execute(text(
        "SELECT DISTINCT original FROM book_aliases "
        "WHERE book_name = :b AND LENGTH(original) >= 2"), {"b": book}).fetchall()
    return [r[0] for r in rows]


def build_prompt(book: str, arc_no: int, arc_name: str, segs: list[tuple[int, str]],
                 atom_table: str) -> str:
    seg_txt = "\n".join(f"第{no}段：{sm}" for no, sm in segs)
    boundary = "\n".join(f"{i}. {r}" for i, r in enumerate(BOUNDARY_RULES, 1))
    return f"""你是小说情节结构分析员。下面给你一部小说某个「篇（弧）」的全部情节段摘要，
以及一张「原子事件词表」。请为**每一个段**选出最贴合的一个原子事件，并写出该段的走法概括。

【原子事件词表】
{atom_table}

【边界裁决规则（两个原子都成立时按这个选，优先级高于对原子定义的字面理解）】
{boundary}

【待标注的弧】{arc_name}（共 {len(segs)} 个段）
{seg_txt}

【输出要求】
只输出一个 JSON 数组，不要任何解释文字，不要 markdown 代码块围栏。数组每个元素形如：
{{"segment_no": 3, "atomic_id": "B01", "text": "……", "tags": ["抬价试探", "隐藏实力"]}}

规则（务必逐条遵守）：
1. atomic_id 必须**严格取自词表**（形如 A02 / B01 / C11 / D08）。不得自创编号、不得改大小写、不得写原子名。
2. 若某段与词表中任何原子都不贴合，atomic_id 写空字符串 ""，并在 tags 里加一项 "无合适原子"，text 仍照要求写。
3. text = 该段「走法」的结构化概括，**{TEXT_ASK_MIN}~{TEXT_ASK_MAX} 字**（中文字符计，绝对不要超过 {TEXT_MAX} 字），
   必须写清 起因 / 经过 / 结果 三部分，让读者不看原文也能照着搭出这一段。
4. 🔴 text 与 tags **不得出现任何专名**：人名、势力名、功法名、地名、宝物名一律不许出现；
   需要指代时用「主角」「同行者」「对手」「某一势力」「一处秘境」这类通用词。
   原文摘要里若出现「主角」「友·配角1」「敌·宗门2」这类匿名化代称，可以沿用。
5. tags 给 2~4 个**手法或特征**标签（如 扮猪吃虎 / 苦肉计 / 借刀杀人 / 抬价试探 / 隐藏实力 /
   引第三方互斗），不要复述原子名。
6. 每个段必须输出且仅输出一条，segment_no 用我给出的编号，不要漏、不要多、不要改号。
7. 若某段同时符合两个原子，**先套【边界裁决规则】**；规则未覆盖的冲突，按「该段的主要动作是什么」选，
   不要把「转」环节里的次要事件当作该段的原子。"""


def fetch_segments(db, book: str, arc_no: int) -> list[tuple[int, str]]:
    """按弧取输入条目：**逐章概括**（docs/10 §5.0 新管线口径，2026-09-21 改）。

    🔴 为什么不再读 `segment_summary`：那一层已被 v3 Pass2 复用为「弧内节拍说明」（规格 20~40 字，
    `plot_import.py:1359-1364`），且有 3 本书是旧路径残留（1 章/段、140~184 字）——**整层混源，不可作输入**。
    正确输入层 = `chapter_summaries.summary`：覆盖 100%、中位 112~156 字、已匿名化。

    返回 `[(chapter_no, summary)]`——编号语义为**章号**（旧实现返回 segment_no）。
    下游幂等键 `(arc_ref, segment_no)` 复用同一列存章号，同弧重标注仍幂等。
    """
    rows = db.execute(text(
        "SELECT chapter_no, summary FROM chapter_summaries "
        "WHERE book_name = :b AND arc_no = :a AND summary IS NOT NULL "
        "ORDER BY chapter_no"), {"b": book, "a": arc_no}).fetchall()
    out = []
    for no, sm in rows:
        if sm and str(sm).strip():
            out.append((int(no), str(sm).strip()))
    return out


def check_names(txt: str, originals: list[str]) -> list[str]:
    """专名泄漏检测：拿 `book_aliases` 里该书的**真实原名表**做子串匹配。

    为什么不用正则：正则只能抓「《》」和「境界N」这类形态，抓不到真正的问题
    ——模型把「项峰」「西夏宫」「熔星草」直接写进走法。原名表是本项目现成的、
    数据驱动的判据（实测三本书覆盖 709 / 480 / 362 个原名，几乎全部 ≥2 字）。
    """
    return sorted({o for o in originals if o in txt}, key=len, reverse=True)


def dump_report(db, targets, report_path: str, no_atom_cases: list[str]) -> int:
    """从库内现状重建 markdown 抽查表（**不调模型**），供重跑后刷新报告。

    与「边标边出报告」的区别：这里的文本一律以库为准 —— 重跑某弧后旧报告就过期了，
    再花一次模型调用只为出报告不划算。
    """
    from app.models.orm import AtomicEventORM, AtomicVariantORM
    rows_out = []
    for book, arc_no, arc_name, expect in targets:
        arc_ref = f"{book}#{arc_no}"
        seg_map = dict(fetch_segments(db, book, arc_no))
        vs = (db.query(AtomicVariantORM)
              .filter(AtomicVariantORM.arc_ref == arc_ref)
              .order_by(AtomicVariantORM.segment_no).all())
        for v in vs:
            e = db.get(AtomicEventORM, v.atomic_id) if v.atomic_id else None
            rows_out.append({
                "arc_ref": arc_ref, "arc_name": arc_name, "expect": expect,
                "segment_no": v.segment_no, "atomic_id": v.atomic_id,
                "atomic_name": e.name if e else "—",
                "cat": e.category_id if e else "—",
                "text": v.text, "tags": v.tags or [],
                "len": len(v.text or ""), "seg_summary": seg_map.get(v.segment_no, ""),
            })

    total = db.query(AtomicVariantORM).count()
    lines = ["# P1 弧级原子标注 · 人工抽查表（POC 3 弧 / 11 段）", "",
             "> 数据源：`atomic_variants`（库内现状，由 `label_atomic_variants.py --report-only` 重建）",
             f"> 库内 atomic_variants 总行数：**{total}**", "",
             "## 1. 归属一览", "",
             "| 弧 | 弧名 | 段 | 原子 | 原子名 | 类 | 走法字数 | 预期核心 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows_out:
        lines.append(f"| {r['arc_ref']} | {r['arc_name']} | {r['segment_no']} | "
                     f"{r['atomic_id'] or '—'} | {r['atomic_name']} | {r['cat']} | "
                     f"{r['len']} | {r['expect']} |")
    lines += ["", "## 2. 走法全文 / tags / 原段摘要 对照", ""]
    for r in rows_out:
        lines += [f"### {r['arc_ref']} 段{r['segment_no']} → {r['atomic_id'] or '（空）'} "
                  f"{r['atomic_name']}", "",
                  f"- **走法（{r['len']} 字）**：{r['text']}",
                  f"- **tags**：{r['tags']}",
                  f"- **原段摘要**：{r['seg_summary']}", ""]
    if no_atom_cases:
        lines += ["## 3. 待新增原子候选（不自动新增，交作者拍板）", ""]
        lines += [f"- {c}" for c in no_atom_cases] + [""]
    Path(report_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已重建（{len(rows_out)} 条）→ {report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="弧级原子标注（docs/10 P1）")
    ap.add_argument("--arc", default=None, help='只跑一个弧，如 "太荒吞天诀#22"')
    ap.add_argument("--book", default=None, help="跑某本书的**全部弧**（弧清单从库内 arc_no/arc_name 读）")
    ap.add_argument("--max-arcs", type=int, default=None, help="配合 --book：只跑前 N 条弧（试跑用）")
    ap.add_argument("--dry-run", action="store_true", help="只取段 + 打印 prompt，不调模型不落库")
    ap.add_argument("--prompt-out", default=None, help="把 prompt 写到该文件（便于核对）")
    ap.add_argument("--report", default=None, help="把结果写成 markdown 表格到该文件")
    ap.add_argument("--raw-dir", default=None, help="把模型原始输出逐弧存到该目录（留档排查）")
    ap.add_argument("--report-only", action="store_true",
                    help="不调模型，只按库内现状重建 --report 报告")
    args = ap.parse_args()

    targets = POC_ARCS
    if args.arc:
        want = args.arc.strip()
        targets = [t for t in POC_ARCS if f"{t[0]}#{t[1]}" == want]
        if not targets:
            print(f"❌ --arc {want} 不在 POC 列表：{[f'{a}#{b}' for a, b, _, _ in POC_ARCS]}")
            return 2

    import app.core.database as dbmod
    from app.models.orm import AtomicEventORM, AtomicVariantORM

    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()

    # --book：从库内读该书全部弧（v3 弧落在 chapter_summaries 的 arc_no/arc_name）
    if args.book:
        rows = db.execute(text(
            "SELECT DISTINCT arc_no, arc_name FROM chapter_summaries "
            "WHERE book_name = :b AND arc_no IS NOT NULL ORDER BY arc_no"),
            {"b": args.book}).fetchall()
        if not rows:
            print(f"❌ {args.book} 库内没有带弧的章（先跑 --stage arc）")
            return 2
        targets = [(args.book, int(a), str(n or f"弧{a}"), "") for a, n in rows]
        if args.max_arcs:
            targets = targets[:args.max_arcs]
        print(f"[book] {args.book}：{len(rows)} 条弧 → 本次跑 {len(targets)} 条")
    dbmod.init_db()

    atom_table = build_atom_table(db)
    valid_ids = {r[0] for r in db.query(AtomicEventORM.id).all()}
    print(f"原子表：{len(valid_ids)} 条（词表 {len(atom_table)} 字符）")

    if args.report_only:
        if not args.report:
            print("❌ --report-only 需同时给 --report <路径>")
            return 2
        rc = dump_report(db, targets, args.report, [])
        db.close()
        return rc

    all_results: list[dict] = []
    no_atom_cases: list[str] = []

    for book, arc_no, arc_name, expect in targets:
        arc_ref = f"{book}#{arc_no}"
        segs = fetch_segments(db, book, arc_no)
        originals = load_originals(db, book)
        print(f"\n{'=' * 68}\n{arc_ref} {arc_name}｜去重段 {len(segs)}｜预期核心 {expect}"
              f"｜原名表 {len(originals)} 条（专名检测用）")
        for no, sm in segs:
            print(f"  段{no}（{len(sm)}字）: {sm[:60]}…")
        if not segs:
            print("  ⚠️ 无段，跳过")
            continue

        prompt = build_prompt(book, arc_no, arc_name, segs, atom_table)
        if args.prompt_out:
            Path(args.prompt_out).write_text(prompt, encoding="utf-8")
            print(f"  prompt 已写入 {args.prompt_out}（{len(prompt)} 字符）")

        if args.dry_run:
            print("  [dry-run] 不调模型、不落库")
            continue

        # ---- 调模型：复用 plot_import 的 key 读取 + HTTP 封装 ----
        from app.services import plot_import as pi
        key = pi.ms_key(db)
        if not key:
            print("  ❌ 未取到魔搭 key（app_configs.llm.modelscope_key 或环境变量 NA_MS_KEY）")
            return 3
        assert MAX_OUT_TOKENS <= 16384, "max_tokens 超过魔搭上限"
        print(f"  调用魔搭 {pi.MS_MODEL}（key …{key[-6:]}｜max_tokens={MAX_OUT_TOKENS}）…")
        raw = pi._ms_post(key, prompt, max_tokens=MAX_OUT_TOKENS, temperature=0.2,
                          timeout=600, thinking=False)
        print(f"  返回 {len(raw)} 字符")
        if args.raw_dir:
            d = Path(args.raw_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{book}#{arc_no}.txt").write_text(raw, encoding="utf-8")
            print(f"  原始输出已存 {d / (book + '#' + str(arc_no) + '.txt')}")

        items = parse_items(raw)
        print(f"  解析出 {len(items)} 条")
        if len(items) != len(segs):
            print(f"  ⚠️ 条数不匹配（段 {len(segs)} / 结果 {len(items)}），逐段核对后再入库")

        got = {int(it.get("segment_no")): it for it in items
               if str(it.get("segment_no", "")).strip().lstrip("-").isdigit()}

        for no, sm in segs:
            it = got.get(no)
            if it is None:
                print(f"  ❌ 段{no} 模型未输出 → 记为「未标注」")
                all_results.append({"arc_ref": arc_ref, "segment_no": no, "atomic_id": None,
                                    "atomic_name": "—", "text": "", "tags": [],
                                    "status": "❌ 未标注", "issue": "模型未输出该段"})
                continue

            aid = str(it.get("atomic_id") or "").strip()
            txt = str(it.get("text") or "").strip()
            tags = it.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]

            issues = []
            if not aid:
                issues.append("无合适原子")
                no_atom_cases.append(f"{arc_ref} 段{no}：{sm[:80]}")
                status = "⚠️ 无合适原子"
            elif aid not in valid_ids:
                issues.append(f"atomic_id={aid} 不在表内")
                no_atom_cases.append(f"{arc_ref} 段{no} 模型给出 {aid}（不在表内）：{sm[:80]}")
                status = "⚠️ 越表"
            else:
                status = "✅"
            n = len(txt)
            if n < TEXT_MIN or n > TEXT_MAX:
                issues.append(f"走法长度 {n} 字（要求 {TEXT_MIN}~{TEXT_MAX}）")
            nh = check_names(txt + "".join(str(t) for t in tags), originals)
            if nh:
                issues.append("专名泄漏 " + "/".join(nh))

            aname = ""
            if aid in valid_ids:
                aname = (db.query(AtomicEventORM.name)
                         .filter(AtomicEventORM.id == aid).scalar() or "")
            all_results.append({
                "arc_ref": arc_ref, "segment_no": no, "atomic_id": aid or None,
                "atomic_name": aname, "text": txt, "tags": tags,
                "status": status, "issue": "；".join(issues), "seg_summary": sm,
            })

        db.commit()   # 每弧一段落提交，失败不影响已完成弧
        db.expire_all()

    if args.dry_run:
        db.close()
        return 0

    # ---- 入库（按 (arc_ref, segment_no) 幂等 upsert）----
    ins = upd = skipped = 0
    for r in all_results:
        if not r["atomic_id"] or r["atomic_id"] not in valid_ids:
            skipped += 1
            continue
        row = (db.query(AtomicVariantORM)
               .filter(AtomicVariantORM.arc_ref == r["arc_ref"],
                       AtomicVariantORM.segment_no == r["segment_no"]).first())
        book = r["arc_ref"].split("#")[0]
        if row is None:
            db.add(AtomicVariantORM(
                id=_variant_id(r["arc_ref"], r["segment_no"]),
                atomic_id=r["atomic_id"], book_name=book, arc_ref=r["arc_ref"],
                segment_no=r["segment_no"], text=r["text"], tags=r["tags"],
                source="ai", quality="draft"))
            ins += 1
        else:
            row.atomic_id = r["atomic_id"]
            row.text = r["text"]
            row.tags = r["tags"]
            row.quality = "draft"
            upd += 1
    db.commit()

    # 🔴 同连接 checkpoint：否则 WAL 帧对跨进程读者不可见
    try:
        ck = db.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
        print(f"\ncheckpoint(PASSIVE): {ck}")
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint 失败: {type(e).__name__}: {e}")

    total = db.query(AtomicVariantORM).count()
    print(f"入库：新增 {ins} / 更新 {upd} / 跳过（无合适原子或越表）{skipped}")
    print(f"atomic_variants 总行数：{total}")

    # ---- 人工抽查表 ----
    print("\n" + "=" * 100)
    print("人工抽查表（逐条核对原子归属是否准确）")
    print("=" * 100)
    for r in all_results:
        print(f"\n[{r['status']}] {r['arc_ref']} 段{r['segment_no']}  →  "
              f"{r['atomic_id'] or '（空）'} {r['atomic_name']}")
        print(f"  走法（{len(r['text'])}字）: {r['text']}")
        print(f"  tags: {r['tags']}")
        if r["issue"]:
            print(f"  ⚠️ {r['issue']}")
        print(f"  原段摘要: {(r.get('seg_summary') or '')[:160]}")

    if no_atom_cases:
        print("\n" + "=" * 100)
        print(f"待新增原子候选（{len(no_atom_cases)} 条，**不自动新增原子**，交作者拍板）")
        print("=" * 100)
        for c in no_atom_cases:
            print(f"  - {c}")

    if args.report:
        lines = ["# P1 弧级原子标注 · 人工抽查表", "",
                 f"- 弧数：{len(targets)}｜段数：{len(all_results)}",
                 f"- 库内 atomic_variants 总行数：{total}", "",
                 "| 弧 | 段 | 原子 | 原子名 | 走法字数 | 状态 | 问题 |",
                 "|---|---|---|---|---|---|---|"]
        for r in all_results:
            lines.append(f"| {r['arc_ref']} | {r['segment_no']} | {r['atomic_id'] or '—'} | "
                         f"{r['atomic_name'] or '—'} | {len(r['text'])} | {r['status']} | "
                         f"{r['issue'] or '—'} |")
        lines += ["", "## 走法全文与原文摘要对照", ""]
        for r in all_results:
            lines += [f"### {r['arc_ref']} 段{r['segment_no']} → {r['atomic_id'] or '（空）'} "
                      f"{r['atomic_name']}", "",
                      f"**走法（{len(r['text'])}字）**：{r['text']}", "",
                      f"**tags**：{r['tags']}", "",
                      f"**原段摘要**：{(r.get('seg_summary') or '')}", ""]
        if no_atom_cases:
            lines += ["## 待新增原子候选", ""] + [f"- {c}" for c in no_atom_cases] + [""]
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")
        print(f"\n报告已写入 {args.report}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
