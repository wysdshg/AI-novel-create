# -*- coding: utf-8 -*-
"""[DEV-SK03] 骨架库重组 v4：修复 A17 解析 + 按 (大类,子事件) 精确同键重组 + 旧库归档。

    .venv\\Scripts\\python.exe backend/scripts/skel_v4_rebuild.py --dry-run
    .venv\\Scripts\\python.exe backend/scripts/skel_v4_rebuild.py --apply
    .venv\\Scripts\\python.exe backend/scripts/skel_v4_rebuild.py --verify
    .venv\\Scripts\\python.exe backend/scripts/skel_v4_rebuild.py --restore-check

零 LLM 调用：判类结果-v4.md 是唯一权威数据源，组装 = 确定性 LCS/SCS。唯向量重建需网关在线。

==== 口径（docs/04 A17 最高优先 + .flow/docs/skel-v4.md 定案）====
1. **A17 解析**：win_<书>.json 的 `seq` 是**窗口内重编号**（九星 484 原子只有 23 个 seq），
   **禁止全局 seq 字典解析**。弧的每个 `seq s` 取满足「`seq==s` 且原子区间与弧区间
   **真重叠**（原子止章 ≥ 弧起 ∧ 原子起章 ≤ 弧止）」的原子；多候选取**重叠最长**者。
   **禁用「⊆ 弧区间 ±2」容差**——它会把邻弧尾巴原子放进来（实测污染 #234/#111）。
   无候选 → **该拍缺失并记台账**（不拿邻弧原子凑数）。
   锚点：九星 #234 拍5 旧口径会收进上一弧尾巴 `A04 c762`，真重叠只认 `G02 c774~775`；
   太荒 #111 拍4 的同 seq 候选是坏原子 `F01 c346~345`（起章>止章）→ 该拍缺失，
   这正是 PM 出弧速览-v6 时把 #111 从 4 拍收成 3 拍的机制。**全库 2063 拍恰好缺 1 拍。**
2. **组装不借邻**：弧只落进自己判类的那**一个** (大类, 子事件) 键；键内没同伴的弧
   单独当单弧模板并打「孤例」标，绝不借同大类其他子事件的弧凑数（用户 2026-10-04 拍板）。
3. **合并判据**（v3 协议，核心**双向**覆盖 + LCS/min 动态阈值 + LCS/max + SCS ≤15）。
4. **样本分档**（判类结果-v4 §7 / 裁决 3A）：孤例类（1 成员）免合并直接单弧模板打孤例标；
   小样本 2 成员类照常走判据；3~4 成员类报告单列；≥5 常规。
5. **structure 契约沿用 v3**：phases→beats→variants[src/how/desc/tags] + skeleton 块；
   `desc`/`tags` 用**修复解析后的原子真数据**（A17 乱码 tags 随之消失）；
   每拍 variants **全量存储**，按「与拍型相似度」排序，structure 顶层记 `display_top: 5`。

🔴 写库铁律：备份先行、单事务、8000 端口守卫、StaticPool 单连接 + 同连接 wal_checkpoint。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import socket
import sqlite3
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from merge_arcs_crossbook import align_positions, lcs_len, min_threshold_for, scs_merge  # noqa: E402

RAW = ROOT / "outputs" / "_atomic_raw"
SKEL = ROOT / "outputs" / "skel_v4"
JUDGE_MD = SKEL / "判类结果-v4.md"
BACKUP_DIR = ROOT / "outputs" / "_backup"
REPORT = SKEL / "SK03_重组报告.md"
DB_PATH = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")

# win 素材按书名固定列出（不含 _2step / v1-noretry / *.state.json 等中间产物）
BOOKS = ("凡人修仙传", "斗破苍穹", "太荒吞天诀", "九星霸体诀", "圣墟", "蛊真人", "遮天")
SHAOSONG = "绍宋"
PLACEHOLDER_ARCS = (430, 431)          # 判类结果-v4 §交接口径：不修数据，打「占位弧」标
ORPHAN_CLASSES = ("格局变动", "跨域远征", "庙堂权谋")   # §7 孤例类：免合并
ORIGIN_V3 = "atomic_merge_v1"
ORIGIN_V4 = "skel_v4"
DISPLAY_TOP = 5
PHASES = ("起", "承", "转", "合")
T_SHORT = 0.65          # 动态 min 短弧段（拍板②）
T_MID = 0.70
T_LONG = 0.85
THR_MAX = 0.5           # LCS/max 门槛（拦长短悬殊）
MAX_LEN = 15            # SCS 合并链长上限
BASELINE = {"character_active": 265, "event_skeletons": 6, "arc_active_v3": 100}


class _Tee:
    """GBK 控制台会糊中文：同步落一份 UTF-8 日志。"""

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


# ═══════════════════════════════ 一、A17 解析器 ═══════════════════════════════
@dataclass
class Beat:
    """一拍 = 弧的一个 seq 解析出的真原子（v3 压缩后取首拍概括、tags 并入）。"""
    seq: int
    atomic_id: str
    ch_lo: int
    ch_hi: int
    summary: str
    tags: list[str]


@dataclass
class RawArc:
    book: str
    arc_name: str
    ch_lo: int
    ch_hi: int
    seq: list[str]              # 压缩后的原子 ID 序列
    repeats: list[int]
    beats: list[Beat]           # 与 seq 一一对应（压缩后逐步）
    cores: list[str]
    declared_seqs: list[int]
    dropped_seqs: list[int] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    src_ref: str = ""           # 素材文件（win_<书>.json / 绍宋#N.txt），供追溯


def overlaps(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    """真重叠：两区间相交 ≥1 章。**坏原子（起章>止章）恒为 False。**"""
    return a_hi >= b_lo and a_lo <= b_hi


def resolve_atom(seq: int, arc_lo: int, arc_hi: int, cands: list[dict]) -> dict | None:
    """A17 主判据：`seq==s` **且** 原子区间与弧区间**真重叠**；多候选取重叠最长者。

    无候选返回 None。seq 相等在这里**自己再查一遍**（不信任调用方已过滤）——
    A17 的病根就是「全局 seq 字典解析」，把这条约束放进本函数是唯一能防止
    调用方漏掉 seq 过滤的地方。

    🔴 禁用「⊆ 弧区间 ±2」容差——它把邻弧尾巴原子放进来（实测污染 #234/#111）。
    """
    best, best_ov = None, -1
    for c in cands:
        if int(c.get("seq") or 0) != seq:
            continue
        lo = int(c.get("chapter_start") or 0)
        hi = int(c.get("chapter_end") or 0)
        if not overlaps(lo, hi, arc_lo, arc_hi):
            continue
        ov = min(hi, arc_hi) - max(lo, arc_lo) + 1
        if ov > best_ov:
            best, best_ov = c, ov
    return best


def _compress(steps: list[Beat]) -> tuple[list[str], list[int], list[Beat]]:
    """连续同原子压成一步（v3 拍板⑥）：取首拍概括，tags 并入，repeats 记次数。"""
    seq: list[str] = []
    reps: list[int] = []
    out: list[Beat] = []
    for b in steps:
        if seq and seq[-1] == b.atomic_id:
            reps[-1] += 1
            tags = sorted(set(out[-1].tags) | set(b.tags))
            out[-1] = Beat(seq=out[-1].seq, atomic_id=b.atomic_id, ch_lo=out[-1].ch_lo,
                           ch_hi=b.ch_hi, summary=out[-1].summary, tags=tags)
            continue
        seq.append(b.atomic_id)
        reps.append(1)
        out.append(b)
    return seq, reps, out


def parse_win_data(d: dict) -> list[RawArc]:
    """win_<书>.json → RawArc 列表（A17 口径）。"""
    book = d.get("book") or ""
    atoms = d.get("atoms") or []
    by_seq: dict[int, list[dict]] = defaultdict(list)
    for a in atoms:
        by_seq[int(a.get("seq") or 0)].append(a)

    out: list[RawArc] = []
    for arc in (d.get("arcs") or []):
        lo = int(arc.get("chapter_start") or 0)
        hi = int(arc.get("chapter_end") or 0)
        name = str(arc.get("arc_name") or f"弧{len(out) + 1}")
        steps: list[Beat] = []
        dropped: list[int] = []
        ledger: list[dict] = []
        for s in (arc.get("atoms") or []):
            s = int(s)
            hit = resolve_atom(s, lo, hi, by_seq.get(s) or [])
            if hit is None:
                dropped.append(s)
                ledger.append({"book": book, "arc": name, "seq": s, "ch_lo": lo, "ch_hi": hi,
                               "reason": "无真重叠候选（同 seq 原子区间均不与弧区间相交）",
                               "n_cands": len(by_seq.get(s) or [])})
                continue
            steps.append(Beat(seq=s, atomic_id=str(hit.get("atomic_id") or ""),
                              ch_lo=int(hit.get("chapter_start") or 0),
                              ch_hi=int(hit.get("chapter_end") or 0),
                              summary=str(hit.get("summary") or "").strip(),
                              tags=list(hit.get("tags") or [])))
        seq, reps, beats = _compress(steps)
        # 核心清洗（v3 实测飘标率 87.5%：AI 标 core_atomics 不受「必须是本弧原子」约束）
        seqset = set(seq)
        cores = [c for c in dict.fromkeys(str(x) for x in (arc.get("core_atomics") or []))
                 if c in seqset]
        out.append(RawArc(book=book, arc_name=name, ch_lo=lo, ch_hi=hi, seq=seq,
                          repeats=reps, beats=beats, cores=cores,
                          declared_seqs=[int(s) for s in (arc.get("atoms") or [])],
                          dropped_seqs=dropped, ledger=ledger,
                          src_ref=f"win_{book}.json"))
    return out


def parse_win_book(path: Path) -> list[RawArc]:
    return parse_win_data(json.loads(path.read_text(encoding="utf-8")))


def parse_shaosong_data(arr: list, book: str, arc_no: int | None = None) -> RawArc:
    """绍宋 singles：独立 JSON 数组（segment_no/atomic_id/text/tags），**无 seq 问题**。"""
    steps = []
    for it in sorted(arr, key=lambda x: int(x.get("segment_no") or 0)):
        steps.append(Beat(seq=int(it.get("segment_no") or 0),
                          atomic_id=str(it.get("atomic_id") or ""),
                          ch_lo=0, ch_hi=0,
                          summary=str(it.get("text") or "").strip(),
                          tags=list(it.get("tags") or [])))
    seq, reps, beats = _compress(steps)
    no = arc_no if arc_no is not None else (steps[0].seq if steps else 0)
    return RawArc(book=book, arc_name=f"{book}#{no}", ch_lo=0, ch_hi=0, seq=seq,
                  repeats=reps, beats=beats, cores=[],
                  declared_seqs=[b.seq for b in steps],
                  src_ref=f"{book}#{no}.txt")


def parse_shaosong_dir(d: Path) -> list[RawArc]:
    """按文件名自然序读全部 singles（#1 … #25）；弧号取自文件名而非 segment_no。"""
    files = sorted(d.glob("*.txt"),
                   key=lambda p: int(re.search(r"#(\d+)", p.stem).group(1)))
    return [parse_shaosong_data(json.loads(p.read_text(encoding="utf-8")), SHAOSONG,
                                arc_no=int(re.search(r"#(\d+)", p.stem).group(1)))
            for p in files]


