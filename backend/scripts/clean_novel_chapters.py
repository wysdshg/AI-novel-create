# -*- coding: utf-8 -*-
"""[DEV-DATA01] 素材章节体检与清洗：章内清理（A）+ 整章隔离（B）。

零 LLM 调用：全部确定性正则（模式常量在 chapter_noise_patterns.py 顶部，供审阅）。

    --scan       只读体检 → outputs/data_clean/扫描报告.md + 扫描明细.json（gate ② 交付物）
    --dry-run    按当前批准口径打印将处理的全部文件与动作（不写任何东西）
    --apply      真改：A 先整文件备份再改写；B 移动到 小说/_quarantine/<书名>/；台账 outputs/data_clean/清洗台账.json
    --verify     apply 后按文件系统现状核账（docs/04 A14：别信返回值）

口径来源（用户 gate ① 拍板 2026-10-03）：38 本全清 / 爬虫末行标记要清 / 空占位章隔离 / 番外隔离 /
`_残章` 与 `书籍信息.txt` 跳过不动。

清洗算法三趟（本库实测：段落 = 一行一段、空行分隔，因此按行处理即按块处理）：
  ① 逐行：整行噪声 → 删该行；行内噪声短语 → 只删片段，抠完再判一次整行噪声
     （赘婿的翻页 nag 挂着的 `</div>` 抠掉后剩下的正是纯 nag），残留无汉字则整行删
  ② 星号行：只在「位于文件后半部 + 星号前正文 ≥200 汉字 + 其后 200 字内命中求票话术」时删
  ③ 书尾作者话块：从文件尾往回走，先攒候选，**只有见过强标记**（作者有话说/PS/求票/更数/
     星号话术块）才提交删除；含引号书名的行、>120 汉字的行、总量 >300 汉字立即停
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chapter_noise_patterns import (  # noqa: E402
    DIALOGUE_MARK, DUP_FINGERPRINT, DUP_MIN_CHARS, EMPTY_BODY_MIN_CJK, GARBLED_CJK_RATIO,
    GARBLED_MAX_CJK, PLEA_KEYWORDS, SEQ_PREFIX,
    QUARANTINE_TITLE, SKIP_DIRS, SKIP_FILES, SMALL_NOISE_MAX_BYTES, SMALL_NOISE_TITLE,
    STAR_TAIL_MIN_BODY, STAR_TAIL_SHORT_MAX_CJK, STAR_TAIL_WINDOW, TAIL_AUTHOR_MAX_CJK,
    TAIL_AUTHOR_MAX_LINES, TAIL_AUTHOR_MAX_TOTAL_CJK, TAIL_STRONG_MARKERS, TIER2, TAIL_PLEA,
    TITLE_BRACKET_KEEP, TITLE_BRACKET_SEG,
    NOTICE_HINT, NOTICE_MAX_CJK, NOTICE_RATIO, apply_inline, cjk_count, classify_pure_line,
    match_star_line,
)

ROOT = Path(__file__).resolve().parents[2]
NOVEL_ROOT = ROOT / "小说"
OUT_DIR = ROOT / "outputs" / "data_clean"
BACKUP_DIR = OUT_DIR / "backup"
LEDGER = OUT_DIR / "清洗台账.json"
DECISIONS = OUT_DIR / "扫描明细.json"
QUARANTINE_DIRNAME = "_quarantine"

ANY_EOL = re.compile(r"\r\n|\r|\n")

# 🔴 人工读过内容后追加的隔离项/保留项（精确文件名，扫描报告逐条给理由）
# QUARANTINE_EXTRA_NAMES：正则判据（notice_short 要求 ≥50% 话术词）够不着、但我逐行读过确认
#   **整章就是作者话**（请假/更新延迟/拜年/推书/改稿说明/月票总结）的文件——
#   它们残留 63~290 汉字且通知词占比 0~45%，机械口径判不出来，只能人工点名隔离（移动不删除）。
#   反例（同样短小但读过确认是正文残段，留在 A 类）：蛊真人各「第X百节」残段、我师兄/0134~0137、
#   秦吏/0122~0305、宰执天下/0725、橙红年代、回到明朝当王爷书友赠诗（0068~0070、0130）。
QUARANTINE_EXTRA_NAMES: dict[str, set[str]] = {
    "吞噬星空": {
        "0109_第109章明天三更.txt",
        "0607_第607章今天第二更第三更会比较晚.txt",
        "0610_第610章病倒了……今天没法更新了.txt",
        "0802_第802章不管如何，我们已经竭尽全力.txt",
        "0908_第908章今天暂停一天，准备第十七篇.txt",
        "1093_第1093章今天的另外两章会很晚，大家可以明早看.txt",
        "1180_第1180章番茄给大家拜年啦~~~~~~.txt",
    },
    "我师兄实在太稳健了": {
        "0034_关于一点细节！（临时公告，下午删除）.txt",
        "0374_三万均订！.txt",
        "0670_在医院.txt",
        "0674_单章：八月份总结.txt",
        "0801_做了个不太稳健的决定【关于新书】.txt",
    },
    "大奉打更人": {
        "0280_今天大扫除，更新晚点。.txt",
        "0311_凌晨后更新，写个大章.txt",
        "0503_上来说一声，更新延迟。.txt",
        "0510_说一说更新的事。.txt",
        "0517_哦，今晚的两更已经结束了。.txt",
        "0702_说说更新问题。.txt",
    },
    "赘婿": {
        "0077_七十五章作了修改并拉票.txt",
        "0480_未来几天，更新不定，一次大调整.txt",
        "0611_元旦快乐.txt",
        "0714_六七四章有修改.txt",
    },
    "蛊真人": {
        "1080_第三波净网活动开始了。.txt",
        "1094_看这里，领2016春节红包！.txt",
        "1200_求一些正版订阅支持！.txt",
        "1386_2016年8月书友活动！.txt",
        "2026_今天更新推迟及未来写作计划.txt",
        "2290_推荐《重生之奇异都市》.txt",
    },
    "秦吏": {"0721_更新在晚上，自从和齐佩甲睡了两天之后…….txt"},
    "绍宋": {"0191_大家反应有点过度，还是说一下吧.txt"},
    "雪中悍刀行": {"0368_第一！.txt"},
    "庆余年": {"0776_无言而伪装从容地请个假.txt"},
}
KEEP_NAMES: dict[str, set[str]] = {}


# ───────────────────────── 单行判定 ─────────────────────────
def classify_line(content: str) -> tuple[str, str, str]:
    """返回 (状态, 模式名, 新内容)。状态：blank | drop | strip | body。"""
    s = content.strip()
    if not s:
        return "blank", "", content
    lead = content[:len(content) - len(content.lstrip())]
    name = classify_pure_line(s)
    if name:
        return "drop", name, content
    stripped, names = apply_inline(s)
    if names:
        # 行内噪声抠掉之后可能正好变成一条独立噪声行（赘婿：`小主…后面更精彩！ </div>`）
        name2 = classify_pure_line(stripped)
        tag = "+".join(dict.fromkeys(names))
        if name2 or cjk_count(stripped) == 0:
            return "drop", f"{tag}+{name2 or 'empty'}", content
        return "strip", tag, lead + stripped.rstrip()
    return "body", "", content


def clean_text(text: str) -> tuple[str, list[dict], int]:
    """返回 (清洗后文本, 命中记录, 清噪后残留正文汉字数)。"""
    rows: list[dict] = []
    for ln in text.splitlines(keepends=True):
        content = ANY_EOL.sub("", ln)
        rows.append({"content": content, "eol": ln[len(content):]})

    hits: list[dict] = []
    for r in rows:
        st, name, new_content = classify_line(r["content"])
        r["status"], r["pattern"] = st, name
        if st in ("drop", "strip"):
            r["content"] = new_content
            hits.append({"pattern": name, "text": new_content.strip()[:60], "action": st})

    _star_pass(rows, hits)
    _tail_pass(rows, hits)

    # 重建：删掉的行连同「紧随其后的那一个空行」一起去掉；没删东西就不动任何空行
    #（本库 2406 个文件本来就有连续空行，无差别压缩 = 任务范围外的改动）
    dropped_after_body = False
    last_body = max((i for i, r in enumerate(rows)
                     if r["status"] != "drop" and r["content"].strip()), default=-1)
    out_parts: list[str] = []
    just_dropped = False
    for i, r in enumerate(rows):
        if r["status"] == "drop":
            just_dropped = True
            dropped_after_body = dropped_after_body or i > last_body
            continue
        is_blank = r["content"].strip() == ""
        if is_blank and just_dropped:
            just_dropped = False        # 只吃掉删除点后面这一个空行
            continue
        out_parts.append(r["content"] + r["eol"])
        just_dropped = False
    out = "".join(out_parts)
    if dropped_after_body:
        # 文件尾巴被删了：收掉多余空行，并把结尾风格还原成原文的样子（90% 无结尾换行、10% 有）
        out = out.rstrip("\r\n")
        if text.endswith(("\n", "\r")):
            out += "\r\n" if "\r\n" in text else "\n"
    return out, hits, cjk_count(out)


def _star_pass(rows: list[dict], hits: list[dict]) -> None:
    """星号行/星号+短话术行：只在尾部区域且后面紧跟求票话术时删（正文分节符不能碰）。"""
    joined = "".join(r["content"] for r in rows)
    total = len(joined) or 1
    starts: list[int] = []
    acc_c = acc_k = 0
    for r in rows:
        starts.append(acc_c)
        r["_body_before"] = acc_k          # 本行之前有多少汉字正文
        acc_c += len(r["content"])
        acc_k += cjk_count(r["content"])
    for i, r in enumerate(rows):
        if r["status"] not in ("body", "strip"):
            continue
        rest = match_star_line(r["content"])
        if rest is None or cjk_count(rest) > STAR_TAIL_SHORT_MAX_CJK:
            continue
        if starts[i] / total < 0.5:                 # 前半部的星号 = 正文分节符
            continue
        if r["_body_before"] < STAR_TAIL_MIN_BODY:   # 前面没正文 → 整章就是噪声，交给 B 类判据
            continue
        after = joined[starts[i] + len(r["content"]): starts[i] + len(r["content"]) + STAR_TAIL_WINDOW]
        if not PLEA_KEYWORDS.search(after):
            continue
        r["status"] = "drop"
        r["pattern"] = "star_tail_plea"
        hits.append({"pattern": "star_tail_plea", "text": r["content"].strip()[:60], "action": "drop"})


def _tail_pass(rows: list[dict], hits: list[dict]) -> None:
    """从文件尾往回：强标记（作者有话说/PS/求票/更数/星号话术）之后到文末的非叙事块整段删。

    覆盖用户点名的圣墟式污染：`******` + 「新书阶段，点击、收藏、投票一个都不能少…」多行段落。
    回扫途中先攒候选，**只有真的见过强标记才提交删除**——避免把「恰好以对话收尾」的正文砍掉。
    """
    i = len(rows) - 1
    while i >= 0 and rows[i]["content"].strip() == "":
        i -= 1
    cand: list[int] = []
    marker = False
    total_cjk = 0
    while i >= 0:
        r = rows[i]
        if r["content"].strip() == "":
            i -= 1
            continue
        if r["status"] == "drop":                     # 第①趟已判定为噪声，本来就要删
            if r["pattern"] in TAIL_STRONG_MARKERS:
                marker = True
            i -= 1
            continue
        n = cjk_count(r["content"])
        if n == 0:                                # 纯符号行（分隔线/残渣），删了不丢正文
            cand.append(i)
            i -= 1
            continue
        talkish = (n <= TAIL_AUTHOR_MAX_CJK and not DIALOGUE_MARK.search(r["content"])
                   and bool(TAIL_PLEA.search(r["content"])))
        if not talkish or total_cjk + n > TAIL_AUTHOR_MAX_TOTAL_CJK or len(cand) >= TAIL_AUTHOR_MAX_LINES:
            break
        total_cjk += n
        cand.append(i)
        i -= 1
    if not marker:
        return
    for j in cand:
        rows[j]["status"] = "drop"
        rows[j]["pattern"] = "tail_author_block"
        hits.append({"pattern": "tail_author_block",
                     "text": rows[j]["content"].strip()[:60], "action": "drop"})


# ───────────────────────── 文件遍历与决策 ─────────────────────────
def iter_books() -> list[Path]:
    return sorted(p for p in NOVEL_ROOT.iterdir()
                  if p.is_dir() and p.name not in SKIP_DIRS and p.name != QUARANTINE_DIRNAME)


def chapter_files(book: Path) -> list[Path]:
    return sorted(f for f in book.glob("*.txt") if f.name not in SKIP_FILES)


def title_body(stem: str) -> str:
    return stem.split("_", 1)[1] if "_" in stem else stem


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def notice_ratio(text: str) -> float:
    """残留正文里有多少字属于「通知/求票/拜年/抓取失败」话术。"""
    total = 0
    talk = 0
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        n = cjk_count(s)
        total += n
        if NOTICE_HINT.search(s):
            talk += n
    return talk / total if total else 0.0


def decide(book: str, path: Path) -> dict:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    new_text, hits, residual = clean_text(text)
    pat_counts = dict(Counter(h["pattern"] for h in hits))
    pat_examples: dict[str, str] = {}
    for h in hits:
        pat_examples.setdefault(h["pattern"], h["text"])
    tier2 = []
    for ln in new_text.splitlines():
        s = ln.strip()
        if not s:
            continue
        for name, rx in TIER2:
            if rx.search(s):
                tier2.append({"pattern": name, "text": s[:60]})
                break
    nratio = notice_ratio(new_text) if residual < NOTICE_MAX_CJK else 0.0
    # 隔离会移走整章，所以给出「清噪后最长的一行」让人看得见移走了什么
    preview = max((ln.strip() for ln in new_text.splitlines() if ln.strip()), key=len, default="")
    d = {
        "book": book, "file": path.name, "rel": str(path.relative_to(ROOT)),
        "size": len(raw), "residual_cjk": residual, "preview": preview[:60],
        "lost_cjk": cjk_count(text) - residual, "notice_ratio": round(nratio, 3),
        "pat_counts": pat_counts, "pat_examples": pat_examples, "n_hits": len(hits),
        "examples": [h["text"] for h in hits[:2]],
        # 重复判定用「清噪后只剩汉字/字母数字」的指纹，避免同一章因噪声差异逃过查重
        "dup_key": (hashlib.md5(DUP_FINGERPRINT.sub("", new_text).encode("utf-8")).hexdigest()
                    if residual >= DUP_MIN_CHARS else ""),
        "tier2": tier2[:4], "action": "none", "reason": "", "rule": "",
    }
    if KEEP_NAMES.get(book) and path.name in KEEP_NAMES[book]:
        d.update(rule="manual_keep", reason="人工确认保留（看着像非正文，实为正文）")
        return d
    title = title_body(path.stem)
    # 括号补充语（（内附更新说明）/【求票】）是顺带一说，摘掉后再判标题主体；括号里写「番外」仍隔离
    bare_title = TITLE_BRACKET_SEG.sub("", title)
    if QUARANTINE_TITLE.search(bare_title) or TITLE_BRACKET_KEEP.search(title):
        d.update(action="B", rule="title_nonbody", reason="标题主体即非正文（番外/感言/后记/群公告/完本）")
        return d
    if len(raw) < SMALL_NOISE_MAX_BYTES and SMALL_NOISE_TITLE.search(bare_title):
        d.update(action="B", rule="title_notice_small",
                 reason=f"标题是通知/求票/请假类且整文件仅 {len(raw)}B < {SMALL_NOISE_MAX_BYTES}B（单章通知，非正文章）")
        return d
    if QUARANTINE_EXTRA_NAMES.get(book) and path.name in QUARANTINE_EXTRA_NAMES[book]:
        d.update(action="B", rule="manual_read", reason="人工读过内容确认：整章非正文")
        return d
    if residual < EMPTY_BODY_MIN_CJK:
        d.update(action="B", rule="empty_body",
                 reason=f"清噪后正文只剩 {residual} 汉字，低于 {EMPTY_BODY_MIN_CJK}（空占位章/纯通知章）")
        return d
    if residual < GARBLED_MAX_CJK and residual / max(len(new_text), 1) < GARBLED_CJK_RATIO:
        d.update(action="B", rule="garbled_body",
                 reason=f"残留仅 {residual} 汉字却占 {len(new_text)} 字符（汉字比例 "
                        f"{residual / max(len(new_text), 1):.2f}<{GARBLED_CJK_RATIO}），抓取乱码章")
        return d
    if residual < NOTICE_MAX_CJK and nratio >= NOTICE_RATIO:
        d.update(action="B", rule="notice_short",
                 reason=f"短章（残留 {residual} 汉字 < {NOTICE_MAX_CJK}）且 "
                        f"{nratio * 100:.0f}% 是更新通知/求票/拜年/抓取失败话术")
        return d
    if hits:
        d.update(action="A", rule="noise_hits", reason="章内噪声清理（正文保留）")
        return d
    d["reason"] = "无命中"
    return d


# ───────────────────────── C 类：书内重复章节 ─────────────────────────
def _seq_of(name: str) -> int:
    m = SEQ_PREFIX.match(name)
    return int(m.group("seq")) if m else 10 ** 9


def mark_duplicates(items: list[dict]) -> int:
    """同一本书内、清噪后正文指纹完全相同的文件：保留序号最小者，其余标 C 隔离。

    已被判 B（整章非正文）或正文太短（dup_key 为空）的不参与——空壳章/通知章清噪后天然同文，
    混进来会互相误判。返回本次标记的重复文件数。
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in items:
        if d["action"] == "B" or not d["dup_key"]:
            continue
        groups[d["dup_key"]].append(d)
    n = 0
    for grp in groups.values():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda x: (_seq_of(x["file"]), x["file"]))
        for d in grp[1:]:
            d["dup_of"] = grp[0]["file"]
            d["action"] = "C"
            d["rule"] = "duplicate"
            d["reason"] = (f"书内重复：清噪后正文与 {grp[0]['file']} 完全相同，"
                           f"保留序号最小者，本文件整章隔离")
            n += 1
    return n


