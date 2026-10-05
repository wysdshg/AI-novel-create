# -*- coding: utf-8 -*-
"""[DEV-SK05B] 全量语义复核落盘 + 9 条退空 + 验收。

复用 SK05 的判据（legal_ids 口径 / 备份纪律 / 台账只记计划值），但独立成段，
因为本单跨 10 个 win 文件（池 B 是全库 F03/F07），且台账要落 outputs/sk05b/。

    .venv\\Scripts\\python.exe backend/scripts/sk05b_recheck_full.py --plan
    ... --apply / --verify
规范顺序：--plan → --apply → --verify（apply 幂等，可重跑）

三条来自 SK05 的硬教训已内置：
  · 台账字段一律取**计划值**（池文件里的现值），不取落盘时文件现值（E17 纪律④）；
  · 派工输入缺 summary 的作废批次被隔离在 _work/_作废_*/，glob 不捡（E18）；
  · evidence 必须是该原子 summary 的**原文子串**，否则整单拒收（E18 解法 3）。

SK05c 修补（2026-10-05，[DEV-SK05C]）：c1791~1792 经 PM 裁定采纳池 B 异议（见 PM_ADOPTED——
撤销退空、按池复核结论留 F07）；verify 的存量遗留两行改为修毕态；清单加 F07 措辞盲区备注。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "outputs" / "_atomic_raw"
S05 = ROOT / "outputs" / "sk05"
S05B = ROOT / "outputs" / "sk05b"
WORK = S05B / "_work"
BAK_SUFFIX = ".bak-sk05b"
NOFIT_TAG = "无合适原子"
SKIPPED_BY_RECHECK: list[dict] = []   # 退空让位记录（每次 collect 重置）
CONFLICTS: list[dict] = []            # PM 命令与本单复核意见相左的条目
# SK05c（2026-10-05）：PM 裁定采纳本单复核异议——该条撤销退空、按池复核结论落盘（留 F07）。
# 键格式与 retire_directives/collect 内部一致：(book, chapter, seq)。
PM_ADOPTED = {("win_寒门枭士.json", "c1791~1792", 17)}
PROD_DB = r"C:/Users/w3013/.ai_novel/data/novel_agent.db"
# 越界口径（PM 裁决决定一）：76 active + D08 试用期合法
TRIAL_OK = {"D08"}


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


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def content_sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()[:16]


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def dump(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def legal_ids() -> set[str]:
    import sqlite3
    con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    ok = {r[0] for r in con.execute("SELECT id, status FROM atomic_events")
          if r[1] == "active"} | TRIAL_OK
    con.close()
    return ok


def chapter_pair(txt: str):
    n = re.findall(r"(\d+)", str(txt))
    return (int(n[0]), int(n[1])) if len(n) >= 2 else (None, None)


def read_pool(name: str) -> list[dict]:
    """池文件（含 summary），按批次号稳定排序合并。"""
    items = []
    for f in sorted(WORK.glob(name), key=lambda p: int(re.findall(r"(\d+)", p.stem)[-1])):
        for it in load(f)["items"]:
            it["_batch"] = f.stem.replace("pool", "")
            items.append(it)
    return items


def atoms_of(book: str) -> list[dict]:
    return load(RAW / book)["atoms"]


# ---------------------------------------------------------------------------
# 一、把子代理结论与池输入对齐成落盘指令
# ---------------------------------------------------------------------------
def evidence_grounded(ev: str, summary: str) -> tuple[bool, str]:
    """evidence 必须能在 summary 里落地，但允许**节选**（子代理常用 …、｜、。拼接片段）。

    做法：按省略号/句读切成片段，≥6 字的片段逐段要求是原文子串；
    全部片段都能落地 = 有据；出现落不了地的片段 = 疑似编造，拒收。
    返回 (是否通过, 不通过的片段)。
    """
    if not ev:
        return True, ""          # 判空/confirm 允许不带 evidence（reason 里有 definition 引证）
    # 分隔符要覆盖子代理实际用到的写法：全角斜杠／竖线｜省略号句读，都可能用来拼接节选
    frags = [f.strip() for f in re.split(r"…|\.\.\.|｜|\||／|/|。|；|、", ev) if len(f.strip()) >= 6]
    if not frags:                # 全是短片段，退回整体子串判断
        return (ev in summary), ("" if ev in summary else ev)
    for f in frags:
        if f not in summary:
            return False, f
    return True, ""


def build_directives(retired_keys: frozenset = frozenset()) -> tuple[list[dict], list[str]]:
    legal = legal_ids()
    errs: list[str] = []
    poolA = {("win_寒门枭士.json", x["chapter"], x["seq"]): x for x in read_pool("poolA_batch*.json")}
    poolB = {(x["book"], x["chapter"], x["seq"]): x for x in read_pool("poolB_batch*.json")}
    pools = {**poolA, **poolB}
    out: list[dict] = []
    seen: set[tuple] = set()

    for f in sorted(WORK.glob("recheck_out_*.json"), key=lambda p: p.name):
        if "_作废" in f.parts:
            continue
        doc = load(f)
        for it in doc.get("items", []):
            key = None
            for (bk, ch, sq), src in pools.items():
                if ch == it.get("chapter") and sq == it.get("seq"):
                    key = (bk, ch, sq)
                    break
            if key is None:
                errs.append(f"{f.name}: {it.get('chapter')} seq{it.get('seq')} 找不到对应池条目")
                continue
            bk, ch, sq = key
            pool = pools[key]
            if key in seen:
                errs.append(f"{ch} seq{sq} 在多个批次重复出现")
                continue
            seen.add(key)

            summary = pool.get("summary") or ""
            expect_now = pool.get("new_id") if "new_id" in pool else pool.get("current_id")
            verdict = it.get("verdict")
            target = (pool.get("new_id") or pool.get("current_id")) if verdict == "confirm" \
                else (it.get("new_id") or "")
            if verdict not in ("confirm", "reclassify", "empty"):
                errs.append(f"{ch} seq{sq}: verdict 非法 {verdict!r}")
                continue
            if target and target not in legal:
                errs.append(f"{ch} seq{sq}: 目标 ID {target!r} 不在词表（active+D08）内")
                continue
            ev = it.get("evidence") or ""
            if verdict == "reclassify" and not ev.strip():
                errs.append(f"{ch} seq{sq}: 改判必须给 summary 原文依据，evidence 为空")
                continue
            ok_ev, bad_frag = evidence_grounded(ev, summary)
            if not ok_ev:
                errs.append(f"{ch} seq{sq}: evidence 落地失败，片段 {bad_frag[:30]!r} "
                            "不在该原子 summary 里（无据改判）")
                continue
            if not (it.get("reason") or "").strip():
                errs.append(f"{ch} seq{sq}: reason 为空，台账无法自证")
                continue
            cs, ce = chapter_pair(ch)
            hit = [a for a in atoms_of(bk)
                   if (a.get("chapter_start"), a.get("chapter_end"), a.get("seq")) == (cs, ce, sq)]
            if len(hit) != 1:
                errs.append(f"{ch} seq{sq} @{bk}: 定位命中 {len(hit)} 条")
                continue
            live = hit[0].get("atomic_id") or ""
            target_for_check = ((pool.get("new_id") or pool.get("current_id"))
                                if verdict == "confirm" else (it.get("new_id") or ""))
            # PM 的退空命令会把这些原子落成空，故空值也是合法态（本条稍后会被 superseded 剔除）
            allowed = {expect_now, target_for_check}
            if (bk, ch, sq) in retired_keys:
                allowed.add("")
            if live not in allowed:
                # 既不是池基线（未改）也不是目标值（已改过）→ 才是真的并发改动
                errs.append(f"{ch} seq{sq} @{bk}: 文件现值 {live!r} 既非池基线 {expect_now!r} "
                            "也非目标值 "
                            f"{target_for_check!r}（疑似并发改动，拒写）")
                continue
            if live == target_for_check and verdict != "confirm":
                d_state = "already-applied"      # 二次跑：已生效，幂等放行
            else:
                d_state = "to-write"

            out.append({
                "book": bk, "chapter": ch, "seq": sq, "cs": cs, "ce": ce,
                "verdict": verdict, "from_id": expect_now, "to_id": target,
                "confidence": it.get("confidence"), "main_thread": it.get("main_thread"),
                "reason": it.get("reason"), "evidence": ev,
                "lexicon_gap": it.get("lexicon_gap") or "",
                "category": pool.get("category") or pool.get("current_id"),
                "tier": ("A-" + ("low" if pool.get("ambiguous") else
                                 ("mid" if pool.get("how") == "chapter-overlap" else "high"))
                         if "new_id" in pool else "B-F03/F07"),
                "src_batch": f.name, "state": d_state,
            })
    return out, errs


def retire_directives() -> tuple[list[dict], list[str]]:
    """池 C：上轮子代理自报低置信的 9 条改判 → 本单按任务单退空，卡点抄进增类需求清单。"""
    rc = load(S05 / "回填复核台账.json")["records"]
    nine = [r for r in rc if r.get("confidence") == "低" and r["verdict"] != "confirm"]
    legal = legal_ids()
    out, errs = [], []
    for r in nine:
        cs, ce = chapter_pair(r["chapter"])
        hit = [a for a in atoms_of("win_寒门枭士.json")
               if (a.get("chapter_start"), a.get("chapter_end"), a.get("seq"))
               == (cs, ce, r["seq"])]
        if len(hit) != 1:
            errs.append(f"退空 {r['chapter']} seq{r['seq']}: 定位命中 {len(hit)}")
            continue
        live = hit[0].get("atomic_id") or ""
        if live not in (r.get("target_id"), ""):   # 已退空的算已生效，别当并发改动
            # 该条已被本单池复核改走 → 命令退空让位给复核结论（不算校验错误）
            if live == "":
                continue
            SKIPPED_BY_RECHECK.append(
                {"chapter": r["chapter"], "seq": r["seq"], "上轮改判": r.get("target_id"),
                 "本单现值": live,
                 "说明": "该条已被本单池 A 复核接管，退空让位；卡点仍抄进增类需求清单"})
            continue
        out.append({"book": "win_寒门枭士.json", "chapter": r["chapter"], "seq": r["seq"],
                    "cs": cs, "ce": ce, "verdict": "empty",
                    # from_id 取上轮改判值（历史真值）；现值可能已被本单退成空，不能当原值
                    "from_id": r.get("target_id"), "to_id": "",
                    "live_before_retire": live,
                    "confidence": "低", "main_thread": r.get("main_thread") or "",
                    "reason": f"PM 裁决：上轮改判本身系子代理低置信勉强取之，本单退空。卡点：{r.get('reason')}",
                    "evidence": "", "lexicon_gap": r.get("reason") or "",
                    "category": r.get("current_id"), "tier": "C-上轮低置信退空",
                    "src_batch": "outputs/sk05/回填复核台账.json"})
    return out, errs


# ---------------------------------------------------------------------------
# 二、plan / apply / verify
# ---------------------------------------------------------------------------
def collect() -> tuple[list[dict], list[str]]:
    SKIPPED_BY_RECHECK.clear()
    CONFLICTS.clear()
    c, e2 = retire_directives()
    # SK05c：PM 已采纳复核异议的条目撤销退空——从命令集合移出、池复核指令保留落盘；
    # 其现值是上一轮自己退成的空，故空值同样算合法态（E19 同一教训）。
    adopted = [x for x in c if (x["book"], x["chapter"], x["seq"]) in PM_ADOPTED]
    c = [x for x in c if (x["book"], x["chapter"], x["seq"]) not in PM_ADOPTED]
    retired_keys = (frozenset((x["book"], x["chapter"], x["seq"]) for x in c)
                    | frozenset(PM_ADOPTED))
    a, e1 = build_directives(retired_keys)
    # 冲突处置：默认 PM 命令「9 条低置信退空」优先——即便本单池 B 按新定义判成留 F07，
    # 也照命令退空，并把该异议原样记进 CONFLICTS 交 PM 复核（不擅自替 PM 改判）。
    # SK05c：PM 对 PM_ADOPTED 里的条目已裁定采纳复核意见，保留池复核指令、不执行退空。
    ck = {(x["book"], x["chapter"], x["seq"]): x for x in a}
    for x in adopted:
        prev = ck.get((x["book"], x["chapter"], x["seq"]))
        if prev is None:
            continue
        # 卡点文本取「池复核 + 上轮退空记录」两源并集：池 B 的自述较简，
        # 单独用会丢掉上轮长文本里的真实缺口信号（如「农桑推广」，QA-SK05B S1）
        gaps_txt = [t for t in (prev.get("lexicon_gap"), x.get("lexicon_gap")) if (t or "").strip()]
        if len(gaps_txt) == 2 and gaps_txt[0] != gaps_txt[1]:
            prev["lexicon_gap"] = gaps_txt[0] + "；" + gaps_txt[1]
        elif gaps_txt:
            prev["lexicon_gap"] = gaps_txt[0]
        CONFLICTS.append({
            "chapter": x["chapter"], "seq": x["seq"], "上轮低置信改判": x["from_id"],
            "本单池复核判定": f"{prev['verdict']}→{prev['to_id'] or '∅'}",
            "复核理由": prev.get("reason"), "复核置信": prev.get("confidence"),
            "实质分歧": bool(prev["to_id"]), "已采纳": True,
            "处置": "PM 裁定采纳本单复核异议（SK05c）：撤销退空命令，照池复核结论落盘（留 F07，中置信）",
        })
    for x in c:
        k = (x["book"], x["chapter"], x["seq"])
        prev = ck.get(k)
        if prev is None:
            continue
        CONFLICTS.append({
            "chapter": x["chapter"], "seq": x["seq"], "上轮低置信改判": x["from_id"],
            "本单池复核判定": f"{prev['verdict']}→{prev['to_id'] or '∅'}",
            "复核理由": prev.get("reason"), "复核置信": prev.get("confidence"),
            # 复核若也判空，与命令殊途同归；只有判成非空才算实质相左
            "实质分歧": bool(prev["to_id"]),
            "处置": "按 PM 命令退空；异议留档，PM 若采纳复核意见可一行改回",
        })
        a.remove(prev)
    return a + c, e1 + e2


def plan(show: bool = True) -> dict:
    dirs, errs = collect()
    stat = {
        "指令条数": len(dirs),
        "verdict": dict(Counter(d["verdict"] for d in dirs)),
        "按池": dict(Counter(d["tier"] for d in dirs)),
        "置信": dict(Counter(str(d.get("confidence")) for d in dirs)),
        "需改写": sum(1 for d in dirs if d["from_id"] != d["to_id"]),
        "跨文件": dict(Counter(d["book"] for d in dirs)),
        "退空与复核相左": len(CONFLICTS),
        "其中已采纳复核异议（SK05c）": sum(1 for cf in CONFLICTS if cf.get("已采纳")),
        "校验错误": errs,
    }
    if show:
        print("== SK05B 复核落盘计划 ==")
        for k, v in stat.items():
            print(f"  {k}: {v}")
        moved = Counter(f"{d['from_id']}→{d['to_id'] or '∅'}" for d in dirs if d["verdict"] != "confirm")
        print("\n  改判去向 TOP15：")
        for k, n in moved.most_common(15):
            print(f"    {n:3d}  {k}")
        gaps = Counter(d["lexicon_gap"] for d in dirs if d["lexicon_gap"])
        print(f"\n  词表缺口线索 {len(gaps)} 种：")
        for g, n in gaps.most_common(12):
            print(f"    ×{n}  {g[:70]}")
    return {"stat": stat, "directives": dirs}


def ensure_backup(book: str) -> None:
    src, bak = RAW / book, RAW / (book + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(src, bak)
        print(f"   [backup] {book} → {book}{BAK_SUFFIX}（改前 sha {sha(src)}）")


# —— 增类需求清单的候选抽取（口径见下，历史三版教训合一；QA-SK05B P1 修复）——
# ① 短语要挂在「缺/无/不含/不覆盖」之后才是增类信号，不能见引号就收（第一版踩过）；
# ② 引号必须**同风格配对**——SK05B 初版开闭字符集不对称（开 ASCII ' 闭中文 ’），
#    纯 ASCII '…' 与中文 ‘…’ 两族静默失配、63 条真缺口漏表（QA-SK05B P1 / 坑 E20）；
# ③ 剔除两类假候选：「另属现有类/关键词误伤」说明句中的反例短语（如 c2106 的
#    「歌舞团与报刊再掀宣传」）、与 77 行 definition 撞串或含省略号的定义回引。
QS = re.compile(r"『([^』]{2,30})』|「([^」]{2,30})」|[\"“]([^\"”]{2,30})[\"”]|['‘]([^'’]{2,30})['’]")
ANCHOR = re.compile(r"(?:缺|词表无|亦无|不含|不覆盖|无)[^。；\n]{0,6}$")
BAD_REF = re.compile(r"另属|误伤|误配|错配|带偏|带出|错挂")
UNQUOTED = re.compile(r"词表(?:亦|也)?无([^，。；、\s]{2,12}?)类|([^，。；：\s]{2,12}?)无对应类")


_EDGE = "，,。;；：:、 '\"\u2018\u2019\u201c\u201d「」『』"


def strip_phrase(t: str) -> str:
    # 去两端空白/标点/引号——嵌套引号（如「'以技换位'」）会连内层引号一起带出，须剥净
    return t.strip(_EDGE)


def extract_candidates(gaps: list[dict]) -> tuple[dict[str, list[str]], dict]:
    """卡点全文 → 候选类名聚类（返回 命中表 与 统计）。"""
    import sqlite3
    con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    lex_defs = [r[0] or "" for r in con.execute("SELECT definition FROM atomic_events")]
    con.close()
    hits: dict[str, list[str]] = {}
    stat = {"剔除回引": 0, "剔除反例": 0, "剔除节拍描述": 0, "无引号补抽": 0, "带候选卡点": 0}
    for d in gaps:
        txt = str(d["lexicon_gap"])
        src = f"{d['chapter']} seq{d['seq']}({d.get('from_id') or d.get('current_id')})"
        seen: set[str] = set()
        for m in QS.finditer(txt):
            phrase = strip_phrase(next(x for x in m.groups() if x is not None))
            if not (2 <= len(phrase) <= 30):
                continue
            if not ANCHOR.search(txt[:m.start()]):
                continue
            if "…" in phrase or any(phrase in x for x in lex_defs):
                stat["剔除回引"] += 1
                continue
            if BAD_REF.search(txt[m.end():m.end() + 24]):
                stat["剔除反例"] += 1
                continue
            seen.add(phrase)
        for m in UNQUOTED.finditer(txt):       # 明说缺口但没打引号（如「词表亦无农桑推广类」）
            raw = m.group(1) or m.group(2) or ""
            if re.match(r"^(?:起承转合|起|承|转|合)(?:拍|段)", raw):
                # 「X拍/段……亦无对应类可归」是节拍内容叙述，不是类名（QA-SK05B P1 四类修）
                stat["剔除节拍描述"] += 1
                continue
            phrase = strip_phrase(raw)
            phrase = re.sub(r"^(?:起承|起|承|转|合|的)+", "", phrase)
            if len(phrase) < 2:
                continue
            if any(phrase in x for x in lex_defs):
                stat["剔除回引"] += 1
                continue
            seen.add(phrase)
            stat["无引号补抽"] += 1
        for phrase in sorted(seen):
            hits.setdefault(phrase, []).append(src)
        if seen:
            stat["带候选卡点"] += 1
    return hits, stat


def apply_all() -> int:
    p = plan(show=True)
    dirs, errs = collect()
    if errs:
        print(f"\n[apply] 有 {len(errs)} 条校验不过，未写任何文件")
        return 1
    by_book: dict[str, list[dict]] = {}
    for d in dirs:
        by_book.setdefault(d["book"], []).append(d)
    stamped = {}
    for book, ds in by_book.items():
        ensure_backup(book)
        data = load(RAW / book)
        n = 0
        for d in ds:
            for a in data["atoms"]:
                if (a.get("chapter_start"), a.get("chapter_end"), a.get("seq")) == (d["cs"], d["ce"], d["seq"]):
                    if a.get("atomic_id") != d["to_id"]:
                        a["atomic_id"] = d["to_id"]
                        n += 1
                    tags = [t for t in (a.get("tags") or []) if t != NOFIT_TAG]
                    if d["verdict"] == "empty":
                        tags.append(NOFIT_TAG)
                    a["tags"] = tags
                    break
        dump(RAW / book, data)
        stamped[book] = {"changed_now": n, "planned": len(ds),
                         "content_sha": content_sha(sorted((d["chapter"], d["seq"], d["to_id"])
                                                           for d in ds)),
                         "file_sha": sha(RAW / book)}
        print(f"   [{book}] 计划 {len(ds)} 条 / 本次改写 {n} 处 → 内容哈希 {stamped[book]['content_sha']}")
    dump(S05B / "全量复核台账.json",
         {"stamp": stamped,
          "口径说明": "directives 全部取自计划与上轮台账，不取落盘现值，故重跑逐字节一致",
          "directives": dirs,
          "PM命令与复核异议": CONFLICTS, "退空让位": SKIPPED_BY_RECHECK})

    md = io.StringIO()
    md.write("# [DEV-SK05B] 回填全量语义复核台账\n\n")
    md.write("> 方法与 SK05 的 57 条轮完全一致：逐条读**四拍 summary 原文**对照 77 行词表 definition 重判，"
             "引原文依据；子代理只读、执行方脚本统一落盘。\n")
    st = p["stat"]
    md.write(f"> 指令 {st['指令条数']} 条（池构成 {st['按池']}），"
             f"verdict 分布 {st['verdict']}；需改写 {st['需改写']} 处。\n\n")
    md.write("| # | 书 | 章节 | seq | 档 | 原值 | 新值 | 判定 | 置信 | 主轴 | 依据（引 definition） | evidence |\n")
    md.write("|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for i, d in enumerate(sorted(dirs, key=lambda x: (x["book"], x["chapter"], x["seq"])), 1):
        md.write(f"| {i} | {d['book'].replace('win_', '').replace('.json', '')} | {d['chapter']} | {d['seq']} "
                 f"| {d['tier']} | {d['from_id']} | {d['to_id'] or '（空）'} | {d['verdict']} "
                 f"| {d.get('confidence')} | {str(d.get('main_thread'))[:26]} "
                 f"| {str(d.get('reason'))[:70]} | {str(d.get('evidence'))[:24]} |\n")
    if CONFLICTS:
        n_real = sum(1 for cf in CONFLICTS if cf.get("实质分歧"))
        n_adopted = sum(1 for cf in CONFLICTS if cf.get("已采纳"))
        md.write("\n## ⚠️ PM 命令与本单复核意见相左（处置逐行见下；SK05c 裁定已回填）\n\n")
        md.write(f"共 {len(CONFLICTS)} 条与退空命令重叠，其中**实质相左 {n_real} 条**"
                 f"（其余复核也判空，与命令殊途同归）；**PM 已采纳复核意见 {n_adopted} 条**"
                 "（SK05c 撤销退空、按池复核结论落盘）。\n\n")
        md.write("| 章节 | seq | 上轮低置信改判 | 本单复核判定 | 实质分歧 | 复核理由（引 definition） | 处置 |\n")
        md.write("|---|---|---|---|---|---|---|\n")
        for cf in CONFLICTS:
            md.write(f"| {cf['chapter']} | {cf['seq']} | {cf['上轮低置信改判']} "
                     f"| {cf['本单池复核判定']}（{cf['复核置信']}） "
                     f"| {'**是**' if cf.get('实质分歧') else '否'} | {str(cf['复核理由'])[:70]} "
                     f"| {cf['处置']} |\n")
    (S05B / "全量复核台账.md").write_text(md.getvalue(), encoding="utf-8")
    print(f"台账 → {S05B / '全量复核台账.md'}")

    gaps = [d for d in dirs if d["lexicon_gap"]]
    for sk in SKIPPED_BY_RECHECK:      # 让位条目的卡点同样登记
        gaps.append({"lexicon_gap": f"（退空让位）{sk['说明']}", "chapter": sk["chapter"],
                     "seq": sk["seq"], "from_id": sk["上轮改判"]})
    g = io.StringIO()
    n_c = st["按池"].get("C-上轮低置信退空", 0)
    g.write(f"# [DEV-SK05B] 增类需求清单（复核中发现的词表缺口 + {n_c} 条退空卡点）\n\n")
    g.write("> 用途：与 SK02c「低置信弧增类商讨」同构，作为下一轮词表扩容的**真实需求信号**。"
            "每条给出处（章节/seq）与卡点原文，便于复核时回看素材。\n\n")
    g.write("> **F07 措辞盲区（SK05c 移入，待下轮词表轮定）**：农技/育种试验算不算 F07「工艺」——"
            "`c1791~1792`（试验田测产＋杂交施肥新法首验）经 PM 裁定采纳池 B 异议（SK05c），"
            "**撤销退空、留 F07（中置信）**；该条不计入下方池 C 退空"
            "（相关候选短语「农桑推广」仍见 §一）。\n\n")
    g.write(f"共 {len(gaps)} 条带缺口线索（其中池C 退空 {n_c} 条）。\n\n")
    hits, cstat = extract_candidates(gaps)
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    g.write("## 一、缺失类名候选（按提及次数排序，供增类商讨直接取用）\n\n")
    g.write(f"共 {len(ranked)} 个候选短语（抽取时剔除：回引现有类定义/省略号 {cstat['剔除回引']} 处、"
            f"「另属/误伤」反例 {cstat['剔除反例']} 处；无引号补抽 {cstat['无引号补抽']} 处。"
            "口径与 QA-SK05B P1 修复见脚本 `extract_candidates` 注释）。\n\n")
    g.write("| 候选类名（卡点原文引号内） | 提及次数 | 出处举例 |\n|---|---|---|\n")
    for t, src in ranked:
        uniq = sorted(set(src))
        g.write(f"| {t} | {len(src)} | {', '.join(uniq[:3])}"
                f"{' 等' if len(uniq) > 3 else ''} |\n")
    g.write(f"\n（另 {len(gaps) - cstat['带候选卡点']} 条卡点全文未打出候选短语、仅文字描述，见下表明细）\n\n")
    g.write("## 二、卡点明细（逐条·全文）\n\n")
    g.write("| 出处 | 原值 | 处置 | 卡点全文 |\n|---|---|---|---|\n")
    for d in gaps:
        txt = str(d["lexicon_gap"]).replace("|", "\\|")
        g.write(f"| {d['chapter']} seq{d['seq']} | {d.get('from_id') or d.get('current_id')} "
                f"| {d.get('verdict', '退空让位')} | {txt} |\n")
    (S05B / "增类需求清单.md").write_text(g.getvalue(), encoding="utf-8")
    print(f"增类需求清单 → {S05B / '增类需求清单.md'}"
          f"（候选 {len(ranked)} 个；剔除回引 {cstat['剔除回引']} / 反例 {cstat['剔除反例']}"
          f" / 补抽 {cstat['无引号补抽']}）")
    return 0


def verify() -> int:
    legal = legal_ids()
    res = []

    def chk(name, ok, detail=""):
        res.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail else ''}")

    dirs, errs = collect()
    bf = [r for r in load(S05 / "回填台账.json")["rows"] if r["action"] == "backfill"]
    s05_rc = {(r["chapter"], r["seq"]): r for r in load(S05 / "回填复核台账.json")["records"]}
    covered = {(d["chapter"], d["seq"]) for d in dirs if d["book"] == "win_寒门枭士.json"}
    hit239 = sum(1 for r in bf if (r["chapter"], r["seq"]) in covered or (r["chapter"], r["seq"]) in s05_rc)
    chk("复核覆盖率 239/239（本单 182 + 上轮 57）", hit239 == len(bf) == 239 and not errs,
        f"覆盖 {hit239}/{len(bf)}，校验错误 {len(errs)}")
    chk("无校验错误（evidence 均出自 summary、ID 合法、定位唯一）", not errs, errs[:6])
    scope = sorted({d["book"] for d in dirs} | {"win_寒门枭士.json", "win_斗破苍穹.p2.json",
                                                 "win_凡人修仙传.p2.json", "win_凡人修仙传.json",
                                                 "win_斗破苍穹.json"})
    allbad, legacy, empty_total = [], [], 0
    for book in scope:
        p = RAW / book
        if not p.exists():
            continue
        atoms = load(p)["atoms"]
        # 改前基线（.bak-sk05b）里就有的非法 ID 属存量遗留，不算本单引入
        base_file = RAW / (book + BAK_SUFFIX)
        pre = (load(base_file)["atoms"] if base_file.exists()
               else load(base_file.with_name(base_file.name.replace(BAK_SUFFIX, "")))["atoms"])
        pre_illegal = Counter((a.get("atomic_id") or "") for a in pre)
        for a in atoms:
            aid = a.get("atomic_id") or ""
            if aid and aid not in legal:
                if pre_illegal.get(aid, 0) > 0 and book != "win_寒门枭士.json":
                    legacy.append((book, aid))
                    pre_illegal[aid] -= 1
                else:
                    allbad.append((book, aid, a.get("chapter_start"), a.get("seq")))
            empty_total += 1 if not aid else 0
    chk("本单未引入任何词表越界（76 active + D08 试用口径）", not allbad, allbad[:5])
    if legacy:
        print(f"  [存量遗留] 改前就在的非法 ID {len(legacy)} 处 {sorted(set((b, i) for b, i in legacy))}"
              f" —— 非本单产生（SK05 只扫 5 书未覆盖），交 PM 另单处理")
    else:
        print("  [存量遗留] 非法 ID 无（SK05c 已定点修毕：九星 G09→A09）")
    contra, legacy_contra = [], []
    touched_all = {(d["book"], d["cs"], d["ce"], d["seq"]) for d in dirs}
    for b in sorted({d["book"] for d in dirs}):
        base_file = RAW / (b + BAK_SUFFIX)
        pre = load(base_file)["atoms"] if base_file.exists() else []
        # 改前就矛盾的组合（空 ID 无标 / 有 ID 带标）按 (章,seq) 建集合，不算本单引入
        pre_bad = {(a.get("chapter_start"), a.get("chapter_end"), a.get("seq")) for a in pre
                   if ((a.get("atomic_id") or "").strip() and NOFIT_TAG in (a.get("tags") or []))
                   or (not (a.get("atomic_id") or "").strip()
                       and NOFIT_TAG not in (a.get("tags") or []))}
        touched = {(d["cs"], d["ce"], d["seq"]) for d in dirs if d["book"] == b}
        for a in atoms_of(b):
            k = (a.get("chapter_start"), a.get("chapter_end"), a.get("seq"))
            bad = ((a.get("atomic_id") or "").strip() and NOFIT_TAG in (a.get("tags") or [])) \
                or (not (a.get("atomic_id") or "").strip() and NOFIT_TAG not in (a.get("tags") or []))
            if not bad:
                continue
            if k in touched:
                contra.append((b, k))                      # 本单改过且仍矛盾 → 真缺陷
            elif k in pre_bad:
                legacy_contra.append((b, k))               # 改前就矛盾 → 存量遗留
            else:
                contra.append((b, k))                      # 本单改动间接造成 → 也按缺陷报
    chk(f"本单改动的 {len(touched_all)} 个原子无矛盾态（有 ID 不带头标、空 ID 必带标）",
        not contra, contra[:5])
    if legacy_contra:
        print(f"  [存量遗留] 改前即矛盾的原子 {len(legacy_contra)} 处 {legacy_contra[:6]}"
              f" —— 非本单产生，与 G09 同单交 PM")
    else:
        print("  [存量遗留] 矛盾态无（SK05c 已定点修毕：4 处除「无合适原子」标）")
    chk(f"十书空 ID 合计 = {empty_total}（较 SK05 终态 48 增 {empty_total - 48}）",
        empty_total > 48, empty_total)
    chk("台账逐条有 definition 引证（reason 非空）",
        all(str(d.get("reason") or "").strip() for d in dirs))
    chk("F03/F07 新定义在库", _def_ok())
    print(f"\nverify: {sum(res)}/{len(res)} PASS")
    return 0 if all(res) else 1


def _def_ok() -> bool:
    import sqlite3
    con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    ok = con.execute("SELECT definition FROM atomic_events WHERE id='F03'").fetchone()[0]
    ok2 = con.execute("SELECT definition FROM atomic_events WHERE id='F07'").fetchone()[0]
    con.close()
    return "已有器物" in ok and "新工艺" in ok2


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    S05B.mkdir(parents=True, exist_ok=True)
    mode = "plan" if a.plan else ("apply" if a.apply else "verify")
    sys.stdout = _Tee(S05B / f"sk05b_{mode}.txt")
    print(f"[DEV-SK05B] mode={mode}  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    rc = {"plan": lambda: 0 if plan()["stat"]["校验错误"] == [] else 1,
          "apply": apply_all, "verify": verify}[mode]()
    print(f"[DEV-SK05B] {mode} rc={rc}")
    sys.stdout.flush()
    sys.exit(rc)


if __name__ == "__main__":
    main()
