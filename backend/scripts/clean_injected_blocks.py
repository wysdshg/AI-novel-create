# -*- coding: utf-8 -*-
"""[DEV-DATA01b] 素材缺陷复核·二批：他书注入块清理 + 整章隔离 + 文件名控制字符 + 缺陷登记。

上游：[INTERN-F6B]/[INTERN-F6C] 切原子时自查上报（任务单 `outputs/task-DEV-DATA01B.md`）。
本单修**素材源文件层**；已入库概括只出报告不改库（见 `audit_summary_contamination.py`）。
不动 `outputs/_atomic_raw`（F6 产物基于隔离后素材，无需重切）。

    --survey      全库只读普查：新口径在 38 本里的命中分布（含三书之外的附带发现）
    --dry-run     三书逐文件动作计划 → outputs/data01b/dry-run报告.md + 决策明细.json（不写素材）
    --apply       按批准清单执行（必须 --force）：A 切尾（先整文件备份）/ B 整章隔离 / R 文件名重命名
    --verify      按文件系统现状核账：三书各模式剩余 0 + 台账备份隔离对账 + 幂等 + 缺陷登记重算

零 LLM 调用：确定性正则 + 已亲验的异质指纹表（`chapter_noise_patterns.py` DATA01b 段）。
纪律：不删任何文件（隔离制）；A 类改写前整文件备份；回滚 = 照 `清洗台账_二批.json` 逆向。

两级证据（半径控制）：
  ① 广告锚点行（爱阅app/星星app/穿越小说吧）→ 只删那几行，绝不切段（无块证据时尾巴是正文）
  ② 异质专名注入块：同块在本文件命中 ≥2 行才认（挡掉「当时宇文家」撞「时宇」这类子串）
     → 从「最早的广告行或块命中行」切到文末；切点早于 25% 且前面剩不下 200 汉字
       = 整章就是他书内容，走 B 隔离
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chapter_noise_patterns import (  # noqa: E402
    FAN_SEQUEL_DELTA_MIN, INJECT_CUT_HARD_FLOOR_CJK, INJECT_CUT_MAX_FRAC,
    INJECT_KEEP_MIN_BODY_CJK, INJECT_MIN_MARKER_LINES, INJECTED_BLOCKS, NAME_CONTROL_CHARS,
    NOTICE_HINT, SEQ_PREFIX, TAIL_PLEA, cjk_count, match_inject_ad_line,
)

ROOT = Path(__file__).resolve().parents[2]
NOVEL_ROOT = ROOT / "小说"
OUT_DIR = ROOT / "outputs" / "data01b"
BACKUP_DIR = OUT_DIR / "backup"
LEDGER = OUT_DIR / "清洗台账_二批.json"
DECISIONS = OUT_DIR / "决策明细.json"
REGISTRY = OUT_DIR / "缺陷登记.json"
QUARANTINE_DIRNAME = "_quarantine"
SKIP_DIRS = {"_残章", "_quarantine"}
SKIP_FILES = {"书籍信息.txt", "progress.json"}
SENT_END = "。！？…：;；”』」】\"'"
BATCH = "DATA01b"

# 本单处置范围（用户拍板：三书）。其它书的命中只在 --survey 里报告，不动文件。
SCOPE_BOOKS = ["寒门枭士", "斗破苍穹", "凡人修仙传"]

CN_NUM = r"[零一二三四五六七八九十百千两\d]{1,8}"
INNER_TITLE = re.compile(r"第\s*(" + CN_NUM + r")\s*章")
# 每个注入块一条合并正则（多词或运算），全库普查时省掉 N×M 次子串扫描
BLOCK_RX: dict[str, re.Pattern[str]] = {
    blk["name"]: re.compile("|".join(re.escape(m) for m in blk["markers"]))
    for blocks in INJECTED_BLOCKS.values() for blk in blocks
}


# ───────────────────────── 基础工具 ─────────────────────────
def iter_books(scope: list[str] | None = None) -> list[Path]:
    return sorted(p for p in NOVEL_ROOT.iterdir()
                  if p.is_dir() and p.name not in SKIP_DIRS
                  and (scope is None or p.name in scope))


def chapter_files(book: Path) -> list[Path]:
    return sorted(f for f in book.glob("*.txt") if f.name not in SKIP_FILES)


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def seq_of(name: str) -> int | None:
    m = SEQ_PREFIX.match(name)
    return int(m.group("seq")) if m else None


def cn2int(s: str) -> int | None:
    """中文数字 → int（只覆盖章号常见写法，解析不了返回 None）。"""
    if s.isdigit():
        return int(s)
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    total = section = num = 0
    for ch in s:
        if ch in digits:
            num = digits[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
        elif ch == "万":
            total = (total + section + num) * 10000
            section = num = 0
        elif ch == "零":
            continue
        else:
            return None
    return total + section + num


def inner_title_no(text: str) -> tuple[int | None, str]:
    """文内首个标题行的章号（全链路以文件名前缀为准，这里只用于对账登记）。"""
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = INNER_TITLE.search(s)
        if m:
            return cn2int(m.group(1)), s[:40]
        if len(s) > 60:
            break
    return None, ""


def rows_of(text: str) -> list[dict]:
    """一行一块（本库段落 = 一行一段），保留原始 EOL 以便原样重建。"""
    out = []
    for ln in text.splitlines(keepends=True):
        content = re.sub(r"\r\n|\r|\n$", "", ln)
        out.append({"content": content, "eol": ln[len(content):]})
    return out


def rebuild(rows: list[dict], original: str) -> str:
    keep = [r for r in rows if not r["cut"]]
    out = "".join(r["content"] + r["eol"] for r in keep)
    if len(keep) < len(rows):        # 尾部被动过：收掉多余空行，还原原结尾换行风格
        out = out.rstrip("\r\n")
        if original.endswith(("\n", "\r")):
            out += "\r\n" if "\r\n" in original else "\n"
    return out


# ───────────────────────── 注入块判定 ─────────────────────────
def registered_blocks() -> list[dict]:
    """全部已登记注入块（survey 用它对撞其它书）。"""
    return [b for blocks in INJECTED_BLOCKS.values() for b in blocks]


def find_injection(text: str, book: str) -> dict | None:
    """返回注入块判定（含切点）；没有注入返回 None。"""
    blocks = INJECTED_BLOCKS.get(book, [])
    rows = rows_of(text)
    nonempty = [i for i, r in enumerate(rows) if r["content"].strip()]
    if not nonempty:
        return None

    ad_idx: list[int] = []
    for i in nonempty:
        if match_inject_ad_line(rows[i]["content"]):
            ad_idx.append(i)

    block_idx: dict[str, list[int]] = {}
    for blk in blocks:
        rx = BLOCK_RX[blk["name"]]
        idxs = [i for i in nonempty if rx.search(rows[i]["content"])]
        if len(idxs) >= INJECT_MIN_MARKER_LINES:
            block_idx[blk["name"]] = idxs

    if not ad_idx and not block_idx:
        return None

    cands: list[tuple[int, str]] = [(i, "ad_anchor") for i in ad_idx]
    for name, idxs in block_idx.items():
        cands.append((idxs[0], name))
    cut, why = min(cands)

    tail_cjk = sum(cjk_count(rows[i]["content"]) for i in nonempty if i >= cut)
    body_cjk = sum(cjk_count(rows[i]["content"]) for i in nonempty if i < cut)
    pos = nonempty.index(cut) if cut in nonempty else 0
    frac = pos / max(len(nonempty) - 1, 1)
    return {
        "cut_idx": cut, "cut_reason": why, "cut_line": rows[cut]["content"].strip()[:40],
        "tail_cjk": tail_cjk, "body_cjk_before": body_cjk, "cut_frac": round(frac, 3),
        "n_ad_lines": len(ad_idx), "blocks": {k: len(v) for k, v in block_idx.items()},
        "n_nonempty": len(nonempty),
    }


def decide_injection(book: str, path: Path, text: str | None = None) -> dict:
    """单文件判定 → 动作 A（切尾/删广告行）/ B（整章隔离）/ none。

    证据分两级：**块证据**（异质专名同块 ≥2 行）够硬就切到文末；只有广告行时不切段，
    只把那几行广告删掉（正文尾巴再长也不是他书内容，砍了就是丢正文）。
    """
    rel = str(path.relative_to(ROOT))
    if text is None:
        text = read_text(path)
    d: dict = {"book": book, "file": path.name, "rel": rel, "size": path.stat().st_size,
               "action": "none", "rule": "", "reason": "", "patterns": []}
    inj = find_injection(text, book)
    if not inj:
        return d
    has_block = bool(inj["blocks"])

    if not has_block:                                   # 只有广告行
        d.update(action="A", rule="ad_line_only", cut_idx=None, patterns=["inject_ad_line"],
                 reason=f"仅站点广告行 {inj['n_ad_lines']} 行（无异质块证据，只删广告行不切段）",
                 tail_cjk=inj["tail_cjk"], body_cjk=inj["body_cjk_before"])
        return d

    if inj["tail_cjk"] < INJECT_CUT_HARD_FLOOR_CJK:
        d["note"] = f"命中注入块但尾巴仅 {inj['tail_cjk']} 汉字，低于 {INJECT_CUT_HARD_FLOOR_CJK}，本单不动"
        d["patterns"] = [inj["cut_reason"]]
        return d

    if inj["cut_frac"] < INJECT_CUT_MAX_FRAC and inj["body_cjk_before"] < INJECT_KEEP_MIN_BODY_CJK:
        d.update(action="B", rule="whole_chapter_foreign",
                 patterns=["injected_tail_block", inj["cut_reason"]],
                 reason=f"整章为他书内容（切点在第 {inj['cut_frac']:.0%} 处，"
                        f"之前正文仅 {inj['body_cjk_before']} 汉字）")
        d.update({k: inj[k] for k in ("cut_idx", "cut_line", "tail_cjk", "body_cjk_before",
                                      "cut_frac", "blocks")})
        return d
    d.update(action="A", rule="injected_tail_block",
             patterns=["injected_tail_block"] + (["inject_ad_line"] if inj["n_ad_lines"] else []),
             reason=f"章末他书注入块（判据 {inj['cut_reason']}，切点第 {inj['cut_idx']} 行 "
                    f"{inj['cut_line']}，尾部 {inj['tail_cjk']} 汉字，正文保留 {inj['body_cjk_before']} 汉字）")
    d.update({k: inj[k] for k in ("cut_idx", "cut_line", "tail_cjk", "body_cjk_before",
                                  "cut_frac", "blocks", "n_ad_lines")})
    return d


def clean_injection(text: str, cut_idx: int | None) -> tuple[str, list[str]]:
    """执行清理：广告行整行删；给了 cut_idx 则从该行切到文末。返回 (新文本, 命中模式)。"""
    hits: list[str] = []
    rows = rows_of(text)
    for r in rows:
        r["cut"] = False
        if not r["content"].strip():
            continue
        name = match_inject_ad_line(r["content"])
        if name:
            r["cut"] = True
            hits.append(name)
    if cut_idx is not None:
        for i, r in enumerate(rows):
            if i >= cut_idx:
                if not r["cut"]:
                    hits.append("injected_tail_block")
                r["cut"] = True
    return rebuild(rows, text), sorted(set(hits))


# ───────────────────────── 非正文（同人续写）判定 ─────────────────────────
def decide_nonbody(book: str, path: Path, text: str) -> dict | None:
    """斗破苍穹 c1658~c1671：整章非本书正文 → B 隔离。"""
    if book != "斗破苍穹":
        return None
    seq = seq_of(path.name)
    if seq is None or seq < 1600:
        return None
    no, title = inner_title_no(text)
    stem = path.stem.split("_", 1)[1] if "_" in path.stem else path.stem
    reasons = []
    if no is not None and seq - no > FAN_SEQUEL_DELTA_MIN:
        reasons.append(f"文内章号重新从「第{no}章」编号（与文件名前缀差 {no - seq}，"
                       f"正文章号偏移实测只 -22~-27）")
    if "关于离开" in stem:
        reasons.append("标题为同人文作者的话")
    if re.search(r"新书大主宰已发", stem):
        reasons.append("标题即「新书大主宰已发」完本感言")
    for pat in (r"本小说根据土豆的", r"第一次写小说", r"穿越小说吧"):
        if re.search(pat, text):
            reasons.append(f"正文自述/尾标命中「{pat}」")
            break
    if not reasons:
        return None
    return {"book": book, "file": path.name, "rel": str(path.relative_to(ROOT)),
            "size": path.stat().st_size, "action": "B", "rule": "fan_sequel_nonbody",
            "patterns": ["fan_sequel"], "reason": "整章非本书正文（" + "；".join(reasons) + "）",
            "inner_title": title}


# ───────────────────────── 文件名控制字符（R 类）─────────────────────────
def decide_rename(book: str, path: Path) -> dict | None:
    if not NAME_CONTROL_CHARS.search(path.name):
        return None
    new_name = re.sub(r"\s{2,}", " ", NAME_CONTROL_CHARS.sub("", path.name)).strip()
    if not new_name or new_name == path.name:
        return None
    return {"book": book, "file": path.name, "rel": str(path.relative_to(ROOT)),
            "new_file": new_name, "size": path.stat().st_size, "action": "R",
            "rule": "control_char_name", "patterns": ["control_char_in_name"],
            "reason": f"文件名含控制字符 {sorted({hex(ord(c)) for c in path.name if ord(c) < 0x20 or ord(c) == 0x7f})}"
                      f"，工具直读失败 → 去控制字符重命名（前缀章号不动）"}


# ───────────────────────── 缺陷登记（只读，不改文件）─────────────────────────
def last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def classify_truncation(text: str) -> tuple[str, str]:
    """末行判定。返回 (状态, 末行尾 24 字)。

    三档（后两档都不算「爬取截断」，只有 strong 才是任务单说的那一类）：
      truncated = 引号没闭合 / 末行以逗号顿号冒号括号收尾 / 末行只剩 1~5 个字的残块 → 句子被切断
      no_mark   = 末行是完整句但没打句末标点（斗破/凡人实测多为这一档，不是截断）
      author_talk / clean 照旧
    """
    last = last_line(text)
    if not last:
        return "clean", ""
    if last in {"全书完", "全文完", "正文完", "(全书完)", "（全书完）"}:
        return "clean", ""
    if TAIL_PLEA.search(last) or NOTICE_HINT.search(last):
        return "author_talk", last[-24:]
    if last[-1] in SENT_END or last.endswith("."):
        return "clean", ""
    # 铁证：末行自己有半个没合上的引号 / 以逗号冒号括号收尾 / 只剩一两个字的残块。
    # 不做全文件引号配平——凡人修仙传抓取把引号打烂（&“_nk” 之类），全文件配平会假报。
    open_quote = ("“" in last and "”" not in last) or ("「" in last and "」" not in last)
    if open_quote or last[-1] in "，、：；（(【[" or len(last) <= 5:
        return "truncated", last[-24:]
    return "no_mark", last[-24:]


def offset_distribution(book: str, texts: dict[Path, str]) -> Counter:
    dist: Counter[str] = Counter()
    for p, text in texts.items():
        seq = seq_of(p.name)
        if seq is None:
            continue
        no, _ = inner_title_no(text)
        if no is None:
            dist["文内无章号"] += 1
        else:
            d = no - seq
            dist[f"{d}" if abs(d) <= 30 else f"大偏移({d})"] += 1
    return dist


def duplicate_report(texts: dict[Path, str], min_len: int = 40) -> dict:
    """重复段/重复章清点（在**处置后**的文本上做，避免注入块把同文率灌满）。"""
    loaded = []
    for p, text in texts.items():
        seq = seq_of(p.name)
        if seq is None:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        loaded.append((seq, p, lines))
    loaded.sort(key=lambda x: x[0])
    intra: list[dict] = []
    same: list[dict] = []
    head_tail: list[dict] = []
    for i, (ch, p, lines) in enumerate(loaded):
        longs = [ln for ln in lines if len(ln) >= min_len]
        dup = {k: v for k, v in Counter(longs).items() if v >= 2}
        if dup:
            intra.append({"chapter": ch, "file": p.name, "n": len(dup),
                          "sample": next(iter(dup))[:40]})
        if i + 1 < len(loaded):
            nch, np_, nlines = loaded[i + 1]
            A = {ln for ln in lines if len(ln) >= min_len}
            B = {ln for ln in nlines if len(ln) >= min_len}
            if A and B:
                ov = len(A & B) / max(1, min(len(A), len(B)))
                if ov >= 0.6:
                    same.append({"prev": ch, "next": nch, "overlap": round(ov, 3),
                                 "prev_file": p.name, "next_file": np_.name})
            common = [ln for ln in lines[-12:] if len(ln) >= min_len and ln in nlines[:12]]
            if common:
                head_tail.append({"prev": ch, "next": nch, "n": len(common),
                                  "sample": common[0][:40]})
    return {"文件内重复段落": intra, "相邻章整章同文": same, "相邻章首尾重复粘贴": head_tail}


# ───────────────────────── 计划编制 ─────────────────────────
def load_texts(book_dir: Path) -> dict[Path, str]:
    return {p: read_text(p) for p in chapter_files(book_dir)}


def build_plan(scope: list[str]) -> tuple[list[dict], list[dict], dict]:
    """返回 (动作计划, 登记缺陷, 概览)。每本书的文本只读一次。"""
    plan: list[dict] = []
    reg: dict = {}
    overview: dict = {"scope_books": scope, "per_book": {}}
    for book in scope:
        bdir = NOVEL_ROOT / book
        if not bdir.is_dir():
            overview.setdefault("missing_books", []).append(book)
            continue
        texts = load_texts(bdir)
        acts: list[dict] = []
        renames: list[dict] = []
        nonbody: list[dict] = []
        for p, text in texts.items():
            rn = decide_rename(book, p)
            if rn:
                renames.append(rn)
            nb = decide_nonbody(book, p, text)
            if nb:
                nonbody.append(nb)
                continue
            d = decide_injection(book, p, text)
            if d["action"] != "none":
                acts.append(d)
        plan.extend(acts)
        plan.extend(nonbody)
        plan.extend(renames)

        # 登记类（不改文件）：句中截断 / 章号偏移 / 重复段 —— 截断与重复按「处置后」文本算
        post: dict[Path, str] = {}
        decided = {d["file"]: d for d in acts}
        for p, text in texts.items():
            d = decided.get(p.name)
            if d and d["action"] == "A":
                text, _ = clean_injection(text, d.get("cut_idx"))
            post[p] = text
        trunc, talk, nomark = [], [], []
        for p, text in post.items():
            if seq_of(p.name) is None:
                continue
            st, ev = classify_truncation(text)
            rec = {"chapter": seq_of(p.name), "file": p.name, "last_chars": ev}
            if st == "truncated":
                trunc.append(rec)
            elif st == "no_mark":
                nomark.append(rec)
            elif st == "author_talk":
                talk.append(rec)
        reg[book] = {
            "文件数": len(texts),
            "句中截断_章数": len(trunc),
            "句中截断_章号": [t["chapter"] for t in trunc],
            "句中截断_样例": trunc[:6],
            "无句末标点_章数": len(nomark),
            "无句末标点_章号": [t["chapter"] for t in nomark],
            "作者话尾巴残留_章数": len(talk),
            "作者话尾巴残留_章号": [t["chapter"] for t in talk],
            "章号偏移分布": dict(offset_distribution(book, texts).most_common(20)),
            "重复清点": duplicate_report(post),
        }
        overview["per_book"][book] = {
            "文件数": len(texts), "A_切尾": sum(1 for x in acts if x["action"] == "A"),
            "B_整章隔离": sum(1 for x in acts if x["action"] == "B") + len(nonbody),
            "R_重命名": len(renames), "句中截断": len(trunc),
        }
    return plan, reg, overview


def cmd_dry_run(args: argparse.Namespace) -> int:
    scope = args.books.split(",") if args.books else SCOPE_BOOKS
    plan, reg, overview = build_plan(scope)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DECISIONS.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    write_dry_run_report(plan, reg, overview, OUT_DIR / "dry-run报告.md")
    c = Counter(d["action"] for d in plan)
    print(f"[dry-run] 范围 {scope}")
    for b, v in overview["per_book"].items():
        print(f"  {b}: {v}")
    print(f"[dry-run] 合计 A {c.get('A', 0)}｜B {c.get('B', 0)}｜R {c.get('R', 0)}"
          f"｜登记不改文件（截断/偏移/重复见报告）")
    print(f"[dry-run] 明细 -> {DECISIONS}\n[dry-run] 登记 -> {REGISTRY}")
    print("[dry-run] 未写任何素材文件。apply 需 --apply --force 且经批准")
    return 0


# ───────────────────────── 报告 ─────────────────────────
def write_dry_run_report(plan: list[dict], reg: dict, overview: dict, out: Path) -> None:
    L: list[str] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    L.append("# [DEV-DATA01b] 素材缺陷复核·二批 dry-run 报告\n")
    L.append(f"- 生成（UTC）：{ts}｜零 LLM 调用，全部确定性判据")
    L.append(f"- 范围（本单处置）：{', '.join(overview['scope_books'])}")
    L.append("- 模式与阈值：`backend/scripts/chapter_noise_patterns.py` DATA01b 段")
    L.append("- 动作：A 章内切尾（改写前整文件备份）｜B 整章隔离（move 到 `小说/_quarantine/<书名>/`，不删）｜"
             "R 文件名去控制字符重命名｜登记 = 不改文件只落 `缺陷登记.json`\n")
    L.append("## 一、动作总账\n")
    L.append("| 书 | 文件数 | A 切尾 | B 隔离 | R 重命名 | 句中截断(登记) | 作者话尾巴(登记) |")
    L.append("|---|---|---|---|---|---|---|")
    for b, v in overview["per_book"].items():
        r = reg[b]
        L.append(f"| {b} | {v['文件数']} | {v['A_切尾']} | {v['B_整章隔离']} | {v['R_重命名']} "
                 f"| {r['句中截断_章数']} | {r['作者话尾巴残留_章数']} |")
    L.append("")
    L.append("## 二、逐文件动作（A/B/R 全量，理由与切点）\n")
    L.append("| 动作 | 判据 | 书 | 文件 | 说明 |")
    L.append("|---|---|---|---|---|")
    for d in plan:
        L.append(f"| {d['action']} | `{d['rule']}` | {d['book']} | `{d['file']}` | {d['reason']} |")
    L.append("")
    for b in reg:
        r = reg[b]
        L.append(f"## 三-{list(reg).index(b) + 1}、{b} 缺陷登记（不改文件）\n")
        L.append(f"- **句中截断 {r['句中截断_章数']} 章**（强判据：引号未闭合/以逗号冒号收尾/残块），"
                 "章号全量见 `缺陷登记.json`；样例（末行尾 ≤24 字）")
        for t in r["句中截断_样例"]:
            L.append(f"  - c{t['chapter']} `{t['file']}` ← …{t['last_chars']}")
        L.append(f"- **仅缺句末标点（不算截断）{r['无句末标点_章数']} 章**：末行是完整句但没打句号，"
                 "斗破/凡人这类占多数，本单不动")
        L.append(f"- **作者话尾巴残留 {r['作者话尾巴残留_章数']} 章**（末行是更新说明类，"
                 f"DATA01 TIER2 口径默认不删）：{r['作者话尾巴残留_章号'][:20]}")
        L.append(f"- **章号偏移分布**（文内标题章号 − 文件名前缀）：{r['章号偏移分布']}")
        dup = r["重复清点"]
        L.append(f"- **文件内重复段落** {len(dup['文件内重复段落'])} 个文件；"
                 f"**相邻章整章同文** {len(dup['相邻章整章同文'])} 对；"
                 f"**相邻章首尾重复粘贴** {len(dup['相邻章首尾重复粘贴'])} 对")
        for x in dup["相邻章整章同文"][:10]:
            L.append(f"  - 整章同文 c{x['prev']}↔c{x['next']}（overlap {x['overlap']}）")
        for x in dup["相邻章首尾重复粘贴"][:10]:
            L.append(f"  - 首尾重复 c{x['prev']}→c{x['next']} {x['n']} 段：{x['sample']}")
        for x in dup["文件内重复段落"][:10]:
            L.append(f"  - 文内重复 c{x['chapter']} {x['n']} 段：{x['sample']}")
        L.append("")
    out.write_text("\n".join(L), encoding="utf-8")


# ───────────────────────── 台账 ─────────────────────────
def load_ledger() -> list[dict]:
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []


def append_ledger(entries: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    allv = load_ledger()
    seen = {(e["rel"], e["action"]) for e in allv}
    allv.extend(e for e in entries if (e["rel"], e["action"]) not in seen)
    LEDGER.write_text(json.dumps(allv, ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────── apply ─────────────────────────
def apply_one(d: dict, ts: str) -> dict | None:
    src = ROOT / d["rel"]
    if not src.exists():
        return {"error": f"文件不存在（可能已处置）：{d['rel']}"}
    if d["action"] == "A":
        text = read_text(src)
        new_text, hits = clean_injection(text, d.get("cut_idx"))
        if hits == []:
            return None                      # 幂等：已经干净
        bak = BACKUP_DIR / d["book"] / src.name
        bak.parent.mkdir(parents=True, exist_ok=True)
        if not bak.exists():
            shutil.copy2(src, bak)
        tmp = src.parent / (src.name + ".tmp")
        tmp.write_bytes(new_text.encode("utf-8"))
        tmp.replace(src)
        return {"rel": d["rel"], "action": "A", "batch": BATCH, "rule": d["rule"],
                "backup": str(bak.relative_to(ROOT)), "patterns": hits,
                "cut_idx": d.get("cut_idx"), "removed_cjk": d.get("tail_cjk", 0),
                "before_bytes": len(text.encode("utf-8")),
                "after_bytes": len(new_text.encode("utf-8")), "ts_utc": ts}
    if d["action"] == "B":
        dst_dir = NOVEL_ROOT / QUARANTINE_DIRNAME / d["book"]
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        if dst.exists():
            return {"error": f"隔离目标已存在，跳过：{dst.relative_to(ROOT)}"}
        size = src.stat().st_size
        shutil.move(str(src), str(dst))
        return {"rel": d["rel"], "action": "B", "batch": BATCH, "rule": d["rule"],
                "new_rel": str(dst.relative_to(ROOT)), "patterns": d["patterns"],
                "before_bytes": size, "ts_utc": ts}
    if d["action"] == "R":
        dst = src.parent / d["new_file"]
        if dst.exists():
            return {"error": f"重命名目标已存在，跳过：{d['rel']} -> {d['new_file']}"}
        src.rename(dst)
        return {"rel": d["rel"], "action": "R", "batch": BATCH, "rule": d["rule"],
                "new_rel": str(dst.relative_to(ROOT)), "new_file": d["new_file"],
                "patterns": d["patterns"], "before_bytes": dst.stat().st_size, "ts_utc": ts}
    return {"error": f"未知动作 {d['action']}"}


def apply_plan(plan: list[dict]) -> tuple[list[dict], list[str]]:
    """逐条落地并写台账。返回 (台账条目, 错误)。幂等：已处置的条目自然无动作。"""
    entries: list[dict] = []
    errors: list[str] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in plan:
        r = apply_one(d, ts)
        if r is None:
            continue
        if "error" in r:
            errors.append(r["error"])
        else:
            entries.append(r)
    append_ledger(entries)
    return entries, errors


def cmd_apply(args: argparse.Namespace) -> int:
    dec = Path(args.decisions)
    if not dec.exists():
        print(f"🔴 缺批准清单：{dec}（先 --dry-run 并经用户/PM 批准）")
        return 2
    approved = json.loads(dec.read_text(encoding="utf-8"))
    if not args.force:
        print("🔴 未加 --force，停止（dry 保护）")
        return 2
    scope = args.books.split(",") if args.books else SCOPE_BOOKS
    plan, _reg, _ov = build_plan(scope)          # 按文件系统现状重算，不盲信旧清单
    keys = {(d["rel"], d["action"]) for d in approved}
    todo = [d for d in plan if (d["rel"], d["action"]) in keys]
    extra = [d["rel"] for d in plan if (d["rel"], d["action"]) not in keys]
    print(f"批准清单 {len(keys)} 项｜现状仍可执行 {len(todo)} 项｜"
          f"已处置/漂移 {len(keys) - len(todo)} 项｜现状新增未批准 {len(extra)} 项")
    for rel in extra[:10]:
        print("  ⚠️ 现状有动作但不在批准清单，本单不做：", rel)
    entries, errors = apply_plan(todo)
    c = Counter(e["action"] for e in entries)
    print(f"APPLY：A {c.get('A', 0)}｜B {c.get('B', 0)}｜R {c.get('R', 0)}｜失败 {len(errors)}｜"
          f"台账累计 {len(load_ledger())} 条")
    for e in errors[:20]:
        print("  🔴", e)
    return 1 if errors else 0


# ───────────────────────── verify ─────────────────────────
def rescan_books(scope: list[str]) -> dict:
    """按文件系统现状重扫：新口径剩余命中数（应全为 0）。"""
    left: Counter[str] = Counter()
    detail: dict[str, list[str]] = defaultdict(list)
    for book in scope:
        bdir = NOVEL_ROOT / book
        if not bdir.is_dir():
            continue
        for p in chapter_files(bdir):
            text = read_text(p)
            for ln in text.splitlines():
                name = match_inject_ad_line(ln)
                if name:
                    left[f"ad:{name}"] += 1
                    detail[f"ad:{name}"].append(f"{book}/{p.name}")
            for blk in INJECTED_BLOCKS.get(book, []):
                n = sum(1 for ln in text.splitlines() if BLOCK_RX[blk["name"]].search(ln))
                if n >= INJECT_MIN_MARKER_LINES:
                    left[f"block:{blk['name']}"] += 1
                    detail[f"block:{blk['name']}"].append(f"{book}/{p.name}({n}行)")
            if NAME_CONTROL_CHARS.search(p.name):
                left["control_char_name"] += 1
                detail["control_char_name"].append(p.name)
            # 编号重启只对斗破苍穹有意义（那里的同人续写本单要隔离）；
            # 凡人的 ±1~7 与少量大偏移属「登记不修」，进 residual 会让核账假失败
            if book == "斗破苍穹":
                no, _ = inner_title_no(text)
                sq = seq_of(p.name)
                if no is not None and sq is not None and sq - no > FAN_SEQUEL_DELTA_MIN:
                    left["fan_sequel_numbering"] += 1
                    detail["fan_sequel_numbering"].append(f"{book}/{p.name}")
    return {"left": left, "detail": {k: v[:12] for k, v in detail.items()}}


def cmd_verify(args: argparse.Namespace) -> int:
    scope = args.books.split(",") if args.books else SCOPE_BOOKS
    ledger = load_ledger()
    problems: list[str] = []
    a_e = [e for e in ledger if e["action"] == "A"]
    b_e = [e for e in ledger if e["action"] == "B"]
    r_e = [e for e in ledger if e["action"] == "R"]

    for e in a_e:
        p = ROOT / e["rel"]
        if not p.exists():
            if any(x["rel"] == e["rel"] and x["action"] in ("B", "R") for x in ledger):
                continue                      # 清理后又被隔离/改名：原位无文件是对的
            problems.append(f"A 类文件不存在：{e['rel']}")
            continue
        if not (ROOT / e["backup"]).exists():
            problems.append(f"A 类缺备份：{e['rel']}")
    bak_n = len(list(BACKUP_DIR.rglob("*.txt"))) if BACKUP_DIR.exists() else 0
    print(f"[核账] A 台账 {len(a_e)}｜备份文件 {bak_n}")
    if bak_n != len(a_e):
        problems.append(f"备份数 {bak_n} != A 台账数 {len(a_e)}")

    for e in b_e:
        if (ROOT / e["rel"]).exists():
            problems.append(f"B 类原位置仍在：{e['rel']}")
        if not (ROOT / e["new_rel"]).exists():
            problems.append(f"B 类隔离目标缺失：{e['new_rel']}")
    for e in r_e:
        if (ROOT / e["rel"]).exists():
            problems.append(f"R 类旧文件名仍在：{e['rel']}")
        if not (ROOT / e["new_rel"]).exists():
            problems.append(f"R 类新文件名缺失：{e['new_rel']}")
    print(f"[核账] B 隔离 {len(b_e)}｜R 重命名 {len(r_e)}")

    scan = rescan_books(scope)
    print("[核账] 三书重扫各模式剩余次数（应全为 0）：")
    for name, n in scan["left"].most_common():
        print(f"    {n:>5}  {name}  {scan['detail'][name][:5]}")
        problems.append(f"模式 {name} 仍剩 {n} 次")
    if not scan["left"]:
        print("    0（全部归零）")

    plan, reg, ov = build_plan(scope)
    print(f"[核账] 幂等复算：仍可执行动作 = {len(plan)}（应为 0）")
    for d in plan[:10]:
        print("    ⚠️", d["action"], d["rel"], d["reason"][:60])
    for b, v in ov["per_book"].items():
        r = reg[b]
        print(f"[核账] {b}: A{v['A_切尾']} B{v['B_整章隔离']} R{v['R_重命名']}"
              f"｜句中截断(登记) {r['句中截断_章数']}｜整章同文 "
              f"{len(r['重复清点']['相邻章整章同文'])} 对｜首尾重复粘贴 "
              f"{len(r['重复清点']['相邻章首尾重复粘贴'])} 对")
    if plan:
        problems.append(f"幂等不成立：仍可执行 {len(plan)} 项")

    if problems:
        print(f"\n🔴 核账不通过（{len(problems)} 条）：")
        for p in problems[:40]:
            print("   -", p)
        return 1
    print("\n✅ 核账通过")
    return 0


# ───────────────────────── survey（全库只读普查）─────────────────────────
def cmd_survey(args: argparse.Namespace) -> int:
    """三书之外的同类缺陷普查：只报数，不动文件。"""
    per_book: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[str]] = defaultdict(list)
    total_files = 0
    for book in iter_books():
        for p in chapter_files(book):
            total_files += 1
            text = read_text(p)
            ad_names = {match_inject_ad_line(ln) for ln in text.splitlines()}
            ad_names.discard(None)
            for name in ad_names:
                per_book[book.name][f"ad:{name}"] += 1
                if len(samples[f"ad:{name}"]) < 6:
                    samples[f"ad:{name}"].append(f"{book.name}/{p.name}")
            for blk in registered_blocks():   # 已登记块对撞全库（找同一段注入落在别的书）
                n = sum(1 for ln in text.splitlines() if BLOCK_RX[blk["name"]].search(ln))
                if n >= INJECT_MIN_MARKER_LINES:
                    per_book[book.name][f"block:{blk['name']}"] += 1
                    if len(samples[f"block:{blk['name']}"]) < 6:
                        samples[f"block:{blk['name']}"].append(f"{book.name}/{p.name}({n}行)")
            if NAME_CONTROL_CHARS.search(p.name):
                per_book[book.name]["control_char_name"] += 1
                samples["control_char_name"].append(ascii(p.name))
            no, _ = inner_title_no(text)
            sq = seq_of(p.name)
            if no is not None and sq is not None and sq - no > FAN_SEQUEL_DELTA_MIN:
                per_book[book.name]["章号差>100(仅规模统计,非缺陷)"] += 1
                samples["章号差>100(仅规模统计,非缺陷)"].append(f"{book.name}/{p.name}")
    L = ["# [DEV-DATA01b] 全库同类缺陷普查（只读，本单只处置三书）\n",
         f"- 扫描文件数 {total_files}｜书数 {len(per_book)}",
         "- 判据：`chapter_noise_patterns.py` DATA01b 段（广告锚点行 / 异质专名注入块 / 文件名控制字符）\n"
         "- `ad:*`=该文件含对应站点广告行；`block:*`=该文件含对应他书注入块（≥2 行命中）；"
         "`control_char_name`=文件名含控制字符；`章号差>100`=只是规模统计，"
         "全链路按文件名前缀口径，**不当缺陷**（同人续写要隔离另按编号重启+人工读，见 dry-run 报告）\n"
         "- ⚠ 三书之外只报数不动文件；其它书的 `block:*` 命中是**同一段注入落在别的书**，"
         "属下一批范围，需要按各自书目重新亲验——跨书撞名是真有的：实测 `艾米丽` 在"
         "《我师兄实在太稳健了》《橙红年代》里就是本书人名（假阳性），块指纹只在登记书目内生效\n"]
    pat_total: Counter[str] = Counter()
    for b, c in per_book.items():
        for k, v in c.items():
            pat_total[k] += v
    L.append("## 一、按模式×书命中数\n")
    L.append("| 模式 | 书 | 命中文件数 |")
    L.append("|---|---|---|")
    for b in sorted(per_book):
        for k, v in per_book[b].most_common():
            L.append(f"| `{k}` | {b} | {v} |")
    L.append("\n## 二、样例（≤50 字）\n")
    for k, v in samples.items():
        L.append(f"- `{k}`：{v[:6]}")
    out = OUT_DIR / "全库普查.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"[survey] 扫描 {total_files} 文件 → {out}")
    for k, v in pat_total.most_common():
        print(f"  {v:>6}  {k}")
    return 0


# ───────────────────────── 回滚（验收 2：逐文件可还原）─────────────────────────
def cmd_rollback(args: argparse.Namespace) -> int:
    """照 `清洗台账_二批.json` 逆向还原：A 用备份覆回原位，B 从隔离区 move 回，R 改回原名。

    素材目录不在 git 里（.gitignore `小说/`），备份+台账是唯一还原手段，所以这里逐条核验再动。
    默认只打印将要还原的条目；加 --force 才真还原。
    """
    ledger = load_ledger()
    if not ledger:
        print("台账为空，无可还原项")
        return 0
    plan = []
    for e in ledger:
        if e["action"] == "A":
            bak = ROOT / e["backup"]
            dst = ROOT / e["rel"]
            plan.append(("A", str(bak), str(dst), bak.exists()))
        elif e["action"] in ("B",):
            src = ROOT / e["new_rel"]
            dst = ROOT / e["rel"]
            plan.append(("B", str(src), str(dst), src.exists()))
        elif e["action"] == "R":
            cur = ROOT / e["new_rel"]
            old = cur.parent / Path(e["rel"]).name
            plan.append(("R", str(cur), str(old), cur.exists()))
    missing = [p for p in plan if not p[3]]
    print(f"台账 {len(ledger)} 条｜可还原 {len(plan) - len(missing)}｜源缺失 {len(missing)}")
    for k, s, _d, _ok in missing[:10]:
        print("  🔴 还原源不存在：", k, s)
    if not args.force:
        for k, s, d, _ok in plan[:12]:
            print(f"  [{k}] {s} -> {d}")
        print("（未加 --force，只打印不还原）")
        return 0
    done: list[tuple[str, str]] = []
    errors: list[str] = []
    for kind, s, d, ok in plan:
        if not ok:
            continue
        try:
            if kind == "A":
                shutil.copy2(s, d)
            else:
                if Path(d).exists():
                    errors.append(f"目标已存在，跳过：{d}")
                    continue
                shutil.move(s, d) if kind == "B" else Path(s).rename(Path(d))
            done.append((kind, d))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s} -> {d}: {type(e).__name__}: {e}")
    print(f"ROLLBACK：还原 {len(done)} 条｜失败 {len(errors)}")
    for e in errors[:10]:
        print("  🔴", e)
    return 1 if errors else 0


# ───────────────────────── 处置清单（从台账出，apply 后可复看）─────────────────────────
def cmd_ledger_report(args: argparse.Namespace) -> int:
    """把已落地的 171 项动作出成可读清单：备份/隔离目标/切点/删掉多少字，逐文件可还原。"""
    led = load_ledger()
    c = Counter(e["action"] for e in led)
    L = ["# [DEV-DATA01b] 处置清单（由 `清洗台账_二批.json` 生成，逐文件可还原）\n",
         f"- 生成（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
         f"｜台账 {len(led)} 条 = A {c.get('A', 0)} + B {c.get('B', 0)} + R {c.get('R', 0)}",
         "- 回滚：`.venv\\Scripts\\python.exe backend\\scripts\\clean_injected_blocks.py --rollback --force`",
         "- A 类原文完整保存在 `outputs/data01b/backup/<书名>/`；B 类只 move 未删除，在 `小说/_quarantine/<书名>/`\n",
         "| 动作 | 书 | 文件 | 判据 | 切点行 | 删掉汉字 | 还原凭据 |",
         "|---|---|---|---|---|---|---|"]
    for e in led:
        book = Path(e["rel"]).parent.name
        name = Path(e["rel"]).name
        cut = e.get("cut_idx")
        back = e.get("backup") or e.get("new_rel") or "-"
        L.append(f"| {e['action']} | {book} | `{name}` | `{e.get('rule', '')}` "
                 f"| {cut if cut is not None else '-'} | {e.get('removed_cjk', '-') if e['action'] == 'A' else '-'} "
                 f"| `{back}` |")
    out = OUT_DIR / "处置清单.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"[处置清单] {len(led)} 条 -> {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--survey", action="store_true", help="全库只读普查（含三书之外）")
    ap.add_argument("--dry-run", action="store_true", help="出 dry-run 报告 + 决策明细（不写素材）")
    ap.add_argument("--apply", action="store_true", help="按批准清单执行（需 --force）")
    ap.add_argument("--verify", action="store_true", help="按文件系统现状核账")
    ap.add_argument("--rollback", action="store_true", help="照台账逆向还原（需 --force）")
    ap.add_argument("--ledger-report", action="store_true", help="从台账出可读处置清单")
    ap.add_argument("--force", action="store_true", help="apply/rollback 二次确认")
    ap.add_argument("--books", default="", help="逗号分隔书名，默认三书")
    ap.add_argument("--decisions", default=str(DECISIONS))
    args = ap.parse_args()
    if args.survey:
        return cmd_survey(args)
    if args.dry_run:
        return cmd_dry_run(args)
    if args.apply:
        return cmd_apply(args)
    if args.verify:
        return cmd_verify(args)
    if args.rollback:
        return cmd_rollback(args)
    if args.ledger_report:
        return cmd_ledger_report(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