# ───────────────────────── --scan ─────────────────────────
def cmd_scan(args: argparse.Namespace) -> int:
    decisions = []
    unreadable: list[str] = []
    for book in iter_books():
        per_book = []
        for f in chapter_files(book):
            try:
                per_book.append(decide(book.name, f))
            except FileNotFoundError:
                unreadable.append(f"{book.name}/{f.name}")
        mark_duplicates(per_book)
        decisions.extend(per_book)
    if unreadable:
        print(f"[scan] ⚠️ 列出但读不到的文件 {len(unreadable)} 个（已跳过）："
              f"{unreadable[:5]}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DECISIONS.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
    write_scan_report(decisions, Path(args.report))
    c = Counter(d["action"] for d in decisions)
    print(f"[scan] {len(decisions)} 文件｜A {c.get('A', 0)}｜B {c.get('B', 0)}｜"
          f"C {c.get('C', 0)}｜不动 {c.get('none', 0)}")
    print(f"[scan] 报告 -> {args.report}\n[scan] 明细 -> {DECISIONS}")
    return 0


def write_scan_report(decisions: list[dict], out: Path) -> None:
    by_action = Counter(d["action"] for d in decisions)
    per_book: dict[str, Counter[str]] = defaultdict(Counter)
    pat_total: Counter[str] = Counter()
    pat_books: dict[str, set[str]] = defaultdict(set)
    pat_samples: dict[str, list[str]] = defaultdict(list)
    tier2_total: Counter[str] = Counter()
    for d in decisions:
        per_book[d["book"]][d["action"]] += 1
        for name, n in d["pat_counts"].items():
            pat_total[name] += n
            pat_books[name].add(d["book"])
            if len(pat_samples[name]) < 4:
                ex = d["pat_examples"].get(name) or (d["examples"][0] if d["examples"] else "")
                pat_samples[name].append(f"{d['book']}/{d['file']}：{ex[:60]}")
        for t in d["tier2"]:
            tier2_total[t["pattern"]] += 1

    L: list[str] = []
    L.append("# [DEV-DATA01] 章节噪声扫描报告（gate ②：用户批准正则清单与分类后才动文件）\n")
    L.append(f"- 生成（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    L.append(f"- 扫描文件数 **{len(decisions)}**；零 LLM 调用，全部确定性正则")
    L.append(f"- 模式常量：`backend/scripts/chapter_noise_patterns.py`")
    L.append(f"- 算法：`backend/scripts/clean_novel_chapters.py:clean_text()`\n")

    L.append("## 一、动作分布\n")
    L.append("| 动作 | 文件数 | 占比 | 动作含义 |")
    L.append("|---|---|---|---|")
    tot = len(decisions) or 1
    L.append(f"| A 章内清理 | {by_action.get('A', 0)} | {by_action.get('A', 0) / tot * 100:.1f}% | 删噪声行/片段，正文保留；改写前整文件备份 |")
    L.append(f"| B 整章隔离 | {by_action.get('B', 0)} | {by_action.get('B', 0) / tot * 100:.1f}% | 移动到 `小说/_quarantine/<书名>/`，不删除 |")
    L.append(f"| 不动 | {by_action.get('none', 0)} | {by_action.get('none', 0) / tot * 100:.1f}% | 无命中或人工保留 |")
    L.append("")

    L.append("## 二、按书统计\n")
    L.append("| 书 | A | B | 不动 | 合计 |")
    L.append("|---|---|---|---|---|")
    for b in sorted(per_book):
        c = per_book[b]
        L.append(f"| {b} | {c.get('A', 0)} | {c.get('B', 0)} | {c.get('none', 0)} | {sum(c.values())} |")
    L.append("")

    L.append("## 三、A 类「被删汉字」最多的 20 个文件（正文流失上界审计，逐条过目）\n")
    al = [d for d in decisions if d["action"] == "A"]
    al_sorted = sorted(al, key=lambda d: d["lost_cjk"], reverse=True)
    L.append("| 书 | 文件 | 原汉字 | 清噪后残留 | 被删汉字 | 命中模式 |")
    L.append("|---|---|---|---|---|---|")
    for d in al_sorted[:20]:
        L.append(f"| {d['book']} | `{d['file']}` | {d['lost_cjk'] + d['residual_cjk']} "
                 f"| {d['residual_cjk']} | {d['lost_cjk']} | {', '.join(sorted(d['pat_counts'])[:5])} |")
    L.append("")

    L.append("### 三之二、A 类里清噪后残留 <300 汉字的文件（短章/通知章嫌疑，逐条定夺是否改判 B）\n")
    thin = sorted([d for d in al if d["residual_cjk"] < 300], key=lambda d: d["residual_cjk"])
    L.append(f"共 {len(thin)} 个；通知话术占比 = 残留正文里命中 NOTICE_HINT 的字数比例。\n")
    L.append("| 书 | 文件 | 残留汉字 | 通知占比 | 清噪后首行（≤40 字） |")
    L.append("|---|---|---|---|---|")
    for d in thin[:140]:
        L.append(f"| {d['book']} | `{d['file']}` | {d['residual_cjk']} | {d['notice_ratio']:.0%} | {d['preview']} |")
    L.append("")

    L.append("## 四、正则清单命中统计（样例引用 ≤50 字）\n")
    L.append("| 模式 | 命中次数 | 涉及书 | 样例 |")
    L.append("|---|---|---|---|")
    for name, n in pat_total.most_common():
        ex = (pat_samples.get(name) or ["-"])[0][-90:]
        L.append(f"| `{name}` | {n} | {len(pat_books[name])} | {ex} |")
    L.append("")

    L.append("## 五、B/C 类（整章隔离）全量清单 — 请逐条过目\n")
    blist = [d for d in decisions if d["action"] in ("B", "C")]
    reasons = Counter(d["rule"] for d in blist)
    L.append("判据分布：" + "；".join(f"`{k}` = {v}" for k, v in reasons.most_common()) + "\n")
    L.append("| 书 | 文件 | 大小 | 残留汉字 | 通知占比 | 判据 | 重复于 | 清噪后最长一行（移走的就是这些） |")
    L.append("|---|---|---|---|---|---|---|---|")
    for d in blist:
        L.append(f"| {d['book']} | `{d['file']}` | {d['size']}B | {d['residual_cjk']} | {d['notice_ratio']:.0%} "
                 f"| `{d['rule']}` | {d.get('dup_of', '-')} | {d['preview']} |")
    L.append("")

    L.append("## 六、A 类样例（每本抽 5 个，列出将被删掉的噪声）\n")
    per_b: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        if d["action"] == "A" and len(per_b[d["book"]]) < 5:
            per_b[d["book"]].append(d)
    L.append("| 书 | 文件 | 命中模式 | 被删噪声（≤50 字） |")
    L.append("|---|---|---|---|")
    for b in sorted(per_b):
        for d in per_b[b]:
            L.append(f"| {b} | `{d['file']}` | {', '.join(sorted(d['pat_counts']))[:60]} | {' ∥ '.join(d['examples'])[:80]} |")
    L.append("")

    L.append("## 七、TIER2 疑似作者话（**本单默认不删**，只报告；要删请明确批准）\n")
    L.append(f"命中计数：{dict(tier2_total.most_common())}\n")
    L.append("| 书 | 文件 | 类型 | 行内容（≤50 字） |")
    L.append("|---|---|---|---|")
    shown = 0
    for d in decisions:
        for t in d["tier2"]:
            if shown >= 120:
                break
            L.append(f"| {d['book']} | `{d['file']}` | {t['pattern']} | {t['text']} |")
            shown += 1
        if shown >= 120:
            break
    L.append("")

    L.append("## 八、每本书尾 30 个文件的分类判定（番外/感言集中区，逐个看过）\n")
    books = sorted({d["book"] for d in decisions})
    for b in books:
        rows = [d for d in decisions if d["book"] == b]
        L.append(f"### {b}（{len(rows)} 个文件，下列为末尾 30 个）\n")
        L.append("| 判定 | 文件 | 大小 | 清噪后残留 | 命中模式 |")
        L.append("|---|---|---|---|---|")
        for d in rows[-30:]:
            mark = {"A": "🟡A", "B": "🔴B", "C": "🟠C", "none": "⚪不动"}[d["action"]]
            L.append(f"| {mark} | `{d['file']}` | {d['size']}B | {d['residual_cjk']} | {', '.join(sorted(d['pat_counts'])[:4]) or '-'} |")
        L.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")


# ───────────────────────── --dry-run / --apply ─────────────────────────
def load_ledger() -> list[dict]:
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []


def append_ledger(entries: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    allv = load_ledger()
    seen = {(e["rel"], e["action"]) for e in allv}
    allv.extend(e for e in entries if (e["rel"], e["action"]) not in seen)
    LEDGER.write_text(json.dumps(allv, ensure_ascii=False, indent=1), encoding="utf-8")


def plan_from_disk(only: set[tuple[str, str]] | None = None) -> tuple[list[dict], list[str]]:
    """按文件系统现状重新判定；only = 已批准的 (rel, action)。返回 (计划, 与批准清单的差异说明)。"""
    plan: list[dict] = []
    mismatches: list[str] = []
    seen: set[tuple[str, str]] = set()
    for book in iter_books():
        per_book = []
        for f in chapter_files(book):
            try:
                per_book.append(decide(book.name, f))
            except FileNotFoundError:
                # 目录项能列出但读不到（Windows 上移动/删除后的短暂不一致）：跳过并记一条，不整体崩
                mismatches.append(f"文件列出但读不到，已跳过：{book.name}/{f.name}")
        mark_duplicates(per_book)
        for d in per_book:
            if d["action"] == "none":
                continue
            key = (d["rel"], d["action"])
            seen.add(key)
            if only is not None and key not in only:
                mismatches.append(f"现状有动作但不在批准清单：{d['rel']}（{d['action']}）")
                continue
            plan.append(d)
    if only is not None:
        for rel, action in only - seen:
            mismatches.append(f"批准过但现状不再需要处理（多半已清/已隔离）：{rel}（{action}）")
    return plan, mismatches


def cmd_dry_run(args: argparse.Namespace) -> int:
    plan, mis = plan_from_disk()
    for d in plan:
        print(f"[{d['action']}] {d['rel']}｜{d['size']}B｜{','.join(sorted(d['pat_counts']))[:60]}")
    c = Counter(d["action"] for d in plan)
    print(f"\nDRY-RUN：A {c.get('A', 0)}｜B {c.get('B', 0)}｜C {c.get('C', 0)}｜"
          f"合计 {len(plan)}（未写任何文件）")
    for m in mis[:10]:
        print("  ⚠️", m)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.decisions_path.exists():
        print(f"🔴 缺批准清单：{args.decisions_path}（先 --scan 并经用户批准）")
        return 2
    approved = json.loads(args.decisions_path.read_text(encoding="utf-8"))
    keys = {(d["rel"], d["action"]) for d in approved if d["action"] in ("A", "B", "C")}
    plan, mis = plan_from_disk(only=keys)
    print(f"批准清单 {len(keys)} 项｜现状可执行 {len(plan)} 项｜口径漂移 {len(mis)} 条")
    for m in mis[:20]:
        print("  ⚠️", m)
    if not args.force:
        print("🔴 未加 --force，停止（这是 dry 保护）")
        return 2

    entries: list[dict] = []
    errors: list[str] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i, d in enumerate(plan, 1):
        src = ROOT / d["rel"]
        try:
            if d["action"] == "A":
                raw = src.read_bytes()
                new_text, hits, _ = clean_text(raw.decode("utf-8"))
                if not hits:                       # 幂等：已经干净就跳过
                    continue
                bak = BACKUP_DIR / d["book"] / src.name
                bak.parent.mkdir(parents=True, exist_ok=True)
                if not bak.exists():
                    shutil.copy2(src, bak)
                tmp = src.parent / (src.name + ".tmp")
                tmp.write_bytes(new_text.encode("utf-8"))
                tmp.replace(src)
                entries.append({"rel": d["rel"], "action": "A",
                                "backup": str(bak.relative_to(ROOT)),
                                "patterns": sorted(d["pat_counts"]),
                                "before_bytes": len(raw), "after_bytes": len(new_text.encode("utf-8")),
                                "ts_utc": ts})
            else:
                dst_dir = NOVEL_ROOT / QUARANTINE_DIRNAME / d["book"]
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / src.name
                if dst.exists():
                    errors.append(f"隔离目标已存在，跳过：{dst.relative_to(ROOT)}")
                    continue
                size = src.stat().st_size
                shutil.move(str(src), str(dst))
                entries.append({"rel": d["rel"], "action": d["action"],
                                "new_rel": str(dst.relative_to(ROOT)),
                                "patterns": sorted(d["pat_counts"]), "before_bytes": size,
                                "dup_of": d.get("dup_of", ""), "ts_utc": ts})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{d['rel']}: {type(e).__name__}: {e}")
        if i % 4000 == 0:
            print(f"  ... {i}/{len(plan)}")
    append_ledger(entries)
    n_a = sum(1 for e in entries if e["action"] == "A")
    n_b = sum(1 for e in entries if e["action"] == "B")
    n_c = sum(1 for e in entries if e["action"] == "C")
    print(f"APPLY：A 改写 {n_a}｜B 隔离 {n_b}｜C 重复隔离 {n_c}｜失败 {len(errors)}｜"
          f"台账 {len(load_ledger())} 条")
    for e in errors[:20]:
        print("  🔴", e)
    return 1 if errors else 0


# ───────────────────────── --verify ─────────────────────────
def _batch_b_rels() -> set[str]:
    """后批次（DATA01b）台账里被整章隔离/改名的原路径——本批台账只管到自己那批为止。

    文件不存在就返回空集：老库单独跑 --verify 时行为与从前一致。
    """
    p = ROOT / "outputs" / "data01b" / "清洗台账_二批.json"
    if not p.exists():
        return set()
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return set()
    return {e["rel"] for e in rows if e.get("action") in ("B", "R")}


def cmd_verify(args: argparse.Namespace) -> int:
    """核账只看文件系统现状（docs/04 A14）。"""
    ledger = load_ledger()
    a_e = [e for e in ledger if e["action"] == "A"]
    b_e = [e for e in ledger if e["action"] in ("B", "C")]
    problems: list[str] = []

    # 同一文件可能先被 A 改写、后又被 B/C 整章隔离——那时原位没有文件是正确的，不算问题
    # 二批（DATA01b，他书注入块整章隔离）是另一本台账，这里一并读，否则会假报「A 类文件不存在」
    later_quarantined = {e["rel"] for e in b_e} | _batch_b_rels()
    moved_after_clean = 0
    residue_a = 0
    for e in a_e:
        p = ROOT / e["rel"]
        if not p.exists():
            if e["rel"] in later_quarantined:
                moved_after_clean += 1
                continue
            problems.append(f"A 类文件不存在：{e['rel']}")
            continue
        _new, hits, _r = clean_text(read_text(p))
        if hits:
            residue_a += 1
            if len(problems) < 25:
                problems.append(f"A 类仍有噪声：{e['rel']} -> {sorted({h['pattern'] for h in hits})[:4]}")
    print(f"[核账] A 台账 {len(a_e)}｜重扫仍有噪声 = {residue_a}"
          f"｜清理后又被整章隔离 = {moved_after_clean}")

    still = sum(1 for e in b_e if (ROOT / e["rel"]).exists())
    gone = sum(1 for e in b_e if not (ROOT / e["new_rel"]).exists())
    for e in b_e:
        if (ROOT / e["rel"]).exists() and len(problems) < 40:
            problems.append(f"隔离类(B/C)原位置仍在：{e['rel']}")
        if not (ROOT / e["new_rel"]).exists() and len(problems) < 40:
            problems.append(f"隔离类(B/C)目标缺失：{e['new_rel']}")
    print(f"[核账] 隔离类(B+C) 台账 {len(b_e)}｜原位置残留 = {still}｜目标缺失 = {gone}")

    backups = list(BACKUP_DIR.rglob("*.txt")) if BACKUP_DIR.exists() else []
    print(f"[核账] 备份文件 {len(backups)}｜A 台账 {len(a_e)}")
    if len(backups) != len(a_e):
        problems.append(f"备份数 {len(backups)} != A 台账数 {len(a_e)}")

    left: Counter[str] = Counter()
    for book in iter_books():
        for f in chapter_files(book):
            text = read_text(f)
            for ln in text.splitlines():
                s = ln.strip()
                if not s:
                    continue
                n = classify_pure_line(s)
                if n:
                    left[n] += 1
                else:
                    _new, names = apply_inline(s)
                    for x in names:
                        left[x] += 1
    print("[核账] 全库重扫各模式剩余次数（应全为 0，人工保留项除外）：")
    for name, n in left.most_common():
        print(f"    {n:>7}  {name}")
        if n:
            problems.append(f"模式 {name} 仍剩 {n} 次")

    if problems:
        print(f"\n🔴 核账不通过（{len(problems)} 条）：")
        for p in problems[:40]:
            print("   -", p)
        return 1
    print("\n✅ 核账通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只读体检，出扫描报告（gate ②）")
    ap.add_argument("--report", default=str(OUT_DIR / "扫描报告.md"))
    ap.add_argument("--dry-run", action="store_true", help="打印将处理的全部文件与动作")
    ap.add_argument("--apply", action="store_true", help="按批准清单真改（需 --force）")
    ap.add_argument("--force", action="store_true", help="apply 的二次确认开关")
    ap.add_argument("--verify", action="store_true", help="按文件系统现状核账")
    ap.add_argument("--decisions", default=str(DECISIONS), help="批准清单（扫描明细.json）")
    args = ap.parse_args()
    if args.scan:
        return cmd_scan(args)
    if args.dry_run:
        return cmd_dry_run(args)
    if args.apply:
        args.decisions_path = Path(args.decisions)
        return cmd_apply(args)
    if args.verify:
        return cmd_verify(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
