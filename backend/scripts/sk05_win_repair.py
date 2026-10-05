# -*- coding: utf-8 -*-
"""[DEV-SK05] win 文件整备：空 ID 回填 + F6B 流程元话术清洗（+ 整备自检）。

只动三书 win 产物的 atoms[].atomic_id / tags / summary；改前逐文件备份 *.bak-sk05。
不碰 plot_templates / event_skeletons / 向量表，不做判类（那是 SK06/SK07）。

    .venv\\Scripts\\python.exe backend/scripts/sk05_win_repair.py --backfill-dry
    .venv\\Scripts\\python.exe backend/scripts/sk05_win_repair.py --backfill-apply
    .venv\\Scripts\\python.exe backend/scripts/sk05_win_repair.py --meta-dry
    .venv\\Scripts\\python.exe backend/scripts/sk05_win_repair.py --meta-apply
    .venv\\Scripts\\python.exe backend/scripts/sk05_win_repair.py --verify

幂等口径（任务单 verify 第 4 项）：所有变换都从 *.bak-sk05 原始副本重放，
同一输入 → 产物逐字节一致；二次 apply 不会二次加工。

回填依据（任务单 §二.1）：outputs/f6c/_drafts/atoms_s*.json 的 no_fit 类别标注，
类别归并判据**直接复用** outputs/f6c/_tools/agg_proposals.py 的 RULES（不另造规则）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD_DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
RAW = ROOT / "outputs" / "_atomic_raw"
DRAFTS_C = ROOT / "outputs" / "f6c" / "_drafts"
OUT = ROOT / "outputs" / "sk05"
LEDGER = OUT / "回填台账.json"
BAK = ".bak-sk05"

BOOK_HM = "win_寒门枭士.json"
BOOK_DP = "win_斗破苍穹.p2.json"
BOOK_FR = "win_凡人修仙传.p2.json"
BOOK_FR_BASE = "win_凡人修仙传.json"
BOOK_DP_BASE = "win_斗破苍穹.json"
BOOKS_META = [BOOK_DP]                      # 元话术清洗范围=任务单 §二.4（F6B）
BOOKS_ALL = [BOOK_HM, BOOK_DP, BOOK_FR, BOOK_FR_BASE, BOOK_DP_BASE]

# no_fit 类别 → 新原子 ID（13 条转正类；与 sk05_lexicon_apply.NEW_TYPES 一一对应）
CATEGORY_TO_ID = {
    "经营实业与商路": "B09",
    "工程兴工与勘测": "G08",
    "赈灾放粮与济贫": "C16",
    "民生治安与基层治理": "C17",
    "变法施政与律法": "C18",
    "朝堂党争与奏章对质": "C19",
    "教化办学与育才": "C20",
    "舆情宣传与民心": "H09",
    "谍报刺探与反谍": "D09",
    "军事筹谋与战前部署": "A10",
    "会战攻城与战役歼灭": "A11",
    "工艺试制与技术研发": "F07",
    "仪典祭祀与丧葬": "H10",
}
# 挂候选/未提案类别 → 保持空 ID（任务单 §二.1；军制类见 NOTE_JUNZHI）
CATEGORY_KEEP_EMPTY = {
    "军制军械与后勤": "挂候选 #14（已并入 A10 定义，但任务单 §二.1 明令保持空 ID，待 PM 裁决）",
    "宗族族产与内宅": "挂候选 #15，未入库",
    "族群外交与邦交": "挂候选 #16，未入库",
    "农事与良种农技": "仅 2 处，不足提案资格（<3），未入库",
}
NOTE_JUNZHI = CATEGORY_KEEP_EMPTY["军制军械与后勤"]

NOFIT_TAG = "无合适原子"

# 流程元话术（任务单 §二.4）：描述切分/窗口状态的话术，非剧情内容。
META_PHRASES = ["未收尾", "续判", "下窗", "上一窗", "本窗", "前窗",
                "原文未写", "素材未写", "留待下窗"]
# 🔴 不用裸「留待」：斗破 c801~803「封进玉瓶留待盘问」是剧情用语，必须保留。
META_SAFE_KEEP = ["留待盘问"]
# 分句切分点含破折号：QA 实测（P1）只切标点会把
# 「，终议定联手破封之约——未收尾」整段删掉，连带丢一句剧情事实。
CLAUSE_SPLIT = re.compile(r"([，；。]|——|—)")


class _Tee:
    def __init__(self, path: Path) -> None:
        self._f = io.open(path, "w", encoding="utf-8")
        self._con = sys.__stdout__

    def write(self, s: str) -> int:
        self._f.write(s)
        self._con.write(s.encode("gbk", "replace").decode("gbk"))
        return len(s)

    def flush(self) -> None:
        self._f.flush()
        self._con.flush()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def content_sha(items) -> str:
    """只对**本阶段真正写进去的内容**求哈希。

    用整份文件的 sha 会把「多阶段流水线的执行顺序」烙进台账：
    meta 阶段先跑时文件里还没有后续 repair 的 9 处改动，冷跑与就地重跑就会记出两个不同值
    （QA 复测时抓到过）。改成内容哈希后，台账与跑序、起跑状态都无关。
    """
    blob = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def original_of(name: str) -> Path:
    """**计算基线**：一律用首次改动前的原始副本（*.bak-sk05），没有备份时才是当前文件。

    为什么不能用当前态做基线：回填的输入是「空 ID 原子」，二次 apply 时它们已经有 ID，
    读当前态会让台账塌成 45 条、把 239 条实际变更抹掉（实测踩过）。
    **落盘目标**仍是当前文件，且写入本身幂等（只挑空 ID 赋值、按 (章,seq) 写定值）。
    """
    bak = RAW / (name + BAK)
    return bak if bak.exists() else RAW / name


def current_of(name: str) -> Path:
    """**落盘目标**：当前文件（保留其它阶段已写入的改动）。"""
    return RAW / name


def same_atoms(cur: dict, orig: dict, name: str) -> bool:
    if len(cur["atoms"]) != len(orig["atoms"]):
        print(f"   [拒绝] {name} 原子数 {len(cur['atoms'])} ≠ 原始 {len(orig['atoms'])}，"
              f"索引基线已失效，不写文件")
        return False
    return True


# ---------------------------------------------------------------------------
# 一、no_fit 全量类别（复用 F6C 的 RULES，不另造判据）
# ---------------------------------------------------------------------------
def load_rules():
    p = DRAFTS_C.parent / "_tools" / "agg_proposals.py"
    spec = importlib.util.spec_from_file_location("agg_rules", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.RULES


def classify_all(rules) -> dict[str, list[dict]]:
    """slice_id -> no_fit 条目（带类别与章节区间）。"""
    def cls(text: str):
        for name, dom, pat, define in rules:
            if re.search(pat, text):
                return name
        return None

    def rng(s):
        m = re.findall(r"\d+", str(s or ""))
        if not m:
            return None
        a, b = int(m[0]), int(m[-1])
        return (a, b) if a <= b else (b, a)

    per_slice: dict[str, list[dict]] = {}
    for p in sorted(DRAFTS_C.glob("atoms_s*.json")):
        d = load_json(p)
        sid = d.get("slice_id")
        items = []
        for i, nf in enumerate(d.get("no_fit") or []):
            items.append({
                "slice": sid, "idx": i,
                "chapters": nf.get("chapters", ""),
                "range": rng(nf.get("chapters", "")),
                "category": cls((nf.get("what", "") or "") + " " + (nf.get("why", "") or "")),
                "what": (nf.get("what", "") or "")[:100],
            })
        per_slice[sid] = items
    return per_slice


def draft_atom_index() -> dict[str, list[dict]]:
    """summary -> [draft 原子]；另建 slice->空原子表供章节兜底。"""
    by_sum: dict[str, list[dict]] = {}
    empty_by_slice: dict[str, list[dict]] = {}
    for p in sorted(DRAFTS_C.glob("atoms_s*.json")):
        d = load_json(p)
        sid = d["slice_id"]
        for a in d.get("atoms") or []:
            rec = {"slice": sid, "local_seq": a.get("local_seq"),
                   "cs": a.get("chapter_start"), "ce": a.get("chapter_end"),
                   "atomic_id": a.get("atomic_id"), "summary": a.get("summary", "")}
            by_sum.setdefault(a.get("summary", ""), []).append(rec)
            if not (a.get("atomic_id") or "").strip():
                empty_by_slice.setdefault(sid, []).append(rec)
    return by_sum, empty_by_slice


def overlap(a1, a2, b1, b2) -> int:
    lo, hi = max(a1, b1), min(a2, b2)
    return hi - lo + 1 if hi >= lo else 0


def build_plan() -> dict:
    rules = load_rules()
    per_slice = classify_all(rules)
    by_sum, empty_by_slice = draft_atom_index()
    win = load_json(original_of(BOOK_HM))

    rows = []
    for a in win["atoms"]:
        if (a.get("atomic_id") or "").strip():
            continue
        cs, ce = a.get("chapter_start"), a.get("chapter_end")
        # 1) 定位 draft 原子：summary 全等 → 章节重叠兜底
        cands = by_sum.get(a.get("summary", ""), [])
        if cands:
            link = sorted(cands, key=lambda r: (r["slice"], r["local_seq"]))[0]
            how = "summary-exact"
        else:
            pool = [r for lst in empty_by_slice.values() for r in lst
                    if overlap(cs, ce, r["cs"], r["ce"]) > 0]
            if not pool:
                rows.append({"book": BOOK_HM, "chapter": f"c{cs}~{ce}", "seq": a.get("seq"),
                             "link": None, "how": "no-draft-link", "category": None,
                             "new_id": None, "action": "keep-empty",
                             "reason": "草稿里找不到对应原子，无 no_fit 依据，禁止硬造 ID"})
                continue
            link = sorted(pool, key=lambda r: (-overlap(cs, ce, r["cs"], r["ce"]),
                                               r["slice"], r["local_seq"]))[0]
            how = "chapter-overlap"
        # 2) 在该 slice 的 no_fit 条目里按章节重叠取类别
        nfs = [e for e in per_slice.get(link["slice"], [])
               if e["range"] and overlap(cs, ce, e["range"][0], e["range"][1]) > 0]
        if not nfs:
            rows.append({"book": BOOK_HM, "chapter": f"c{cs}~{ce}", "seq": a.get("seq"),
                         "link": f"{link['slice']}#{link['local_seq']}", "how": how,
                         "category": None, "new_id": None, "action": "keep-empty",
                         "reason": "该切片 no_fit 无章节重叠条目（判空但未归类），保持空 ID"})
            continue
        nfs.sort(key=lambda e: (-overlap(cs, ce, e["range"][0], e["range"][1]), e["idx"]))
        best = nfs[0]
        cats = sorted({str(e["category"]) for e in nfs})
        cat = best["category"]
        new_id = CATEGORY_TO_ID.get(cat)
        row = {"book": BOOK_HM, "chapter": f"c{cs}~{ce}", "seq": a.get("seq"),
               "link": f"{link['slice']}#{link['local_seq']}", "how": how,
               "category": cat, "new_id": new_id,
               "n_overlap_nofit": len(nfs), "distinct_categories": cats,
               "ambiguous": len(cats) > 1, "no_fit_what": best["what"],
               "nofit_chapters": best["chapters"]}
        if new_id:
            row["action"] = "backfill"
        elif cat in CATEGORY_KEEP_EMPTY:
            row["action"] = "keep-empty"
            row["reason"] = CATEGORY_KEEP_EMPTY[cat]
        else:
            row["action"] = "keep-empty"
            row["reason"] = "no_fit 关键词规则未归类，保持空 ID"
        rows.append(row)

    # 斗破/凡人 p2 空 ID：无结构化 no_fit 依据（F6A/F6B 草稿只有自由文本 note）
    for b, tag in [(BOOK_DP, "斗破"), (BOOK_FR, "凡人p2")]:
        for a in load_json(original_of(b))["atoms"]:
            if (a.get("atomic_id") or "").strip():
                continue
            rows.append({"book": b, "chapter": f"c{a.get('chapter_start')}~{a.get('chapter_end')}",
                         "seq": a.get("seq"), "link": None, "how": "no-structured-nofit",
                         "category": None, "new_id": None, "action": "keep-empty",
                         "tags": a.get("tags"), "summary_head": (a.get("summary") or "")[:60],
                         "reason": f"{tag} 草稿无结构化 no_fit 字段；该原子语义为修仙专属"
                                   f"（猎兽取材/收服凶魂/婚仪托孤/以势压人），不落 13 条转正类"})
    return {"rule_source": "outputs/f6c/_tools/agg_proposals.py RULES",
            "category_to_id": CATEGORY_TO_ID, "keep_empty_reasons": CATEGORY_KEEP_EMPTY,
            "rows": rows}


def summarize_plan(plan: dict) -> dict:
    rows = plan["rows"]
    bf = [r for r in rows if r["action"] == "backfill"]
    keep = [r for r in rows if r["action"] == "keep-empty"]
    by_cat: dict[str, int] = {}
    for r in bf:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    by_reason: dict[str, int] = {}
    for r in keep:
        key = r.get("reason", "")[:40]
        by_reason[key] = by_reason.get(key, 0) + 1
    return {"total_empty_seen": len(rows), "backfill": len(bf), "keep_empty": len(keep),
            "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
            "keep_empty_by_reason": by_reason,
            "ambiguous_rows": sum(1 for r in rows if r.get("ambiguous")),
            "link_how": {h: sum(1 for r in rows if r.get("how") == h)
                         for h in ("summary-exact", "chapter-overlap",
                                   "no-draft-link", "no-structured-nofit")}}


def backfill_dry() -> int:
    plan = build_plan()
    s = summarize_plan(plan)
    print("== 空 ID 回填 dry-run ==")
    print(f"扫描到空 ID 原子: {s['total_empty_seen']}")
    print(f"  将回填: {s['backfill']}   保持空: {s['keep_empty']}")
    print(f"  链接方式: {s['link_how']}")
    print(f"  多类别歧义条目: {s['ambiguous_rows']}（按最长章节重叠取类别）")
    print("\n回填按类别分布:")
    for k, v in s["by_category"].items():
        print(f"   {v:4d}  {CATEGORY_TO_ID[k]}  {k}")
    print("\n保持空的理由分布:")
    for k, v in s["keep_empty_by_reason"].items():
        print(f"   {v:4d}  {k}")
    dump_json(OUT / "回填计划_dry.json", plan)
    print(f"\n计划 → {OUT / '回填计划_dry.json'}")
    tot = s["backfill"] + s["keep_empty"]
    print(f"合计核对: {s['backfill']}+{s['keep_empty']}={tot} (应=280 寒门+3 斗破+1 凡人p2=284)")
    return 0 if tot == s["total_empty_seen"] else 1


def ensure_backup(name: str) -> Path:
    src = RAW / name
    bak = RAW / (name + BAK)
    if not bak.exists():
        shutil.copy2(src, bak)
        print(f"   [backup] {name} → {name + BAK} (src sha={sha(src)})")
    return bak


def backfill_apply() -> int:
    rc = backfill_dry()
    if rc:
        print("[apply] 计划自检失败，未写任何文件")
        return rc
    plan = load_json(OUT / "回填计划_dry.json")
    todo = [r for r in plan["rows"] if r["action"] == "backfill"]
    by_book: dict[str, dict] = {}
    for r in todo:
        by_book.setdefault(r["book"], {})[f"{r['chapter']}|{r['seq']}"] = r["new_id"]
    stamp = {}
    for book, mapping in by_book.items():
        ensure_backup(book)
        data = load_json(current_of(book))
        n = 0
        in_place = 0
        for a in data["atoms"]:
            key = f"c{a.get('chapter_start')}~{a.get('chapter_end')}|{a.get('seq')}"
            if key not in mapping:
                continue
            if (a.get("atomic_id") or "").strip():
                if a["atomic_id"] == mapping[key]:
                    in_place += 1
                continue
            a["atomic_id"] = mapping[key]
            tags = [t for t in (a.get("tags") or []) if t != NOFIT_TAG]
            a["tags"] = tags
            n += 1
            in_place += 1
        dump_json(RAW / book, data)
        # in_place=计划该有的 ID 现在实际有多少条（重跑不变）；filled_now=本次新写几条。
        # 只记 filled_now 会让二次跑的台账显示 0，读的人以为回填没生效。
        stamp[book] = {"in_place": in_place, "filled_now": n,
                       "planned": len(mapping),
                       "written_content_sha": content_sha(sorted(mapping.items()))}
        print(f"   [{book}] 计划 {len(mapping)} 条 / 已到位 {in_place} 条"
              f"（本次新写 {n} 条）→ 内容哈希 {stamp[book]['written_content_sha']}")
    # filled_now 只进日志，不进台账：台账必须与起跑状态无关，可被任何人复现
    ledger = {"stamp": {k: {kk: vv for kk, vv in v.items() if kk != "filled_now"}
                        for k, v in stamp.items()}, **plan}  # filled_now 只进日志
    dump_json(LEDGER, ledger)
    print(f"台账 → {LEDGER}")
    return 0


# ---------------------------------------------------------------------------
# 二、F6B 流程元话术清洗
# ---------------------------------------------------------------------------
def strip_meta(summary: str) -> tuple[str, list[str]]:
    """删除描述切分流程的分句；返回新 summary 与命中的话术词。

    只删**含话术词的那一小句**——同一段里破折号前的剧情内容必须留下（QA P1）。
    """
    if not summary:
        return summary, []
    hits: list[str] = []
    parts = re.split(CLAUSE_SPLIT, summary)
    # parts = [clause, sep, clause, sep, ...]
    kept, dropped = [], []
    i = 0
    while i < len(parts):
        clause = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        text = clause
        if any(p in text for p in META_SAFE_KEEP):
            kept.append(text + sep)
        elif any(p in text for p in META_PHRASES):
            dropped.append(text + sep)
            hits += [p for p in META_PHRASES if p in text]
        else:
            kept.append(text + sep)
        i += 2
    new = "".join(kept).rstrip()
    if dropped:
        new = new.rstrip("，；—")
        if not new.endswith("。"):
            new += "。"
    return new, hits


# QA S3：话术一律**就地删除**，不再往 tags 追加「未收尾/素材未写」这类控制标签。
# 理由：① 任务单 §二.4 本就允许「挪到 tags **或删除**」；② 该信息已由 windows 的
# locked_to/reach_end 机制承载；③ 追加会让 tags 从 3~4 涨到 5~6（实测 16 个原子越界），
# 且 tags 会被 skel_v4_rebuild 原样并进 variants，污染 SK07 的模板拍骨架。
# 分组的意义只保留在台账里（供 PM 回溯是哪类话术），不落库到 atom。
META_TAG_WINDOW = ["未收尾", "续判", "下窗", "上一窗", "本窗", "前窗", "留待下窗"]
META_TAG_SOURCE = ["原文未写", "素材未写"]


def meta_plan() -> list[dict]:
    rows = []
    for book in BOOKS_META:
        for idx, a in enumerate(load_json(original_of(book))["atoms"]):
            new, hits = strip_meta(a.get("summary", ""))
            if hits:
                kinds = (["跨窗未收尾"] if any(x in META_TAG_WINDOW for x in hits) else []) \
                    + (["素材未写"] if any(x in META_TAG_SOURCE for x in hits) else [])
                rows.append({"book": book, "index": idx,
                             "chapter": f"c{a.get('chapter_start')}~{a.get('chapter_end')}",
                             "seq": a.get("seq"), "phrases": sorted(set(hits)),
                             "meta_kind": kinds,
                             "dropped": (a.get("summary", "") or "")[-60:],
                             "new_summary": new,
                             "tags_before": list(a.get("tags") or [])})
    return rows


def meta_dry() -> int:
    rows = meta_plan()
    print("== 流程元话术清洗 dry-run（范围=F6B 斗破 p2）==")
    print(f"命中原子 {len(rows)} 条（F6B 报告观察项记 25 条）")
    from collections import Counter
    print("话术词计数:", dict(Counter(p for r in rows for p in r["phrases"])))
    for r in rows:
        print(f"\n  {r['book']} {r['chapter']} seq{r['seq']} 命中{r['phrases']}")
        print(f"    删句: …{r['dropped']}")
        print(f"    改后尾: …{r['new_summary'][-60:]}")
    dump_json(OUT / "元话术清洗计划_dry.json", {"rows": rows})
    print(f"\n计划 → {OUT / '元话术清洗计划_dry.json'}")
    return 0


def meta_apply() -> int:
    rows = meta_plan()
    stamp = {}
    for book in BOOKS_META:
        ensure_backup(book)
        data = load_json(current_of(book))
        n = 0
        for r in rows:
            if r["book"] != book:
                continue
            a = data["atoms"][r["index"]]
            if (f"c{a.get('chapter_start')}~{a.get('chapter_end')}", a.get("seq")) \
                    != (r["chapter"], r["seq"]):
                print(f"   [拒绝] {book} index={r['index']} 定位漂移："
                      f"当前原子 c{a.get('chapter_start')}~{a.get('chapter_end')} "
                      f"≠ 计划 {r['chapter']}，不写文件")
                return 1
            assert (f"c{a.get('chapter_start')}~{a.get('chapter_end')}", a.get("seq")) \
                == (r["chapter"], r["seq"]), f"{book} 落盘目标与计划不一致"
            a["summary"] = r["new_summary"]
            n += 1
        dump_json(RAW / book, data)
        mine = [r for r in rows if r["book"] == book]
        in_place = sum(1 for r in mine
                       if data["atoms"][r["index"]]["summary"] == r["new_summary"])
        stamp[book] = {"planned": len(mine), "in_place": in_place,
                       "written_content_sha": content_sha(
                           [[r["index"], r["new_summary"]] for r in mine])}
        print(f"   [{book}] 计划 {stamp[book]['planned']} 条 / 已到位 {in_place} 条"
              f"（本次新写 {n} 条）→ 内容哈希 {stamp[book]['written_content_sha']}")
    path = OUT / "元话术清洗台账.json"
    dump_json(path, {"book": BOOKS_META, "stamp": stamp, "rows": rows})
    print(f"台账 → {path}")
    return 0


# ---------------------------------------------------------------------------
# 二·补、D08 误用重标（任务单 §二.2：凡人 base c203~204；斗破 c1421~1423 不动）
# ---------------------------------------------------------------------------
D08_REDECISION = {
    "book": BOOK_FR_BASE, "cs": 203, "ce": 204, "seq": 10,
    "from_id": "D08", "to_id": "D01",
    "tag_swap": {"记忆遗失恢复": "压制知情者"},
    "evidence": (
        "原文《第二百零二章 灭口》《第二百零三章 无忧针与忘尘丹》：主角为救人当众灭杀巨剑门强者，"
        "目击者菡云芝成为唯一知情人；主角趁其转身一掌击晕、抱入石洞，以无忧针法清除其短期记忆、"
        "喂忘尘丸，自陈「要不是有这套无忧针法，可以清除人的短期记忆，否则我还真不知道，"
        "该如何应付泄密之事」「你只会忘记了半日内所发生的一切」。"
        "D08 定义=「记忆缺失或被篡改后的寻回」，此处是**施加**失忆且无寻回，判 D08 为误用；"
        "D01 身份暴露四拍的合拍明写「改换身份离开原地，或反手压制知情者」——"
        "藏拙者行迹被目睹 → 泄密风险 → 反手压制知情者，链条逐项命中，故重判 D01。"),
}


def d08_locate():
    data = load_json(RAW / D08_REDECISION["book"])
    hits = []
    for i, a in enumerate(data["atoms"]):
        if (a.get("chapter_start"), a.get("chapter_end"), a.get("seq")) == (
                D08_REDECISION["cs"], D08_REDECISION["ce"], D08_REDECISION["seq"]):
            hits.append((i, a))
    return data, hits


def d08_dry() -> int:
    r = D08_REDECISION
    data, hits = d08_locate()
    print(f"== D08 重标 dry-run == {r['book']} c{r['cs']}~{r['ce']} seq{r['seq']}")
    if len(hits) != 1:
        print(f"  [FAIL] 定位到 {len(hits)} 条，应为 1 条")
        return 1
    i, a = hits[0]
    print(f"  现 atomic_id={a['atomic_id']} tags={a.get('tags')}")
    print(f"  将改 atomic_id={r['from_id']}→{r['to_id']}，tags 替换 {r['tag_swap']}")
    print(f"  summary 不动（任务单只要求重标 ID）")
    print(f"  依据：{r['evidence']}")
    others = [(b, x.get("chapter_start"), x.get("chapter_end"))
              for b in BOOKS_ALL
              for x in load_json(RAW / b)["atoms"] if x.get("atomic_id") == r["from_id"]]
    print(f"  全量 D08 现用位置: {others}（斗破 c1421~1423 合规不动；"
          f"遮天 c1700~1702 不在本单范围，见回执）")
    return 0


def d08_apply() -> int:
    r = D08_REDECISION
    data, hits = d08_locate()
    if len(hits) != 1:
        print(f"[apply] 定位失败 {len(hits)} 条，未写文件")
        return 1
    i, a = hits[0]
    if a.get("atomic_id") != r["to_id"]:
        assert a.get("atomic_id") == r["from_id"], f"现 ID 非 {r['from_id']}: {a.get('atomic_id')}"
        a["atomic_id"] = r["to_id"]
    a["tags"] = [r["tag_swap"].get(t, t) for t in (a.get("tags") or [])]
    ensure_backup(r["book"])
    dump_json(RAW / r["book"], data)
    print(f"[apply] {r['book']} c{r['cs']}~{r['ce']} seq{r['seq']} → {r['to_id']} "
          f"tags={a['tags']} sha={sha(RAW / r['book'])}")
    path = OUT / "D08重标台账.json"
    dump_json(path, {"decision": r, "atom_index": i,
                     "written_content_sha": content_sha(
                         [r["to_id"], a["atomic_id"], a["tags"]])})
    print(f"台账 → {path}")
    return 0


# ---------------------------------------------------------------------------
# 二·补2、F6B 内容级小错回修落盘（任务单 §二.3）
#         修正文本由 4 个「读原文核查」子代理产出在 outputs/sk05/_work/repair_*.json，
#         本阶段只做落盘 + 硬校验（四拍齐全 / 80~250 字 / 定位唯一）。
# ---------------------------------------------------------------------------
REPAIR_DIR = OUT / "_work"
BEAT_RE = re.compile(r"^起：.+｜承：.+｜转：.+｜合：.+$", re.S)


def repair_items() -> list[dict]:
    items = []
    for p in sorted(REPAIR_DIR.glob("repair_*.json")):
        doc = load_json(p)
        for it in doc.get("items", []):
            it["_src"] = p.name
            it["_group"] = doc.get("group")
            items.append(it)
    return items


def repair_validate(it: dict) -> list[str]:
    errs = []
    verdict = it.get("verdict", "")
    if "属实" not in verdict:          # 「报告说法不成立」= 误报，不改
        return []
    new = it.get("new_summary") or ""
    old = it.get("old_summary") or ""
    if not new:
        errs.append("new_summary 为空")
        return errs
    if new == old:
        errs.append("verdict=属实需改 但 new_summary 与 old 相同（未给修正文本）")
    if not BEAT_RE.match(new):
        errs.append("四拍格式不合格（须 起：…｜承：…｜转：…｜合：…）")
    n = len(new)
    if not (80 <= n <= 250):
        errs.append(f"长度 {n} 不在 80~250")
    return errs


def repair_dry() -> int:
    items = repair_items()
    print(f"== F6B 回修落盘 dry-run == 收到子代理结论 {len(items)} 条")
    data = load_json(RAW / BOOK_DP)
    n_apply = n_reject = 0
    for it in items:
        loc = [a for a in data["atoms"] if (a.get("chapter_start"), a.get("chapter_end"),
                                            a.get("seq")) == (it.get("chapter_start"),
                                                               it.get("chapter_end"),
                                                               it.get("seq"))]
        verdict = it.get("verdict", "")
        tag = f"[{it['_group']}] {it.get('issue')} seq{it.get('seq')} c{it.get('chapter_start')}~{it.get('chapter_end')}"
        if len(loc) != 1:
            print(f"  [定位异常] {tag} → 命中 {len(loc)} 条，跳过")
            n_reject += 1
            continue
        a = loc[0]
        if "属实" not in verdict:
            print(f"  [误报不改] {tag} verdict={verdict}")
            n_reject += 1
            continue
        errs = repair_validate(it)
        if errs:
            print(f"  [校验失败] {tag} {errs}")
            n_reject += 1
            continue
        print(f"  [将改] {tag}")
        print(f"      old: {(a.get('summary') or '')[:78]}…")
        print(f"      new: {(it['new_summary'])[:78]}…")
        if it.get("new_tags") is not None and it["new_tags"] != (a.get("tags") or []):
            print(f"      tags: {a.get('tags')} → {it['new_tags']}")
        n_apply += 1
    print(f"\n可落盘 {n_apply} 条 / 跳过 {n_reject} 条（误报、定位异常或校验不过）")
    return 0 if n_apply else 1


def repair_apply() -> int:
    items = repair_items()
    data = load_json(current_of(BOOK_DP))
    records, bad = [], 0
    for it in items:
        rec = {"group": it["_group"], "issue": it.get("issue"), "seq": it.get("seq"),
               "chapter": it.get("chapter_start"), "chapter_end": it.get("chapter_end"),
               "verdict": it.get("verdict", ""),
               "evidence": it.get("evidence", "")[:220],
               # P2：台账必须自带改前/改后，否则交付物无法自证（QA 只能反查 .bak）
               "old_summary": it.get("old_summary", ""), "new_summary": it.get("new_summary", ""),
               "old_tags": it.get("old_tags"), "new_tags": it.get("new_tags")}
        if "属实" not in rec["verdict"]:
            rec["status"] = "misreport-not-changed"
            print(f"  [误报不改] {rec['issue']} seq{rec['seq']}")
            records.append(rec)
            continue
        errs = repair_validate(it)
        hit = [a for a in data["atoms"] if (a.get("chapter_start"), a.get("chapter_end"),
                                            a.get("seq")) == (it.get("chapter_start"),
                                                               it.get("chapter_end"),
                                                               it.get("seq"))]
        if errs or len(hit) != 1:
            rec["status"] = "rejected"
            rec["why"] = errs or [f"定位命中 {len(hit)} 条"]
            bad += 1
            print(f"  [拒绝] {rec['issue']} {rec['why']}")
            records.append(rec)
            continue
        a = hit[0]
        changed = []
        if a.get("summary") != it["new_summary"]:
            a["summary"] = it["new_summary"]
            changed.append("summary")
        if it.get("new_tags") is not None and (a.get("tags") or []) != it["new_tags"]:
            a["tags"] = it["new_tags"]
            changed.append("tags")
        # status 记**终态**（该原子的 summary/tags 是否已等于核查结论），
        # 不记"本次有没有写"——否则二次跑会让台账整份变样（同 P4① 那类缺陷）。
        rec["_changed_now"] = bool(changed)
        rec["status"] = "in-place"
        rec["fields"] = [f for f in ("summary", "tags")
                         if (f == "summary" and a.get("summary") == it["new_summary"])
                         or (f == "tags" and it.get("new_tags") is not None
                             and (a.get("tags") or []) == it["new_tags"])]
        # changed（本次是否真写了字节）只进日志：台账要能被任何人重跑复现
        records.append(rec)
        print(f"  [{'改' if changed else '已达标'}] {rec['issue']} seq{a['seq']} {changed}")
    if bad:
        print(f"[apply] 有 {bad} 条被拒，未写任何文件")
        return 1
    ensure_backup(BOOK_DP)
    dump_json(RAW / BOOK_DP, data)
    dump_json(OUT / "F6B回修台账.json",
              {"book": BOOK_DP,
               "records": [{k: v for k, v in r.items() if k != "_changed_now"}
                           for r in records],
               "written_content_sha": content_sha(
                   [[r["chapter"], r["seq"], r.get("new_summary", "")] for r in records])})
    n_ap = sum(1 for r in records if r["status"] == "in-place")
    n_now = sum(1 for r in records if r.get("_changed_now"))
    n_ms = sum(1 for r in records if r["status"] == "misreport-not-changed")
    print(f"到位 {n_ap} 条（本次新写 {n_now} 条）/ 误报不改 {n_ms} 条 → sha {sha(RAW / BOOK_DP)}")
    print(f"台账 → {OUT / 'F6B回修台账.json'}")
    return 0


# ---------------------------------------------------------------------------
# 二·补3、A04/F03 存量标注复扫（任务单 §三.1 的「复扫无异常」项）
#   定义放宽后存量标注自动合法，但「自动合法」需要用机检证明，而不是断言。
#   判据：F03 应含造物语素；A04 应含设伏方或被伏方语素（新定义两向均可）。
#   不含者不算 FAIL，逐条列成待裁清单交 PM。
# ---------------------------------------------------------------------------
MAKE_TOKENS = ["造", "铸", "锻", "打造", "改装", "组装", "制成", "试制", "炉", "作坊",
               "器", "械", "刀", "甲", "车", "弓", "炮", "雷", "锤", "炼"]
SETUP_TOKENS = ["设伏", "埋伏", "暗箭", "偷袭", "设局", "布局", "暗杀", "伏击", "圈套",
                "伏兵", "诱", "伪装", "暗中", "杀手", "突袭", "响箭", "陷阱", "伏着",
                "围杀", "围剿", "下毒", "毒杀", "暗算", "藏", "盯", "窥"]
VICTIM_TOKENS = ["落入", "中计", "遭暗", "被伏", "陷入", "遭袭", "中伏", "上当",
                 "察觉", "遭遇", "追杀", "锁定", "追击"]


def rescan() -> int:
    out = {"判据": {"F03": "summary 应含造物语素", "A04": "summary 应含设伏方或被伏方语素"},
           "counts": {}, "suspicious": {"F03": [], "A04": []}, "notes": {}}
    for b in BOOKS_ALL + ["win_九星霸体诀.json", "win_遮天.json", "win_太荒吞天诀.json",
                          "win_圣墟.json", "win_蛊真人.json"]:
        p = RAW / b
        if not p.exists():
            continue
        for a in load_json(p)["atoms"]:
            _id = a.get("atomic_id")
            if _id not in ("F03", "A04"):
                continue
            s = a.get("summary", "") or ""
            out["counts"].setdefault(_id, {}).setdefault(b, 0)
            out["counts"][_id][b] += 1
            if _id == "F03" and not any(k in s for k in MAKE_TOKENS):
                out["suspicious"]["F03"].append({"book": b, "chapter": f"c{a.get('chapter_start')}~{a.get('chapter_end')}",
                                                 "seq": a.get("seq"), "summary": s[:160]})
            if _id == "A04" and not any(k in s for k in SETUP_TOKENS + VICTIM_TOKENS):
                out["suspicious"]["A04"].append({"book": b, "chapter": f"c{a.get('chapter_start')}~{a.get('chapter_end')}",
                                                 "seq": a.get("seq"), "summary": s[:160]})
    for k in out["counts"]:
        out["counts"][k]["合计"] = sum(v for kk, v in out["counts"][k].items())
    # SK04 §2.4 点名的借用噪声（寒门 c775~776 讲普及教育）是否仍在
    hm = load_json(RAW / BOOK_HM)
    out["notes"]["SK04§2.4 点名的 F03 借用噪声"] = [
        {"chapter": f"c{a.get('chapter_start')}~{a.get('chapter_end')}", "head": a["summary"][:90]}
        for a in hm["atoms"]
        if a.get("atomic_id") == "F03"
        and any(k in a.get("summary", "") for k in ("普及教育", "兴学", "蒙学"))]
    dump_json(OUT / "A04F03复扫.json", out)
    print("== A04/F03 存量复扫 ==")
    print("用量:", {k: v["合计"] for k, v in out["counts"].items()},
          "（三书口径见 verify：A04=111 / F03=35）")
    print(f"F03 可疑（无造物语素）{len(out['suspicious']['F03'])} 条；"
          f"A04 可疑（双向语素全无）{len(out['suspicious']['A04'])} 条 —— 全库口径")
    for r in out["suspicious"]["F03"]:
        print(f"   F03 {r['book']} {r['chapter']}: {r['summary'][:70]}")
    for r in out["suspicious"]["A04"][:8]:
        print(f"   A04 {r['book']} {r['chapter']}: {r['summary'][:70]}")
    print("SK04 §2.4 点名的借用噪声:", out["notes"]["SK04§2.4 点名的 F03 借用噪声"])
    print(f"→ {OUT / 'A04F03复扫.json'}")
    return 0


# ---------------------------------------------------------------------------
# 三、verify（任务单 §三 的 win 侧项）
# ---------------------------------------------------------------------------
def verify() -> int:
    res = []
    def chk(name, ok, detail=""):
        res.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              f"{(' — ' + str(detail)) if detail else ''}")

    con = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ids = {r["id"]: r["status"] for r in con.execute(f"SELECT id, status FROM atomic_events")}
    con.close()
    legal = set(ids)
    act = {k for k, v in ids.items() if v == "active"}
    chk("库词表口径", len(act) == 76 and len(legal) == 77,
        f"active {len(act)} / 总 {len(legal)}")

    total_empty, cand_left = 0, []
    for book in BOOKS_ALL:
        p = RAW / book
        data = load_json(p)
        atoms = data["atoms"]
        out_of_range = [a for a in atoms if (a.get("atomic_id") or "").strip()
                        and a["atomic_id"] not in legal]
        empty = [a for a in atoms if not (a.get("atomic_id") or "").strip()]
        badtag = [a for a in atoms if (a.get("atomic_id") or "").strip()
                  and NOFIT_TAG in (a.get("tags") or [])]
        chk(f"{book} 词表越界 = 0", not out_of_range,
            f"{[(a['atomic_id'], a['chapter_start']) for a in out_of_range][:5]}")
        chk(f"{book} 非空 ID 无「无合适原子」残留标", not badtag,
            f"{[(a['atomic_id'], a['chapter_start']) for a in badtag][:5]}")
        total_empty += len(empty)
        if book == BOOK_HM:
            print(f"   ({book} 空 ID 剩 {len(empty)} 条，均为候选类/未归类)")
        for a in empty:
            cand_left.append((book, a.get("chapter_start"), a.get("chapter_end"),
                              tuple(a.get("tags") or [])))
    chk("三书空 ID 合计 = 45（寒门 41 + 斗破 3 + 凡人 p2 1）", total_empty == 45,
        f"实测 {total_empty}")
    # QA P5：任务单 §三.1 写「76 active 口径」，但 §一.2 又明令 D08 维持 candidate 不动，
    # 而斗破 c1421~1423 语义上确实最贴 D08。三条口径互斥 → 不自判，只把引用数报出来。
    cand = {i for i, st in ids.items() if st != "active"}
    refs = [(b, a.get("chapter_start"), a.get("chapter_end"), a["atomic_id"])
            for b in BOOKS_ALL
            for a in load_json(RAW / b)["atoms"]
            if (a.get("atomic_id") or "") in cand]
    print(f"  [上报] 引用 candidate ID 的原子 = {len(refs)} 处 {refs}"
          f"（判越界用的是 {len(legal)} 全表口径；若按 76 active 口径这些即算越界，待 PM 裁）")
    if LEDGER.exists():
        lg = load_json(LEDGER)
        st = lg.get("stamp", {}).get(BOOK_HM, {})
        planned_bf = sum(1 for r in lg.get("rows", []) if r["action"] == "backfill")
        chk("回填台账自洽：计划数 = 已到位数 = 239",
            st.get("planned") == st.get("in_place") == planned_bf == 239,
            json.dumps(st, ensure_ascii=False))
    else:
        chk("回填台账存在", False, str(LEDGER))

    # 元话术复扫 0（范围=F6B）
    leftover = []
    for book in BOOKS_META:
        for a in load_json(RAW / book)["atoms"]:
            s = a.get("summary", "")
            if any(p in s for p in META_PHRASES) and not any(k in s for k in META_SAFE_KEEP):
                leftover.append((book, a.get("chapter_start"), s[-40:]))
    chk("F6B summary 流程元话术复扫 = 0", not leftover, leftover[:5])
    # 「留待盘问」这类剧情用语必须还在（防清洗过宽）
    keep_ok = any("留待盘问" in a.get("summary", "")
                  for a in load_json(RAW / BOOK_DP)["atoms"])
    chk("剧情用语「留待盘问」未被误删", keep_ok)

    # A04/F03 存量标注复扫（机检口径：用量对账 + 矛盾态）
    counts = {}
    for book in BOOKS_ALL:
        for a in load_json(RAW / book)["atoms"]:
            counts.setdefault(a.get("atomic_id") or "__empty__", [0, {}])
            counts[a.get("atomic_id") or "__empty__"][0] += 1
            counts[a.get("atomic_id") or "__empty__"][1][book] = \
                counts[a.get("atomic_id") or "__empty__"][1].get(book, 0) + 1
    for _id, want in (("A04", 111), ("F03", 35)):
        got = counts.get(_id, [0])[0]
        chk(f"{_id} 三书用量对账 = {want}（SK04 §2.5 矩阵）", got == want,
            f"实测 {got}，分布 {counts.get(_id, [0, {}])[1]}")

    # D08 重标（§二.2）
    d08 = {b: [(a.get("chapter_start"), a.get("chapter_end")) for a in load_json(RAW / b)["atoms"]
               if a.get("atomic_id") == "D08"] for b in BOOKS_ALL}
    chk("凡人 base c203~204 已改判 D01（该书 D08 归零）", not d08[BOOK_FR_BASE], d08[BOOK_FR_BASE])
    chk("斗破 p2 c1421~1423 的 D08 保持不动（SK04 判合规）",
        d08[BOOK_DP] == [(1421, 1423)], d08[BOOK_DP])
    tgt = next((a for a in load_json(RAW / BOOK_FR_BASE)["atoms"]
                if (a.get("chapter_start"), a.get("chapter_end")) == (203, 204)), {})
    chk("重标行 tags 已换掉「记忆遗失恢复」",
        "记忆遗失恢复" not in (tgt.get("tags") or []), tgt.get("tags"))
    print("\nverify:", sum(1 for x in res if x), "/", len(res), "PASS")
    return 0 if all(res) else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--backfill-dry", action="store_true")
    g.add_argument("--backfill-apply", action="store_true")
    g.add_argument("--meta-dry", action="store_true")
    g.add_argument("--meta-apply", action="store_true")
    g.add_argument("--d08-dry", action="store_true")
    g.add_argument("--d08-apply", action="store_true")
    g.add_argument("--repair-dry", action="store_true")
    g.add_argument("--repair-apply", action="store_true")
    g.add_argument("--rescan", action="store_true")
    g.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    mode = ("backfill_dry" if a.backfill_dry else "backfill_apply" if a.backfill_apply
            else "meta_dry" if a.meta_dry else "meta_apply" if a.meta_apply
            else "d08_dry" if a.d08_dry else "d08_apply" if a.d08_apply
            else "repair_dry" if a.repair_dry else "repair_apply" if a.repair_apply
            else "rescan" if a.rescan else "verify")
    OUT.mkdir(parents=True, exist_ok=True)
    sys.stdout = _Tee(OUT / f"sk05_win_{mode}.txt")
    print(f"[DEV-SK05] mode={mode}")
    rc = {"backfill_dry": backfill_dry, "backfill_apply": backfill_apply,
          "meta_dry": meta_dry, "meta_apply": meta_apply,
          "d08_dry": d08_dry, "d08_apply": d08_apply,
          "repair_dry": repair_dry, "repair_apply": repair_apply,
          "rescan": rescan, "verify": verify}[mode]()
    print(f"[DEV-SK05] {mode} rc={rc}")
    sys.stdout.flush()
    sys.exit(rc)


if __name__ == "__main__":
    main()