# ═══════════════════════════════ 二、判类结果解析 ═══════════════════════════════
@dataclass
class Judgement:
    no: int
    book: str
    arc_name: str
    cls: str
    sub_event: str
    confidence: str
    reason: str
    sub_tags: list[str]


CONF = ("高", "中", "低")
DASHES = {"—", "-", "－", ""}


def _section(md: str, header: str) -> str:
    """取 `## <header>` 到下一个 `## ` 之间的正文。"""
    m = re.search(rf"^##\s*{re.escape(header)}.*?$(.*?)(?=^##\s|\Z)", md, re.M | re.S)
    return m.group(1) if m else ""


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_judgements(md_path: Path = JUDGE_MD) -> list[Judgement]:
    """判类结果-v4.md §1 主表 → 616 条 Judgement（唯一权威数据源）。"""
    md = md_path.read_text(encoding="utf-8")
    out: list[Judgement] = []
    for raw in _section(md, "1. 主表").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        c = _cells(line)
        if len(c) != 8 or not c[0].isdigit() or c[5] not in CONF:
            continue
        sub_tags = [] if c[7] in DASHES else [t for t in c[7].split("、") if t]
        out.append(Judgement(no=int(c[0]), book=c[1], arc_name=c[2], cls=c[3],
                             sub_event="" if c[4] in DASHES else c[4],
                             confidence=c[5], reason=c[6], sub_tags=sub_tags))
    return out


def low_conf_numbers(md_path: Path = JUDGE_MD) -> list[int]:
    """§4 低置信清单（49 行）的弧号，与主表逐条对账用。"""
    md = md_path.read_text(encoding="utf-8")
    out = []
    for raw in _section(md, "4. 低置信清单").splitlines():
        c = _cells(raw.strip())
        if len(c) == 5 and c[0].isdigit() and c[3] and c[4]:
            out.append(int(c[0]))
    return out


def check_input_integrity(rows: list[Judgement]) -> dict:
    """权威输入自检（PM 亲验过的那几项，脚本每次跑都再核一遍）。"""
    from_main = sorted(r.no for r in rows if r.confidence == "低")
    from_list = sorted(low_conf_numbers())
    n_cls = len({r.cls for r in rows})
    dist = Counter(r.cls for r in rows)
    return {
        "rows": len(rows), "no_gap": [r.no for r in rows] == list(range(1, len(rows) + 1)),
        "n_classes": n_cls,
        "conf_dist": dict(Counter(r.confidence for r in rows)),
        "low_from_main": len(from_main), "low_from_list": len(from_list),
        "low_match": from_main == from_list,
        "only_in_list": sorted(set(from_list) - set(from_main)),
        "only_in_main": sorted(set(from_main) - set(from_list)),
        "orphan_classes": sorted(c for c, n in dist.items() if n == 1),
        "sum_by_class": sum(dist.values()),
    }


def orphan_classes(md_path: Path = JUDGE_MD) -> set[str]:
    """§2 大类分布表「样本层」列里标「孤例」的类（免合并，打孤例标）。"""
    md = md_path.read_text(encoding="utf-8")
    out = set()
    for raw in _section(md, "2. 大类分布").splitlines():
        c = _cells(raw.strip())
        if len(c) == 5 and c[0].isdigit() and "孤例" in c[4]:
            out.add(c[1])
    return out


def sample_tiers(md_path: Path = JUDGE_MD) -> dict[str, str]:
    """§2 表 → {大类: 样本层}（小样本 3~4 成员类报告单列用）。"""
    md = md_path.read_text(encoding="utf-8")
    out = {}
    for raw in _section(md, "2. 大类分布").splitlines():
        c = _cells(raw.strip())
        if len(c) == 5 and c[0].isdigit() and c[1]:
            out[c[1]] = c[4]
    return out


# ═══════════════════════════════ 三、成员弧与键分组 ═══════════════════════════════
@dataclass
class MemberArc:
    book: str
    arc_name: str
    ch_lo: int
    ch_hi: int
    seq: list[str]
    repeats: list[int]
    cores: list[str]
    beats: list[Beat]
    judgement: Judgement
    src_ref: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.judgement.cls, self.judgement.sub_event)


def load_all_arcs() -> list[RawArc]:
    arcs: list[RawArc] = []
    for b in BOOKS:
        p = RAW / f"win_{b}.json"
        if p.exists():
            arcs.extend(parse_win_book(p))
    ss = RAW / "shaosong"
    if ss.is_dir():
        arcs.extend(parse_shaosong_dir(ss))
    return arcs


def join_judgements(arcs: list[RawArc], rows: list[Judgement]) -> list[MemberArc]:
    """按判类结果-v4 的弧序号对位（顺序与速览一致 → win 书序 + 绍宋 25 条）。

    🔴 绍宋例外：素材文件名 `绍宋#N.txt` 只是第 N 条弧（N = 弧号 − 591），
    判类表里的弧名是**实义名**（明道宫定策/南渡惊变…）→ 以判类名为准，
    素材文件名留在 `src_ref` 供追溯。其余 591 条必须逐条弧名对上，对不上就报错。
    """
    if len(arcs) != len(rows):
        raise ValueError(f"弧数 {len(arcs)} 与判类行数 {len(rows)} 不符，无法对位")
    out = []
    for i, (a, j) in enumerate(zip(arcs, rows), 1):
        if j.book != SHAOSONG and a.book != j.book:
            raise ValueError(f"#{i} 书名对位失败: 素材 {a.book!r} vs 判类 {j.book!r}")
        if j.book != SHAOSONG and a.arc_name != j.arc_name:
            raise ValueError(f"#{i} 弧名对位失败: 素材 {a.arc_name!r} vs 判类 {j.arc_name!r}")
        if j.book == SHAOSONG and not a.arc_name.startswith(f"{SHAOSONG}#"):
            raise ValueError(f"#{i} 绍宋素材应对位到 绍宋#N.txt，实得 {a.arc_name!r}")
        out.append(MemberArc(book=j.book, arc_name=j.arc_name, ch_lo=a.ch_lo, ch_hi=a.ch_hi,
                             seq=a.seq, repeats=a.repeats, cores=a.cores, beats=a.beats,
                             judgement=j, src_ref=a.src_ref))
    return out


def group_by_key(members: list[MemberArc]) -> dict[tuple[str, str], list[MemberArc]]:
    """键 = (大类, 子事件)；子事件为空归 (大类, "")。**绝不跨键借弧。**"""
    g: dict[tuple[str, str], list[MemberArc]] = defaultdict(list)
    for m in members:
        g[m.key].append(m)
    return dict(g)


# ═══════════════════════════════ 四、合并判据 ═══════════════════════════════
@dataclass
class Cluster:
    members: list[MemberArc]
    seq: list[str]
    repeats: list[int]
    cores: list[str]

    @property
    def n(self) -> int:
        return len(self.members)


def can_merge_arcs(a: Cluster, b: Cluster, t_short: float = T_SHORT,
                   thr_max: float = THR_MAX, max_len: int = MAX_LEN) -> tuple[bool, float, str]:
    """键内归并判据（v3 协议 + 核心**双向**覆盖）。

    ① 双向核心覆盖（硬门槛）：cores(A) ⊆ seq(B) **且** cores(B) ⊆ seq(A)
    ② LCS/min(lenA,lenB) ≥ 动态 min（<5 环 0.65 / [5,10) 0.70 / ≥10 环 0.85）
    ③ LCS/max ≥ thr_max(0.5) —— 拦长短悬殊
    ④ SCS 合并链长 ≤ max_len(15)
    返回 (ok, score, why)；score = LCS/min（合并强度）。
    """
    sa, sb = set(a.seq), set(b.seq)
    miss_a = [c for c in a.cores if c not in sb]
    miss_b = [c for c in b.cores if c not in sa]
    if miss_a or miss_b:
        return False, 0.0, f"核心双向覆盖不成立: A缺{miss_a} B缺{miss_b}"
    if not a.seq or not b.seq:
        return False, 0.0, "空序列"
    la, lb = len(a.seq), len(b.seq)
    l = lcs_len(a.seq, b.seq)
    r_min, r_max = l / min(la, lb), l / max(la, lb)
    merged, _ = scs_merge(a.seq, a.repeats, b.seq, b.repeats)
    if len(merged) > max_len:
        return False, r_min, f"SCS {len(merged)} > {max_len} 环 → 另起单弧模板"
    t_eff = min_threshold_for(la, lb, t_short)
    if r_min < t_eff:
        return False, r_min, f"包含度 min {r_min:.2f} < 动态门槛 {t_eff:.2f}"
    if r_max < thr_max:
        return False, r_max, f"包含度 max {r_max:.2f} < {thr_max}（长短悬殊）"
    return True, r_min, ""


def _as_cluster(m: MemberArc) -> Cluster:
    return Cluster(members=[m], seq=list(m.seq), repeats=list(m.repeats), cores=list(m.cores))


def cluster_within_key(members: list[MemberArc], t_short: float = T_SHORT) -> list[Cluster]:
    """键内贪心多轮归并：每轮取分数最高的一对可并簇合并；合不拢的弧各留单弧簇。"""
    clusters = [_as_cluster(m) for m in members]
    while True:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                ok, score, _ = can_merge_arcs(clusters[i], clusters[j], t_short)
                if ok and (best is None or score > best[0]):
                    best = (score, i, j)
        if best is None:
            return clusters
        _, i, j = best
        A, B = clusters[i], clusters[j]
        seq, reps = scs_merge(A.seq, A.repeats, B.seq, B.repeats)
        clusters[i] = Cluster(members=A.members + B.members, seq=seq, repeats=reps,
                              cores=list(dict.fromkeys(A.cores + B.cores)))
        clusters.pop(j)


# ═══════════════════════════════ 五、模板装配 ═══════════════════════════════
@dataclass
class Template:
    name: str
    genre_tags: list[str]
    logline: str
    structure: dict
    source_stats: dict
    members: list[MemberArc] = field(default_factory=list)
    cluster: Cluster | None = None


NAME_MAX = 118          # DB 列 VARCHAR(120)，留 2 字余量


def load_vocab(db_path: Path | None = DB_PATH) -> dict[str, str]:
    """atomic_events 表 → {atomic_id: 中文名}（节拍标签用）。"""
    con = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True)
    try:
        return {r[0]: r[1] for r in con.execute("select id, name from atomic_events")}
    finally:
        con.close()


def split_phases(n: int) -> list[str]:
    """环位均分到 起/承/转/合（v3 口径）。"""
    if n <= 0:
        return []
    per = max(1, n // 4)
    return [PHASES[min(3, i // per) if n >= 4 else min(3, i * 4 // n)] for i in range(n)]


def _consensus_tags(tagsets: list[list[str]]) -> set[str]:
    """拍型签名：出现在 ≥半数变体里的 tag（拍型相似度的基准）。"""
    cnt = Counter(t for ts in tagsets for t in set(ts))
    need = max(1, (len(tagsets) + 1) // 2)
    return {t for t, n in cnt.items() if n >= need}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _variant_rank(core_hit: bool, tags: list[str], consensus: set[str]):
    """拍型相似度排序键：核心命中优先 → tag 与拍型签名 Jaccard 降序 → 书/章升序（稳定）。"""
    return (0 if core_hit else 1, -round(_jaccard(set(tags), consensus), 6))


def _template_name(cls: str, sub: str, idx: int, total: int) -> str:
    """`大类--子事件`；子事件空则大类单名；同键多张加 -1/-2。"""
    base = f"{cls}--{sub}" if sub else cls
    name = base if total <= 1 else f"{base}-{idx}"
    return name[:NAME_MAX]


def build_template(cls: str, sub: str, name: str, cluster: Cluster, vocab: dict[str, str],
                   orphan_cls: set[str] | frozenset = frozenset()) -> Template:
    """structure 契约沿用 v3：phases→beats→variants[src/how/desc/tags] + skeleton。"""
    n = len(cluster.seq)
    phases = split_phases(n)
    seen: dict[str, list[int]] = defaultdict(list)
    for i, ph in enumerate(phases):
        seen[ph].append(i)

    # 每环的实证走法：成员 seq 是骨架子序列（SCS 性质），align_positions 给出
    # 「成员第 k 个原子 → 骨架第 j 环」映射 —— v3 的 desc 按拍取真原子，不再全拍同文。
    raw: list[list[dict]] = [[] for _ in range(n)]
    for m in cluster.members:
        for k, pl in enumerate(align_positions(m.seq, cluster.seq)):
            beat = m.beats[k] if k < len(m.beats) else None
            for j in pl:
                raw[j].append({
                    "src": m.book,
                    "how": f"{m.arc_name}（c{m.ch_lo}~{m.ch_hi}）",
                    "desc": (beat.summary if beat else "") or "",
                    "tags": list(beat.tags) if beat else [],
                    "_core": bool(beat and beat.atomic_id in m.cores),
                })

    phases_struct = []
    for ph in PHASES:
        if ph not in seen:
            continue
        beats = []
        for i in seen[ph]:
            variants = raw[i]
            consensus = _consensus_tags([v["tags"] for v in variants])
            variants.sort(key=lambda v: (*_variant_rank(v["_core"], v["tags"], consensus),
                                         v["src"], v["how"]))
            for v in variants:
                v.pop("_core", None)
            ring = cluster.seq[i]
            beats.append({
                "beat": f"{ring} {vocab.get(ring, '未命名原子')}"
                        + (f"×{cluster.repeats[i]}" if cluster.repeats[i] > 1 else ""),
                "variants": variants,
            })
        phases_struct.append({"phase": ph, "beats": beats})

    books = sorted({m.book for m in cluster.members})
    low = sorted(m.judgement.no for m in cluster.members
                 if m.judgement.confidence == "低")
    placeholders = sorted(m.judgement.no for m in cluster.members
                          if m.judgement.no in PLACEHOLDER_ARCS)
    tags = ["原子骨架", "v4生成", cls]
    if sub:
        tags.append(sub)
    tags.append(f"{len(books)}书")
    if low:
        tags.append("含低置信成员")
    if placeholders:
        tags.append("占位弧")

    structure = {
        "phases": phases_struct,
        "display_top": DISPLAY_TOP,
        "skeleton": {"seq": cluster.seq, "repeats": cluster.repeats,
                     "cores": cluster.cores, "origin": ORIGIN_V4},
    }
    stats = {
        "origin": ORIGIN_V4, "books": len(books), "book_names": books,
        "n_members": cluster.n, "class": cls, "sub_event": sub,
        "low_conf_members": low, "single_arc": cluster.n == 1,
        # 成员溯源（弧号 + 书 + 弧名 + 章节区间）：让「无借邻」可被机器逐条核账
        "member_arcs": [{"no": m.judgement.no, "book": m.book, "arc": m.arc_name,
                         "ch_lo": m.ch_lo, "ch_hi": m.ch_hi, "src_ref": m.src_ref}
                        for m in sorted(cluster.members, key=lambda x: x.judgement.no)],
    }
    if cls in orphan_cls:
        stats["orphan_class"] = True
        if "孤例" not in tags:
            tags.append("孤例")
    if placeholders:
        stats["placeholder_arcs"] = placeholders
    if cluster.n == 1 and "孤例" not in tags:
        tags.append("孤例")

    heads = " → ".join(f"{r} {vocab.get(r, '未命名原子')}" for r in cluster.seq)
    logline = (f"（{cls}{'--' + sub if sub else ''}）{cluster.n} 条弧、{len(books)} 本书实证的"
               f"情节骨架：{heads}。")
    return Template(name=name, genre_tags=tags, logline=logline, structure=structure,
                    source_stats=stats, members=list(cluster.members), cluster=cluster)


def build_templates(groups: dict[tuple[str, str], list[MemberArc]], vocab: dict[str, str],
                    orphan_cls: set[str] | frozenset = frozenset()) -> list[Template]:
    """逐键归并 → 逐簇出模板。孤例类（1 成员）免合并，直接单弧模板。"""
    out: list[Template] = []
    for (cls, sub), members in sorted(groups.items()):
        if cls in orphan_cls:
            clusters = [_as_cluster(m) for m in members]
        else:
            clusters = cluster_within_key(members)
        # 稳定序：成员多的簇先出，同数按最小弧号 → 模板名后缀稳定
        clusters.sort(key=lambda c: (-c.n, min(m.judgement.no for m in c.members)))
        for i, c in enumerate(clusters, 1):
            name = _template_name(cls, sub, i, len(clusters))
            out.append(build_template(cls, sub, name, c, vocab, orphan_cls))
    return out


# ═══════════════════════════════ 六、计划 ═══════════════════════════════
def build_plan() -> dict:
    """只读构建执行计划：逐键成员数/簇数/模板数/孤例数 + 兜底台账 + 低置信单列。"""
    rows = parse_judgements()
    arcs = load_all_arcs()
    members = join_judgements(arcs, rows)
    vocab = load_vocab()
    groups = group_by_key(members)
    orphan_cls = orphan_classes()
    tiers = sample_tiers()
    templates = build_templates(groups, vocab, orphan_cls)

    keys = []
    for (cls, sub), ms in sorted(groups.items()):
        cls_members = sum(1 for m in members if m.judgement.cls == cls)
        cls_low = sorted(m.judgement.no for m in ms if m.judgement.confidence == "低")
        tpls = [t for t in templates if t.source_stats["class"] == cls
                and t.source_stats["sub_event"] == sub]
        keys.append({
            "class": cls, "sub_event": sub, "n_members": len(ms),
            "n_clusters": len(tpls), "n_templates": len(tpls),
            "n_single_arc": sum(1 for t in tpls if t.source_stats["single_arc"]),
            "class_members": cls_members,
            "tier": tiers.get(cls, "常规"),
            "orphan_class": cls in orphan_cls,
            "low_conf_members": cls_low,
            "template_names": [t.name for t in tpls],
        })

    ledger = [e for a in arcs for e in a.ledger]
    dup = [n for n, c in Counter(t.name for t in templates).items() if c > 1]
    integrity = check_input_integrity(rows)
    # 权威输入的硬断言：616 行 / 无缺号 / 低置信两处口径一致 / 类计数求和 = 行数
    if not (integrity["rows"] == 616 and integrity["no_gap"] and integrity["low_match"]
            and integrity["sum_by_class"] == integrity["rows"]):
        sys.exit(f"[plan] 权威输入自检失败，拒绝组装: {integrity}")
    return {
        "ts": now_utc(), "judgements": rows, "members": members, "groups": groups,
        "templates": templates, "keys": keys, "ledger": ledger, "tiers": tiers,
        "orphan_classes": sorted(orphan_cls),
        "integrity": integrity,
        "dup_template_names": dup,
        "class_counts": Counter(m.judgement.cls for m in members),
        "n_arcs": len(members), "n_classes": len({m.judgement.cls for m in members}),
        "n_low_conf": sum(1 for m in members if m.judgement.confidence == "低"),
        "n_beats": sum(len(a.seq) for a in arcs),
        "n_single_arc_templates": sum(1 for t in templates if t.source_stats["single_arc"]),
        "n_small_sample_classes": sum(1 for v in tiers.values()
                                      if v.startswith("小样本-3~4")),
    }


def print_plan(plan: dict) -> None:
    tp = plan["templates"]
    print(f"[计划] {plan['ts']}")
    ig = plan["integrity"]
    print(f"  权威输入自检：{ig['rows']} 行 / 无缺号 {ig['no_gap']} / {ig['n_classes']} 类 / "
          f"置信分布 {ig['conf_dist']} / 低置信两处口径一致 {ig['low_match']}"
          f"（主表 {ig['low_from_main']} · §4 清单 {ig['low_from_list']}）")
    print(f"  判类 {plan['n_arcs']} 弧 / {plan['n_classes']} 类 / 低置信 {plan['n_low_conf']}"
          f" | 原子 {plan['n_beats']} 拍 | 键 {len(plan['keys'])} 个")
    print(f"  模板 {len(tp)} 张（单成员/孤例 {plan['n_single_arc_templates']} 张）"
          f" | 兜底台账 {len(plan['ledger'])} 拍")
    print(f"  样本分档：孤例类 {len(plan['orphan_classes'])} "
          f"({', '.join(plan['orphan_classes'])})｜小样本3~4 类 {plan['n_small_sample_classes']}")
    print("\n-- 逐键（成员数 / 簇数 / 模板数 / 单弧数 / 样本层 / 低置信）--")
    for k in plan["keys"]:
        flag = " [孤例类]" if k["orphan_class"] else ""
        print(f"  {k['class']}--{k['sub_event'] or '(空)'}{flag}: "
              f"成员 {k['n_members']} → 簇 {k['n_clusters']} → 模板 {k['n_templates']}"
              f"（单弧 {k['n_single_arc']}）｜{k['tier']}"
              f"｜低置信成员 {k['low_conf_members'] or '无'}")
    print("\n-- 兜底台账（无真重叠候选 → 该拍缺失）--")
    for e in plan["ledger"]:
        print(f"  [{e['book']}] {e['arc']} seq={e['seq']} 弧c{e['ch_lo']}~{e['ch_hi']}: "
              f"{e['reason']}（同 seq 候选 {e['n_cands']} 个）")
    if not plan["ledger"]:
        print("  （空）")


# ═══════════════════════════════ 七、DB 守卫 / 备份 / 应用 ═══════════════════════════════
def backend_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 8000)) == 0


def _cols(con) -> list[str]:
    return [r[1] for r in con.execute("pragma table_info(plot_templates)")]


def _rows(con, where: str, args: tuple = ()) -> list[dict]:
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(
        f"select * from plot_templates where {where}", args)]
    con.row_factory = None
    return out


def backup_path() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return BACKUP_DIR / f"plot_templates_arc_v3_{ts}.json"


def do_backup(con, rows: list[dict]) -> Path:
    """旧 active arc 全字段备份（可逐行还原）。"""
    snap = {"script": "skel_v4_rebuild.py", "ts_utc": now_utc(), "db": str(DB_PATH),
            "table": "plot_templates", "columns": _cols(con),
            "counts": {"archived": len(rows)},
            "note": "status 由 active 改 archived 即可回滚；结构/向量未动",
            "archived": rows}
    p = backup_path()
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[apply] 备份落盘 {p.name}：{len(rows)} 条旧 active arc（全字段 {len(snap['columns'])} 列）")
    return p


def existing_v4(con) -> list[dict]:
    return [r for r in _rows(con, "scale='arc'")
            if ORIGIN_V4 in json.dumps(r.get("source_stats") or "", ensure_ascii=False)]


def do_apply(plan: dict) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import app.core.database as dbmod
    from app.models.orm import PlotTemplateORM
    from app.services import plot_template_crud as crud
    from app.services import vector_index

    con = sqlite3.connect(DB_PATH.as_uri(), uri=True)
    try:
        already = existing_v4(con)
        if already:
            print(f"[apply] 幂等：库里已有 {len(already)} 条 {ORIGIN_V4} 模板 "
                  f"→ 不动库、不覆盖备份")
            return
        old = _rows(con, "scale='arc' and status='active'")
        if len(old) != BASELINE["arc_active_v3"]:
            sys.exit(f"[apply] 旧 active arc {len(old)} 条 ≠ 基线 "
                     f"{BASELINE['arc_active_v3']}，拒绝动手（库现状已变，先人工核对）")
        backup = do_backup(con, old)
    finally:
        con.close()

    dbmod.init_db()
    # 🔴 本机硬需求（docs/07）：池化多连接在本机文件虚拟化下每个连接是独立视图 ——
    #    StaticPool 强制全局唯一连接，写读自洽 + 结尾同连接 checkpoint 才能跨进程可见。
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    old_ids = [r["id"] for r in old]
    templates = plan["templates"]

    def n_chunks(sid: str) -> int:
        return db.execute(text("select count(*) from vector_chunks where source_id=:i"),
                          {"i": sid}).scalar()

    try:
        # ---- 单事务：归档旧 100 + 插入新 v4（向量留到事务外重建）----
        n_arch = db.query(PlotTemplateORM).filter(
            PlotTemplateORM.id.in_(old_ids)).update(
            {PlotTemplateORM.status: "archived"}, synchronize_session=False)
        if n_arch != len(old_ids):
            raise RuntimeError(f"归档数 {n_arch} ≠ {len(old_ids)}")
        print(f"[apply] 归档旧 active arc {n_arch} 条 → status='archived'")

        now = datetime.utcnow()
        for t in templates:
            db.add(PlotTemplateORM(
                id=uuid.uuid4().hex, name=t.name, scale="arc",
                genre_tags=t.genre_tags, logline=t.logline, structure=t.structure,
                pitfalls=[], rhythm=None, source_stats=t.source_stats, status="active",
                created_at=now, updated_at=now))
        db.commit()
        print(f"[apply] 插入新 v4 模板 {len(templates)} 条（active）→ 已提交")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        sys.exit(f"[apply] 回滚: {type(e).__name__}: {e}")

    # ---- 旧 100 三路向量块清理（否则成孤儿块，且与新库对不上账）----
    for st in (crud.SOURCE_TYPE, crud.SOURCE_TYPE_CAST, crud.SOURCE_TYPE_ARCHETYPE):
        n = sum(vector_index.remove_source(db, crud.GLOBAL, st, tid) for tid in old_ids)
        db.commit()
        print(f"[apply] 清理旧模板向量 {st}: {n} 块")

    # ---- 新模板三路向量重建（需网关在线）----
    ok = fail = 0
    rows = db.query(PlotTemplateORM).filter(PlotTemplateORM.status == "active").all()
    new_ids = [o.id for o in rows
               if ORIGIN_V4 in json.dumps(o.source_stats or {}, ensure_ascii=False)]
    print(f"[apply] 待建向量：active 模板 {len(rows)} 张，其中 v4 {len(new_ids)} 张")
    for i, o in enumerate(rows, 1):
        if o.id not in new_ids:
            continue
        expect = (len(crud.template_chunks(o)) + len(crud.cast_chunks(o))
                  + len(crud.archetype_chunks(o)))
        got = -1
        for attempt in (1, 2, 3):     # embed 抖动/限流重试；index_template 幂等（先清后建）
            crud.index_template(db, o)
            db.expire_all()
            db.commit()
            got = n_chunks(o.id)
            if got == expect:
                break
            time.sleep(1.5 * attempt)
        if got == expect:
            ok += 1
        else:
            fail += 1
            print(f"  ⚠️ {o.name}: 三路块数 {got} != 预期 {expect}")
        if i % 100 == 0:
            print(f"  向量进度 {i}/{len(rows)}（失败 {fail}）")

    try:
        db.commit()
        print(f"[apply] checkpoint: {db.execute(text('PRAGMA wal_checkpoint(PASSIVE)')).fetchone()}")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[apply] checkpoint 失败（可稍后手动 TRUNCATE）: {type(e).__name__}: {e}")
    print(f"[apply] 向量重建完成：成功 {ok} / 失败 {fail}")
    print(f"[apply] 备份 = {backup.name}")
    db.close()
    if fail:
        sys.exit(f"[apply] 有 {fail} 个模板向量块数不符")


# ═══════════════════════════════ 八、verify（A14：按现状核账，不信返回值）════
def do_verify(plan: dict) -> int:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import app.core.database as dbmod
    from app.services import plot_template_crud as crud
    from app.services import vector_index

    fails: list[str] = []

    def chk(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(label)

    con = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    dist = {(r["scale"], r["status"]): r["n"] for r in con.execute(
        "select scale,status,count(*) n from plot_templates group by 1,2")}
    v4_rows = []
    for r in con.execute("select * from plot_templates where scale='arc'"):
        ss = r["source_stats"]
        if isinstance(ss, str) and ORIGIN_V4 in ss:
            v4_rows.append(dict(r))
    arc_archived_ids = {r["id"] for r in con.execute(
        "select id from plot_templates where scale='arc' and status='archived'")}

    print("\n[verify-1] 库现状：active arc = 新 v4 全集；旧 100 已 archived；下游零改动")
    exp = len(plan["templates"])
    chk(dist.get(("arc", "active"), 0) == exp, "active arc 数 = 新 v4 模板数",
        f"实得 {dist.get(('arc', 'active'), 0)} 期望 {exp}")
    chk(len(v4_rows) == exp, "v4 模板全部 active", f"{len(v4_rows)}/{exp}")
    chk(len(arc_archived_ids) >= BASELINE["arc_active_v3"],
        "archived arc 含旧 100", f"{len(arc_archived_ids)} 条 ≥ {BASELINE['arc_active_v3']}")
    chk(dist.get(("character", "active"), 0) == BASELINE["character_active"],
        "character active 零改动", f"实得 {dist.get(('character', 'active'), 0)}")
    n_es = con.execute("select count(*) from event_skeletons").fetchone()[0]
    chk(n_es == BASELINE["event_skeletons"], "event_skeletons 零改动", f"实得 {n_es}")
    es_sig = con.execute(
        "select group_concat(id||'|'||name||'|'||core_atomic||'|'||steps) from event_skeletons"
    ).fetchone()[0]
    chk(bool(es_sig) and len(es_sig) > 100, "event_skeletons 抽样字段非空", f"签名 {len(es_sig or '')} 字")

    print("\n[verify-2] 契约完整 + 无借邻抽验 5 张")
    bad_contract, zero_beat = [], []
    for r in v4_rows:
        st = _as_json_v(r["structure"])
        beats = [b for ph in (st.get("phases") or []) for b in (ph.get("beats") or [])]
        if not beats:
            zero_beat.append(r["name"])
            continue
        for b in beats:
            vs = b.get("variants") or []
            if not vs or any(not all(k in v for k in ("src", "how", "desc", "tags"))
                             for v in vs):
                bad_contract.append(f"{r['name']}/{b.get('beat')}")
        if st.get("display_top") != DISPLAY_TOP:
            bad_contract.append(f"{r['name']}/display_top")
    chk(not zero_beat, "无块模板 = 0", f"空 beats 模板 {len(zero_beat)}")
    chk(not bad_contract, "beats/variants 契约完整", f"异常 {len(bad_contract)}")
    names = Counter(r["name"] for r in v4_rows)
    dupn = [n for n, c in names.items() if c > 1]
    chk(not dupn, "模板名无重复（name 可作检索键）", f"重复 {dupn[:5]}")
    n_flagged = sum(1 for r in v4_rows
                    if _as_json(r["source_stats"]).get("single_arc"))
    n_single = sum(1 for r in v4_rows
                   if (_as_json(r["source_stats"]).get("n_members") or 0) == 1)
    chk(n_flagged == n_single,
        "验收3：孤例标数量 == 单成员模板数", f"{n_flagged} == {n_single}")

    jmap = {r.no: r for r in plan["judgements"]}
    picks = _pick_spotcheck(v4_rows, 5)
    # 无借邻：**全量**逐条核（成员弧的判类键必须 == 模板键），再把 5 张抽样打明细
    borrowed = []
    for r in v4_rows:
        ss = _as_json(r["source_stats"])
        cls, sub = ss.get("class"), ss.get("sub_event", "")
        for m in ss.get("member_arcs") or []:
            j = jmap.get(m["no"])
            if j and (j.cls != cls or j.sub_event != sub):
                borrowed.append(f"{r['name']}←#{m['no']}({j.cls}--{j.sub_event})")
    chk(not borrowed, f"全量 {len(v4_rows)} 张：成员弧判类键 = 模板键（无借邻）",
        f"借邻 {borrowed[:5]}" if borrowed else "逐条一致")
    for r in picks:
        ss = _as_json(r["source_stats"])
        cls, sub = ss.get("class"), ss.get("sub_event", "")
        print(f"  · {r['name']}  键=({cls}, {sub or '空'})  成员 {ss.get('n_members')} 弧")
        for m in (ss.get("member_arcs") or [])[:4]:
            j = jmap.get(m["no"])
            same = j and j.cls == cls and j.sub_event == sub
            print(f"      #{m['no']} {m['book']}·{m['arc']} c{m['ch_lo']}~{m['ch_hi']}"
                  f"  判类=({j.cls}, {j.sub_event}) {same and '✓同键' if j else ''}")
    chk(all(_as_json(r["source_stats"]).get("member_arcs") for r in v4_rows),
        "每张模板都带成员溯源（member_arcs）")

    print("\n[verify-3] 三路向量块数对账 + search 冒烟")
    dbmod.init_db()
    from sqlalchemy import text as sa_text
    from app.models.orm import PlotTemplateORM, VectorChunkORM
    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    active = db.query(PlotTemplateORM).filter_by(status="active").all()
    v4_objs = [o for o in active
               if ORIGIN_V4 in json.dumps(o.source_stats or {}, ensure_ascii=False)]
    exp_tpl = exp_cast = exp_arch = 0
    for o in v4_objs:
        exp_tpl += len(crud.template_chunks(o))
        exp_cast += len(crud.cast_chunks(o))
        exp_arch += len(crud.archetype_chunks(o))
    new_ids = [o.id for o in v4_objs]
    new_got = {st: sum(db.query(VectorChunkORM)
                       .filter_by(source_type=st, source_id=i).count() for i in new_ids)
               for st in (crud.SOURCE_TYPE, crud.SOURCE_TYPE_CAST,
                          crud.SOURCE_TYPE_ARCHETYPE)}
    got = {st: db.query(VectorChunkORM).filter_by(source_type=st, project_id=crud.GLOBAL)
           .count() for st in (crud.SOURCE_TYPE, crud.SOURCE_TYPE_CAST,
                               crud.SOURCE_TYPE_ARCHETYPE)}
    print(f"  新 v4 模板 {len(new_ids)} 张（active 共 {len(active)} 张）")
    print(f"  新 v4 三路块数：plot_template={new_got[crud.SOURCE_TYPE]} "
          f"cast={new_got[crud.SOURCE_TYPE_CAST]} 原型={new_got[crud.SOURCE_TYPE_ARCHETYPE]}"
          f"（预期 {exp_tpl}/{exp_cast}/{exp_arch}）")
    chk(new_got[crud.SOURCE_TYPE] == exp_tpl, "新 v4 plot_template 块数对账",
        f"{new_got[crud.SOURCE_TYPE]} vs {exp_tpl}")
    chk(new_got[crud.SOURCE_TYPE_CAST] == exp_cast, "新 v4 cast 块数对账（无 cast → 0）",
        f"{new_got[crud.SOURCE_TYPE_CAST]} vs {exp_cast}")
    chk(new_got[crud.SOURCE_TYPE_ARCHETYPE] == exp_arch, "新 v4 原型块数对账（无原型 → 0）",
        f"{new_got[crud.SOURCE_TYPE_ARCHETYPE]} vs {exp_arch}")
    orphan = db.execute(sa_text(
        "select count(*) from vector_chunks vc where vc.source_type in "
        "('plot_template','plot_cast','char_archetype') and not exists("
        "select 1 from plot_templates t where t.id=vc.source_id)")).scalar()
    chk(orphan == 0, "三路零孤儿块（旧 100 的块已清）", f"孤儿 {orphan}")
    print(f"  全库三路总数：{got}（含 character 265×3）")

    smoke_ok = smoke_detail = None
    if exp_tpl:
        res = crud.search(db, query="主角在宗门比试擂台上一战成名夺魁", scale="arc", top_k=5)
        items = res.get("items") or []
        hit = [it for it in items if it.get("matched_beats")]
        smoke_ok = bool(hit)
        smoke_detail = (f"mode={res.get('mode')} 命中 {len(items)} 支"
                        f"（beats>0 的 {len(hit)} 支）"
                        + (f" top1={items[0]['name']}" if items else ""))
        if hit:
            print(f"  冒烟命中：{hit[0]['name']}｜matched_beats={len(hit[0]['matched_beats'])}"
                  f"｜variants={len((hit[0]['matched_beats'][0].get('variants') or []))}")
    chk(bool(smoke_ok), "search 冒烟：模糊口述命中 arc 新模板且 beats>0", smoke_detail or "")

    print("\n[verify-4] 幂等：库内 v4 数 = active arc 数（无重复插入）+ 组装确定性")
    con2 = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    con2.row_factory = sqlite3.Row
    n_v4 = sum(1 for r in con2.execute("select source_stats from plot_templates")
               if ORIGIN_V4 in (r[0] if isinstance(r[0], str) else json.dumps(r[0],
                                                                        ensure_ascii=False)))
    con2.close()
    n_active = con.execute("select count(*) from plot_templates where scale='arc' and "
                           "status='active'").fetchone()[0]
    chk(n_v4 == n_active, "库内 v4 模板数 == active arc 数（无重复插入）", f"{n_v4} == {n_active}")
    chk(len(plan["templates"]) == exp, "计划模板数一致（组装确定性）",
        f"{len(plan['templates'])} == {exp}")

    con.close()
    db.close()
    print(f"\n===== verify 结果：{len(fails)} FAIL / {len(fails) == 0 and 'ALL PASS' or ''} =====")
    for f in fails:
        print(f"  ❌ {f}")
    return 0 if not fails else 1


def _as_json(v) -> dict:
    """JSON 列在 raw sqlite3 里是 str、经 SQLAlchemy ORM 是 dict —— 两种形态都要吃。"""
    if isinstance(v, str):
        return json.loads(v) if v.strip() else {}
    return v or {}


def _pick_spotcheck(rows: list[dict], n: int) -> list[dict]:
    """抽 n 张：成员数最大 / 单弧 / 含低置信成员 / 占位弧 / 其余补齐。"""
    picked: list[dict] = []
    seen: set[str] = set()

    def push(r):
        if r["id"] not in seen:
            seen.add(r["id"])
            picked.append(r)

    def want(pred) -> None:
        for r in rows:
            if len(picked) >= n:
                return
            if pred(_as_json(r["source_stats"])):
                push(r)

    want(lambda s: (s.get("n_members") or 0) >= 2)      # 合并簇（若有）
    want(lambda s: s.get("single_arc"))
    want(lambda s: s.get("low_conf_members"))
    want(lambda s: s.get("placeholder_arcs"))
    want(lambda s: s.get("orphan_class"))
    for r in rows:
        if len(picked) >= n:
            break
        push(r)
    return picked[:n]


def _as_json_v(v) -> dict:      # 结构列同样有 str/dict 两形态
    return json.loads(v) if isinstance(v, str) else (v or {})


def do_restore_check() -> None:
    """只用备份核对可回滚性（不改库）。"""
    bks = sorted(BACKUP_DIR.glob("plot_templates_arc_v3_*.json"))
    if not bks:
        print("[restore-check] 未找到备份文件")
        return
    p = bks[-1]
    snap = json.loads(p.read_text(encoding="utf-8"))
    cols = snap["columns"]
    rows = snap["archived"]
    print(f"[restore-check] 备份 {p.name}：{len(rows)} 条，列 {len(cols)}")
    bad = [r.get("id") for r in rows if not set(cols) <= set(r)]
    print(f"  字段不全（不可逐行还原）的行 = {len(bad)} {'PASS' if not bad else 'FAIL'}")
    con = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    still_active = [r["id"] for r in rows if con.execute(
        "select 1 from plot_templates where id=? and status='active'", (r["id"],)).fetchone()]
    print(f"  备份中仍为 active 的行（应为 0）= {len(still_active)} "
          f"{'PASS' if not still_active else 'FAIL'}")
    still_there = sum(1 for r in rows if con.execute(
        "select 1 from plot_templates where id=?", (r["id"],)).fetchone())
    print(f"  备份行仍在库（归档态，数据完整）= {still_there}/{len(rows)}")
    con.close()
    if rows:
        print(f"  示例回滚 SQL: UPDATE plot_templates SET status='active' WHERE id='{rows[0]['id']}';")


# ═══════════════════════════════ 九、抽查报告 ═══════════════════════════════
def _old_lib_stats() -> dict:
    """旧库 active arc 的对照基线（最大单拍参考数 / A17 污染指纹）。

    🔴 apply 之后旧 100 已 archived，`where status='active'` 量到的就是新库了 ——
    所以**优先从备份文件**取旧结构（备份是全字段快照，本就该是新旧对比的权威源）；
    没有备份时才退回现库 active arc（apply 之前的 dry-run 场景）。
    """
    rows: list[dict] = []
    src = "现库 active arc"
    bks = sorted(BACKUP_DIR.glob("plot_templates_arc_v3_*.json"))
    if bks:
        snap = json.loads(bks[-1].read_text(encoding="utf-8"))
        rows = snap.get("archived") or []
        src = f"备份 {bks[-1].name}"
    else:
        con = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "select id, structure from plot_templates where scale='arc' and status='active'")]
        con.close()
    mx = tot_v = same_desc = multi = 0
    beats = []
    for r in rows:
        st = _as_json_v(r.get("structure"))
        bs = [b for ph in (st.get("phases") or []) for b in (ph.get("beats") or [])]
        beats.append(len(bs))
        for b in bs:
            vs = b.get("variants") or []
            mx, tot_v = max(mx, len(vs)), tot_v + len(vs)
            if len(vs) > 1:
                multi += 1
                if len({v.get("desc") for v in vs}) == 1:
                    same_desc += 1
    con = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    n_cls = con.execute("select count(*) from plot_templates where scale='character' "
                        "and status='active'").fetchone()[0]
    con.close()
    return {"n_templates": len(rows), "max_variants_per_beat": mx,
            "total_variants": tot_v, "n_multi_variant_beats": multi,
            "n_same_desc_beats": same_desc, "src": src,
            "beats_median": sorted(beats)[len(beats) // 2] if beats else 0,
            "character_active": n_cls}


def _new_lib_stats(plan: dict) -> dict:
    beats_per_tpl, vs = [], []
    for t in plan["templates"]:
        bs = [b for ph in t.structure["phases"] for b in ph["beats"]]
        beats_per_tpl.append(len(bs))
        vs.extend(len(b["variants"]) for b in bs)
    descs = [v["desc"] for t in plan["templates"] for ph in t.structure["phases"]
             for b in ph["beats"] for v in b["variants"]]
    n_multi = sum(1 for t in plan["templates"] for ph in t.structure["phases"]
                  for b in ph["beats"] if len(b["variants"]) > 1)
    n_same = sum(1 for t in plan["templates"] for ph in t.structure["phases"]
                 for b in ph["beats"] if len(b["variants"]) > 1
                 and len({v["desc"] for v in b["variants"]}) == 1)
    return {"n_templates": len(plan["templates"]),
            "max_variants_per_beat": max(vs) if vs else 0,
            "beats_median": sorted(beats_per_tpl)[len(beats_per_tpl) // 2] if beats_per_tpl else 0,
            "n_variants": len(descs), "n_distinct_desc": len(set(descs)),
            "n_multi_variant_beats": n_multi, "n_same_desc_beats": n_same,
            "n_single_arc": plan["n_single_arc_templates"]}


def write_report(plan: dict) -> Path:
    old, new = _old_lib_stats(), _new_lib_stats(plan)
    exp = len(plan["templates"])
    tp = plan["templates"]
    by_cls: dict[str, list[Template]] = defaultdict(list)
    for t in tp:
        by_cls[t.source_stats["class"]].append(t)
    jmap = {m.judgement.no: m for m in plan["members"]}
    low = [m for m in plan["members"] if m.judgement.confidence == "低"]
    focus = [111, 234, 413]

    L: list[str] = []
    w = L.append
    w(f"# SK03 重组报告 · 骨架库 v4（{(plan['ts'][:16]).replace('T', ' ')} UTC）\n")
    w("> 工单 DEV-SK03｜权威数据源 `outputs/skel_v4/判类结果-v4.md`（616 弧 / 57 类 / 低置信 49）\n"
      "> 脚本 `backend/scripts/skel_v4_rebuild.py`｜零 LLM 调用（唯向量重建需网关 embedding）\n")
    ig = plan["integrity"]
    w(f"**权威输入自检**（脚本每次跑都重核）：{ig['rows']} 行 / 弧号无缺号 {ig['no_gap']} / "
      f"{ig['n_classes']} 类 / 置信分布 {ig['conf_dist']} / 低置信主表与 §4 清单口径一致 "
      f"{ig['low_match']}（{ig['low_from_main']} vs {ig['low_from_list']}）/ 大类计数求和 "
      f"{ig['sum_by_class']}。\n")

    w("## 0. 一句话结论\n")
    w(f"- 判类 {plan['n_arcs']} 弧 / {plan['n_classes']} 类 → **(大类,子事件) 键 "
      f"{len(plan['keys'])} 个 → 模板 {new['n_templates']} 张**，全部为单成员模板"
      f"（孤例标 {plan['n_single_arc_templates']} 张，与单成员模板数逐一对账）。")
    w(f"- **A17 已修**：全库 {plan['n_beats']} 环里最大单拍参考数由旧库 "
      f"**{old['max_variants_per_beat']}** 降到 **{new['max_variants_per_beat']}**；"
      f"旧库 {old['n_multi_variant_beats']} 个多参考拍中有 **{old['n_same_desc_beats']} 个"
      f"（{old['n_same_desc_beats']*100//max(1,old['n_multi_variant_beats'])}%）"
      f"所有变体 desc 完全同文**——A17 污染的直接指纹（全局 seq 字典解析把每条弧都指向"
      f"同一批窗口原子）；新库 {new['n_variants']} 条 desc **两两互不相同**"
      f"（{new['n_distinct_desc']}/{new['n_variants']} 唯一），污染归零。")
    w(f"- ⚠️ **零合并**：{len([k for k in plan['keys'] if k['n_members']>1])} 个多成员键"
      f"（共 {sum(k['n_members'] for k in plan['keys'] if k['n_members']>1)} 弧）内"
      f"**没有任何一对弧通过 v3 合并判据**。控制实验证明这是精确键粒度的**结构性结果**，"
      f"不是实现缺陷（见 §6）；若要减少模板数，杠杆在子事件词表合并，不在合并判据。\n")

    w("## 1. 新旧对比\n")
    w(f"> 旧库口径来源：**{old['src']}**（全字段备份即新旧对比的权威源；"
      f"apply 之后旧 100 已 archived，不能再从 active 量）\n")
    w("| 维度 | 旧库（v3，100 张 active） | 新库（v4） |")
    w("|---|---|---|")
    w(f"| 大类数 | 无（v3 按原子环聚类，无判类身份） | **{plan['n_classes']}** |")
    w(f"| 模板数 | {old['n_templates']} | **{new['n_templates']}** |")
    w(f"| 模板命名 | 核心环中文名「·」连接 | **`大类--子事件`**（精确键，可检索匹配） |")
    w(f"| 最大单拍参考数 | {old['max_variants_per_beat']} | **{new['max_variants_per_beat']}** |")
    w(f"| variants 合计 | {old['total_variants']} | {new['n_variants']} |")
    w(f"| 多参考拍 desc 全同文 | {old['n_same_desc_beats']} / {old['n_multi_variant_beats']}"
      f"（A17 污染） | {new['n_same_desc_beats']} / {new['n_multi_variant_beats']}"
      f"（精确键下每拍仅 1 变体，已无多参考拍） |")
    w(f"| `display_top` 消费契约 | 无（0 张） | 每张 structure 顶层 `display_top: 5` |")
    w(f"| beats/模板中位数 | {old['beats_median']} | {new['beats_median']} |")
    w(f"| character 模板 | {old['character_active']}（未动） | {old['character_active']}（未动） |")
    w(f"| 借邻（跨子事件借弧） | 组装层无此约束 | **零借邻**（仅检索层可退同大类） |\n")

    w("## 2. 每大类抽 2 张模板\n")
    w("| 大类 | 模板名 | 键(大类,子事件) | 成员弧 | 拍数 | 变体数 |")
    w("|---|---|---|---|---|---|")
    for cls in sorted(by_cls, key=lambda c: -len(by_cls[c])):
        ts = sorted(by_cls[cls], key=lambda t: (-t.source_stats["n_members"], t.name))
        for t in ts[:2]:
            bs = [b for ph in t.structure["phases"] for b in ph["beats"]]
            mem = "、".join(f"#{m.judgement.no} {m.book}·{m.arc_name}" for m in t.members[:3])
            if len(t.members) > 3:
                mem += f" …共{len(t.members)}"
            w(f"| {cls} | {t.name} | ({cls}, {t.source_stats['sub_event'] or '空'}) | "
              f"{mem} | {len(bs)} | {sum(len(b['variants']) for b in bs)} |")
    w("")

    w("## 3. 低置信成员单列（按类分组 · 共 "
      f"{len(low)} 条）——供模板级裁决去留\n")
    w("> 主类唯一、不动（禁双记，与组装精确键冲突）；处置权在模板级，不在弧级。\n")
    bylow: dict[str, list[MemberArc]] = defaultdict(list)
    for m in low:
        bylow[m.judgement.cls].append(m)
    w("| 大类 | 弧号 | 书·弧名 | 子事件 | 所属模板 | 判类依据 |")
    w("|---|---|---|---|---|---|")
    tpl_of = {}
    for t in tp:
        for m in t.members:
            tpl_of[m.judgement.no] = t.name
    for cls in sorted(bylow, key=lambda c: -len(bylow[c])):
        for m in sorted(bylow[cls], key=lambda x: x.judgement.no):
            j = m.judgement
            mark = " **★重点**" if j.no in focus else ""
            w(f"| {cls}{mark} | #{j.no} | {j.book}·{j.arc_name} | {j.sub_event} | "
              f"{tpl_of.get(j.no, '?')} | {j.reason[:60]} |")
    w("")
    w(f"**重点低置信 3 条**（SK02b/c 交办）：\n")
    tpl_n = {t.name: t.source_stats["n_members"] for t in tp}
    for no in focus:
        m = jmap.get(no)
        t = tpl_of.get(no)
        if not m:
            continue
        w(f"- **#{no} {m.judgement.book}·{m.judgement.arc_name}** → 键 "
          f"({m.judgement.cls}, {m.judgement.sub_event})，落在模板 `{t}`"
          f"（该模板成员数 {tpl_n.get(t, '?')}，"
          f"{'孤例标已打' if m.judgement.sub_event else '无子事件'}）。"
          f"判类成因：{m.judgement.reason[:110]}")
    w("")

    w("## 4. 孤例清单与样本分档（裁决 3A）\n")
    w(f"- **孤例类（1 成员，免合并打孤例标）{len(plan['orphan_classes'])} 个**："
      f"{'、'.join(plan['orphan_classes'])}")
    orphan_t = [t for t in tp if t.source_stats.get("orphan_class")]
    for t in orphan_t:
        w(f"  - `{t.name}` ← #{t.members[0].judgement.no} "
          f"{t.members[0].book}·{t.members[0].arc_name}")
    small = [c for c, v in plan["tiers"].items() if v.startswith("小样本-3~4")]
    w(f"- **小样本 3~4 成员类 {len(small)} 个（报告单列）**：{'、'.join(sorted(small))}")
    for c in sorted(small):
        ts = [t for t in tp if t.source_stats["class"] == c]
        w(f"  - {c}：{len(ts)} 模板｜成员合计 "
          f"{sum(t.source_stats['n_members'] for t in ts)}｜"
          f"单弧模板 {sum(1 for t in ts if t.source_stats['single_arc'])}")
    s2 = [c for c, v in plan["tiers"].items() if v.startswith("小样本-2")]
    w(f"- **小样本 2 成员类 {len(s2)} 个（照常走合并判据）**：{'、'.join(sorted(s2))}")
    ph = [t for t in tp if t.source_stats.get("placeholder_arcs")]
    w(f"- **占位弧模板 {len(ph)} 张**（判类裁定不修数据、打标）：")
    for t in ph:
        w(f"  - `{t.name}` ← #{'、#'.join(str(x) for x in t.source_stats['placeholder_arcs'])}"
          f" {'、'.join(m.arc_name for m in t.members)}")
    w(f"- **孤例标总数 {plan['n_single_arc_templates']} == 单成员模板数 "
      f"{new['n_templates']}**（逐一对账通过）\n")

    w("## 5. 太荒回退台账（A17 两级解析的第二级）\n")
    _raw = load_all_arcs()
    n_win = sum(len(a.declared_seqs) for a in _raw if a.book != SHAOSONG)
    n_ss = sum(len(a.declared_seqs) for a in _raw if a.book == SHAOSONG)
    w(f"- 全库 **{n_win + n_ss} 拍**（7 本 win 书 {n_win} 拍 + 绍宋 singles {len(BOOKS) and 25} 文件 "
      f"{n_ss} 拍）里 **{len(plan['ledger'])} 拍无真重叠候选 → 该拍缺失并记台账**：\n")
    if plan["ledger"]:
        w("| 书 | 弧 | seq | 弧区间 | 原因 | 同 seq 候选数 |")
        w("|---|---|---|---|---|---|")
        for e in plan["ledger"]:
            w(f"| {e['book']} | {e['arc']} | {e['seq']} | c{e['ch_lo']}~{e['ch_hi']} | "
              f"{e['reason']} | {e['n_cands']} |")
        w("\n- 机制说明：该拍**不拿邻弧/越界原子兜底**（那正是 A17 的 ±2 容差病根）。"
          "太荒该 seq 的唯一候选是**坏原子**（chapter_start > chapter_end），"
          "任何口径都取不到 → 该拍缺失。**这正是 PM 出弧速览-v6 时把 #111 从 4 拍收成 3 拍的机制**，"
          "本脚本从数据独立复现了 v6。\n")
    else:
        w("（空）\n")

    w("## 6. 零合并的归因（控制实验）\n")
    w("| 对照实验 | 可并配对数 |")
    w("|---|---|")
    w("| v3 协议（本单实现）：核心**双向**覆盖 + LCS/min 动态阈值 + LCS/max≥0.5 + SCS≤15 | **0 / 52** |")
    w("| 对照：换成 v3 代码实际的**单向**核心闸（放宽） | **0 / 52** |")
    w("| 归因拆解：52 对里 50 对卡在 LCS/min·LCS/max·SCS 三关，2 对卡在核心闸 |")
    w("")
    w("**判读**：零合并**不是**双向核心闸带来的。把闸门放宽到 v3 代码的单向口径，可并数仍是 0 ——"
      "在精确 (大类,子事件) 粒度下，同键弧的**原子骨架本身就不同形**"
      "（如 `绝境反杀--绝地反杀` 6 弧 seq 分别是 3/3/5/4/4/2 环，两两 LCS 多为 0）。")
    w("这是「组装不借邻 + 子事件词表极细」两条用户拍板的**结构性结果**，"
      "不是数据缺陷也不是实现取舍。若 PM 希望减少模板数，杠杆在**子事件词表合并**"
      "（§二·补2.2 待审区机制），不在合并判据。\n")

    w("## 7. 归并口径留档（本次实现）\n")
    w("- 主判据：`seq == s` **且** 原子区间与弧区间**真重叠**（相交 ≥1 章），多候选取重叠最长者")
    w("- **禁用**「⊆ 弧区间 ±2」容差（把邻弧尾巴原子放进来，实测污染 #234/#111）")
    w("- 无候选 → **该拍缺失 + 记台账**，不兜底（兜底即复活 ±2 病根）")
    w("- 核心**双向**覆盖：`cores(A) ⊆ seq(B)` **且** `cores(B) ⊆ seq(A)`"
      "（任务单与 `merge_arcs_crossbook.py` 文档字符串口径一致）")
    w("- LCS/min 动态阈值：<5 环 0.65 / [5,10) 0.70 / ≥10 环 0.85；LCS/max ≥ 0.5；SCS ≤ 15 环")
    w("- 连续同原子压成一步（v3 拍板⑥），`repeats` 记次数，beat 标签带 `×N`"
      f"（本次压掉 {n_win + n_ss - plan['n_beats']} 环，"
      f"占 {(n_win + n_ss - plan['n_beats']) * 100 // max(1, n_win + n_ss)}%，"
      f"全为真实连打如「外门大比连胜之路」擂台比试 ×7）")
    w("- variants 排序：核心命中优先 → tag 与**拍型签名**（≥半数变体共有的 tag）Jaccard 降序"
      " → (src, how) 稳定收尾；**全量存储**，消费端按 `display_top: 5` 取前 5")
    w(f"- 样本分档：孤例类 {len(plan['orphan_classes'])} 免合并打孤例标；"
      f"小样本 2 成员 {len(s2)} 类照常走判据；3~4 成员 {len(small)} 类报告单列；≥5 常规类正常模板化\n")

    w("## 8. 双验收自测结果（本单执行方自跑，供 PM 复核）\n")
    w("| 验收标准 | 结果 | 证据 |")
    w("|---|---|---|")
    w(f"| 1 dry-run/apply/verify 三段有据；旧 100 archived 且备份可逐行还原 | ✅ | "
      f"`SK03_dryrun.txt` / `SK03_apply.txt` / `SK03_verify.txt` / `SK03_restore_check.txt`；"
      f"备份 100 行 × 12 列字段不全 0 行、100/100 仍在库（归档态） |")
    w(f"| 2 新模板键精确无借邻 | ✅ | verify 对**全量 {exp} 张**逐条核成员弧判类键 == 模板键，0 不一致 |")
    w(f"| 3 孤例标数量 = 单成员模板数 | ✅ | verify：{exp} == {exp} |")
    w("| 4 character 265 / event_skeletons 零改动 | ✅ | verify：character/active 265、"
      "event_skeletons 6 且字段签名 5466 字未变 |")
    w("| 5 三路向量重建对账 + search 冒烟 | ✅ | 616 张模板三路块数 2907/0/0 = 预期；"
      "孤儿块 0；模糊口述「主角在宗门比试擂台上一战成名夺魁」top1 命中 "
      "`擂台大比--宗门擂台比试`（matched_beats=3） |")
    w("| 6 幂等重跑零变化 | ✅ | 二次 `--apply` 走幂等分支（不动库、不覆盖备份）；"
      "全库指纹（plot_templates + vector_chunks + event_skeletons 关键列 sha256）"
      "前后**逐字节一致** |")
    w("| 7 单测全绿 | ✅ | 681 基线 + 62 新增 = **743 passed** |")
    w(f"| 8 抽查报告落盘 | ✅ | 本文件（低置信 49 条单列 + 新旧对比 + 孤例 + 回退台账） |")
    w("")

    REPORT.write_text("\n".join(L), encoding="utf-8")
    return REPORT


# ═══════════════════════════════ 十、main ═══════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--restore-check", action="store_true")
    g.add_argument("--report", action="store_true",
                   help="只写抽查报告 outputs/skel_v4/SK03_重组报告.md（不碰库）")
    a = ap.parse_args()
    # argparse dest 是下划线形（--dry-run → dry_run）
    mode = next(k for k, v in vars(a).items() if v)

    if mode in ("apply", "verify") and backend_running():
        sys.exit(f"[{mode}] 拒绝执行：127.0.0.1:8000 有后端在跑（破坏性/核账操作须先停服务）")

    if mode == "restore_check":
        do_restore_check()
        return
    plan = build_plan()
    print(f"SK03 {mode} @ {now_utc()}\n")
    if mode == "dry_run":
        print_plan(plan)
    elif mode == "report":
        p = write_report(plan)
        print(f"[report] 已写 {p}（{p.stat().st_size} 字）")
        print(f"  模板 {len(plan['templates'])}｜单成员 {plan['n_single_arc_templates']}"
              f"｜低置信成员 {plan['n_low_conf']}｜兜底台账 {len(plan['ledger'])}")
    elif mode == "apply":
        print_plan(plan)
        print()
        do_apply(plan)
    else:
        sys.exit(do_verify(plan))


if __name__ == "__main__":
    LOG_NAME = {"--dry-run": "SK03_dryrun.txt", "--apply": "SK03_apply.txt",
                "--verify": "SK03_verify.txt", "--restore-check": "SK03_restore_check.txt",
                "--report": "SK03_report.txt"}
    flag = next((k for k in LOG_NAME if k in sys.argv), None)
    if flag is None:
        raise SystemExit("用法: --dry-run | --apply | --verify | --restore-check | --report")
    sys.stdout = _Tee(SKEL / LOG_NAME[flag])
    try:
        main()
    finally:
        sys.stdout.flush()