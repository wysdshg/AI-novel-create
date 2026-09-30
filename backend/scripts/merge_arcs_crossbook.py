# -*- coding: utf-8 -*-
"""
三书跨书归并（docs/10 §11 生成式骨架管线 · 第②③步：单弧模板 → 归并）
================================================================
纯算法、0 次模型调用。按 2026-09-18 已拍板的 9 项协议实现：

- 输入：多本书的切原子产物 outputs/_atomic_raw/win_<书>.json（弧 + core_atomics + 原子明细）
- 单弧模板：弧 = 原子序列；**连续同原子压成一步**（repeat_count，拍板⑥）；核心 = core_atomics（拍板①，归并只读不改）
- 归并判据（拍板②）：
    a) **核心双向全覆盖**（硬门槛）：cores(A) ⊆ set(B.seq) 且 cores(B) ⊆ set(A.seq)
       —— 「核心可多个，一个不能少；非核心可缺」（§11.1 ③ 的反例语义）
    b) **LCS(A,B) / min(len(A),len(B)) ≥ 0.7**（**包含度**，分母 min，见 §11.4.2）
    c) 合并后链长 ≤ --max-len（默认 15，拍板⑤ 13~15 取上限）
- 合并算法：**SCS 最短公共超序列**（= LCS 骨架 + 各自独有环按原相对顺序插入），与 §11.1 ③ 的合并例一致
- 迭代：最多 --rounds 轮（默认 3，拍板⑤），每轮扫全对直到无合并发生
- 部分匹配**不落库**（拍板③）—— 本脚本只做完全归并，检索时的部分匹配另行实现
- 「部分匹配不落库」与「向上归并链长上限」都不影响本脚本的纯计算性质

输出：
- outputs/_atomic_raw/merge_<书们>.json  结构化结果（组 + 成员 + 挂环）
- 控制台统计 + 分类代表（单弧 / 单书多弧 / 两书 / 三书 / 三书含一书多弧）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DB_RO = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"
ROOT = Path(__file__).resolve().parents[2]          # backend/scripts/x.py → 项目根
RAW_DIR = ROOT / "outputs" / "_atomic_raw"


# ---------------------------------------------------------------- 数据结构
@dataclass
class Arc:
    book: str
    arc_name: str
    ch_lo: int
    ch_hi: int
    seq: list[str]                 # 压缩连续同原子后的原子 ID
    repeats: list[int]             # 每步的重复次数（≥1）
    cores: list[str]               # core_atomics（去重、保持顺序）
    summaries: list[str]           # 每步的起承转合概括（首步原文，后续步并入）
    tags: list[list[str]]


@dataclass
class Group:
    gid: int
    seq: list[str]
    repeats: list[int]
    cores: list[str]
    members: list[Arc] = field(default_factory=list)      # 骨架成员（参与 SCS）
    references: list[Arc] = field(default_factory=list)   # 参考成员（完全包含挂靠，不改骨架）
    rounds: int = 1                # 经历了几轮归并


# ---------------------------------------------------------------- 基础算法
def lcs_dp(a: list[str], b: list[str]) -> list[list[int]]:
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    return dp


def lcs_len(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    return lcs_dp(a, b)[0][0]


def scs_merge(a: list[str], rep_a: list[int], b: list[str], rep_b: list[int]):
    """最短公共超序列：LCS 骨架 + 各自独有环按原相对顺序插入。
    返回 (merged_seq, merged_repeats)。重复次数：同环两边都有时取 max（走法覆盖上限）。"""
    dp = lcs_dp(a, b)
    out, out_rep = [], []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            out.append(a[i]); out_rep.append(max(rep_a[i], rep_b[j])); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            out.append(a[i]); out_rep.append(rep_a[i]); i += 1
        else:
            out.append(b[j]); out_rep.append(rep_b[j]); j += 1
    while i < len(a):
        out.append(a[i]); out_rep.append(rep_a[i]); i += 1
    while j < len(b):
        out.append(b[j]); out_rep.append(rep_b[j]); j += 1
    return out, out_rep


def align_positions(member_seq: list[str], group_seq: list[str]) -> list[list[int]]:
    """成员序列每个原子 → 在组序列上的匹配位置（贪心双指针，保持顺序；组上无位的环=[]）。
    🔴 修复（2026-09-19，四书三书组实测暴露）：旧实现拿 lcs_dp（前缀语义表）做**前向**回溯，
    dp[i+1][j]/dp[i][j+1] 比较值错位 → 多原子成员几乎全部对齐失败（三书组 8 环 7 环 covered_by 空）。
    成员必为组骨架子序列（SCS 合并性质，can_merge 三态保证），贪心匹配即正确对齐。"""
    pos: list[list[int]] = [[] for _ in member_seq]
    j = 0
    for i, a in enumerate(member_seq):
        while j < len(group_seq) and group_seq[j] != a:
            j += 1
        if j < len(group_seq):
            pos[i].append(j)
            j += 1
    return pos


def compress(seq: list[str]) -> tuple[list[str], list[int]]:
    """连续相同原子压成一步（拍板⑥）。"""
    out, rep = [], []
    for x in seq:
        if out and out[-1] == x:
            rep[-1] += 1
        else:
            out.append(x); rep.append(1)
    return out, rep


# ---------------------------------------------------------------- 加载
def load_vocab() -> dict[str, dict]:
    con = sqlite3.connect(DB_RO, uri=True)
    cur = con.cursor()
    vocab = {}
    for aid, name, cat_name, dom in cur.execute("""
            SELECT e.id, e.name, c.name, c.domain FROM atomic_events e
            LEFT JOIN atomic_categories c ON c.id = e.category_id""").fetchall():
        vocab[aid] = {"name": name, "cat": cat_name or "", "domain": dom or ""}
    con.close()
    return vocab


def load_book(path: Path, vocab: dict[str, dict], clean_cores: bool = True) -> list[Arc]:
    d = json.loads(path.read_text(encoding="utf-8"))
    book = d.get("book") or path.stem.replace("win_", "")
    atoms_by_seq = {int(a["seq"]): a for a in d.get("atoms") or []}
    arcs = []
    n_float = n_dropped_arcs = 0
    for arc in d.get("arcs") or []:
        seqs = arc.get("atoms") or []
        raw = [atoms_by_seq.get(int(s)) for s in seqs]
        raw = [a for a in raw if a]
        if not raw:
            continue
        ids = [str(a["atomic_id"]) for a in raw]
        cseq, crep = compress(ids)
        # 压缩后每步取首步概括；被压缩步的 tags 并入
        summaries, tags = [], []
        k = 0
        for step, r in enumerate(crep):
            first = raw[k]
            summaries.append(str(first.get("summary") or "").strip())
            tg = []
            for off in range(r):
                tg.extend(raw[k + off].get("tags") or [])
            tags.append(sorted(set(tg)))
            k += r
        # 🔴 核心清洗（2026-09-19 实测：飘标率 87.5% —— AI 标 core_atomics 不受
        #    「必须是本弧原子」约束，飘标核心会让弧永远无法参与归并）。
        #    core ∉ 本弧序列 → 丢弃（数据清洗，不改归并判据本身）。
        seqset = set(cseq)
        cores = []
        for c in arc.get("core_atomics") or []:
            c = str(c)
            if c not in cores:
                cores.append(c)
        if clean_cores and cores:
            kept = [c for c in cores if c in seqset]
            if len(kept) != len(cores):
                n_float += len(cores) - len(kept)
                n_dropped_arcs += 1
                cores = kept
        arcs.append(Arc(book=book, arc_name=str(arc.get("arc_name") or f"弧{len(arcs)+1}"),
                        ch_lo=int(arc.get("chapter_start") or 0), ch_hi=int(arc.get("chapter_end") or 0),
                        seq=cseq, repeats=crep, cores=cores, summaries=summaries, tags=tags))
    if clean_cores and n_float:
        print(f"  [{book}] 核心清洗：丢弃飘标核心 {n_float} 个（涉及 {n_dropped_arcs} 弧）")
    return arcs


# ---------------------------------------------------------------- 归并
# 动态 min 门槛分段参数（2026-09-19 用户拍板：弧短时降 min 帮扩充，弧长时提 min 限扩充）
_T_MID = 0.70        # 常规段（拍板②原值）
_T_LONG = 0.85       # 长组段：使 5/6=0.83 拦、6/7=0.86 过（用户原话「主环十个时 5/6 都没必要并、至少 6/7」）
_SHORT_LT = 5        # 短弧上界（<5 环）
_LONG_GE = 10        # 长组下界（≥10 环）
# 🔴 GATE = 分段策略常量的**唯一出处**（main() 用 args 覆盖；t_short 走参数传，因为是「随调用变化的期望值」）。
#    不能用函数默认参数承载——Python 默认值在 def 时绑定，运行期覆盖模块常量不会生效。
GATE = {"t_mid": _T_MID, "t_long": _T_LONG, "short_lt": _SHORT_LT, "long_ge": _LONG_GE}


def min_threshold_for(la: int, lb: int, t_short: float) -> float:
    """动态 min 门槛。分段依据 = **较长方的环数**（链式里通常是组/主环长度，
    与「主环达到十个时」的语义一致）：
    - < short_lt(5) 环：短弧取值离散（3 环只有 0.33/0.67/1.0 三档），0.7 会卡掉 0.67
      这个「只差一环」的自然档位 → 降到 t_short(0.65) 帮短弧聚合（实测正是斗×太跨书桥）；
    - [short_lt, long_ge) 环：常规 t_mid(0.70)（拍板②原值）；
    - ≥ long_ge(10) 环：长组吸收要求近全包 → t_long(0.85)。
    """
    m = max(la, lb)
    if m < GATE["short_lt"]:
        return t_short
    if m < GATE["long_ge"]:
        return GATE["t_mid"]
    return GATE["t_long"]


def can_merge(A: Group, B: Group, thr_min: float, thr_max: float, max_len: int,
              thr: float | None = None) -> tuple[str, float, str]:
    """归并判据（2026-09-19 用户拍板版）。返回 (mode, score, reason)，mode ∈ {"merge","reference","reject"}。

    ① **双重包含度门槛**（骨架级归并，`len(SCS) ≤ 15` 时）：
       - `LCS/min(lenA,lenB) ≥ 动态min`（分段：短<5 环 0.65 / 中 0.70 / 长≥10 环 0.85）
       - `LCS/max(lenA,lenB) ≥ thr_max(0.5)`（拦长短悬殊；锚点=提案合并例 3/6=0.5 刚好过；
         1 环挂 7 环 0.14 / 4 环仅 3 同挂 12 环 0.25 全拦）
    ② **15 环超限规则**：`len(SCS) > 15` 时仅当 **B 完全包含于 A**（LCS/min = 1.00
       且 B 不长于 A）→ 挂**参考成员**（骨架不变）；否则**另起模板**。
       🔴 两条通道分开判定：超长分支不看比率——完全包含本身即最强相似证据。
    ③ 单向核心覆盖（提案 §11.1③ 原文语义）：新成员 B ⊇ 目标组 A 的核心。
    """
    if thr is not None:            # 兼容旧调用（旧 thr= min 口径基准值）
        thr_min = thr
    sa, sb = set(A.seq), set(B.seq)
    if not set(A.cores) <= sb:
        miss = [c for c in A.cores if c not in sb]
        return "reject", 0.0, f"B 缺 A 核心 {miss}"
    l = lcs_len(A.seq, B.seq)
    la, lb = len(A.seq), len(B.seq)
    if la == 0 or lb == 0:
        return "reject", 0.0, "空序列"
    r_min, r_max = l / min(la, lb), l / max(la, lb)
    merged, _ = scs_merge(A.seq, A.repeats, B.seq, B.repeats)
    # 🔴 两条通道**分开判定**（2026-09-19 单元验证修正）：
    #    超长 → 参考通道只看「完全包含」，不看比率（完全包含本身即最强相似证据）；
    #    正常 → 骨架通道看双门槛（max 拦长短悬殊、动态 min 短宽长严）。
    if len(merged) > max_len:
        if l == lb and lb <= la:
            return "reference", r_min, f"SCS {len(merged)} > {max_len}，但 B ⊆ A → 挂参考"
        return "reject", r_min, f"SCS {len(merged)} > {max_len} 且非完全包含 → 另起模板"
    t_eff = min_threshold_for(la, lb, thr_min)
    if r_min < t_eff:
        return "reject", r_min, f"包含度 min {r_min:.2f} < {t_eff:.2f}(动态)"
    if r_max < thr_max:
        return "reject", r_max, f"包含度 max {r_max:.2f} < {thr_max}（长短悬殊）"
    return "merge", r_min, ""


def diagnose(all_arcs: list[Arc], thr_min: float, thr_max: float):
    """扫全对：双口径直方 + 卡点分类 + 跨书最接近的对。回答「归并效果为什么是这样」。
    🔴 拍板②：min 门槛为**动态分段**——每对按 t_eff = min_threshold_for(la, lb, thr_min)
    判定，与 can_merge 完全同口径；跨书统计同样用 t_eff。"""
    from collections import Counter
    n = len(all_arcs)
    hist = Counter()
    cross_pairs = []              # (r_min, r_max, t_eff, A, B, l)
    for i in range(n):
        for j in range(i + 1, n):
            A, B = all_arcs[i], all_arcs[j]
            l = lcs_len(A.seq, B.seq)
            la, lb = len(A.seq), len(B.seq)
            r_min = l / min(la, lb) if la and lb else 0.0
            r_max = l / max(la, lb) if la and lb else 0.0
            t_eff = min_threshold_for(la, lb, thr_min)
            cross = A.book != B.book
            if r_min >= t_eff and r_max >= thr_max:
                hist["双达标"] += 1
            elif r_min >= t_eff:
                hist["min达标/max卡"] += 1
            elif r_max >= thr_max:
                hist["max达标/min卡"] += 1
            else:
                hist["双不达"] += 1
            if cross:
                cross_pairs.append((r_min, r_max, t_eff, A, B, l))
    total = n * (n - 1) // 2
    print(f"\n===== 诊断：全 {total} 对（动态 min：<{GATE['short_lt']}环→{thr_min} / "
          f"{GATE['short_lt']}~{GATE['long_ge']-1}环→{GATE['t_mid']} / "
          f"≥{GATE['long_ge']}环→{GATE['t_long']}；max≥{thr_max}）=====")
    for k in ["双达标", "min达标/max卡", "max达标/min卡", "双不达"]:
        print(f"  {k:14s} {hist.get(k,0):6d} ({hist.get(k,0)/total*100:.1f}%)")
    cross_pairs.sort(key=lambda x: (-min(x[0], x[1]), -x[1]))
    n_cross = len(cross_pairs)
    ok = [p for p in cross_pairs if p[0] >= p[2] and p[1] >= thr_max]
    mx = [p for p in cross_pairs if p[0] >= p[2] and p[1] < thr_max]
    print(f"  跨书对 {n_cross}：双达标 {len(ok)}｜min达标被max卡 {len(mx)}")
    print("  跨书 top-10（按双口径排序）:")
    for r_min, r_max, t_eff, A, B, l in cross_pairs[:10]:
        print(f"    min={r_min:.2f}(t={t_eff:.2f}) max={r_max:.2f} (LCS {l}) "
              f"[{A.book}#{A.arc_name} c{A.ch_lo}~{A.ch_hi} {'→'.join(A.seq)}]"
              f" × [{B.book}#{B.arc_name} c{B.ch_lo}~{B.ch_hi} {'→'.join(B.seq)}]")


def merge_groups(groups: list[Group], thr_min: float, thr_max: float, max_len: int, rounds: int):
    """贪心多轮归并：每轮按分数降序扫全对，合并发生则更新代表，直到无合并。"""
    n_ref = 0
    for rd in range(1, rounds + 1):
        merged_any = False
        while True:
            best = None  # (score, i, j, mode)
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    mode, score, _ = can_merge(groups[i], groups[j], thr_min, thr_max, max_len)
                    if mode != "reject" and (best is None or score > best[0]):
                        best = (score, i, j, mode)
            if best is None:
                break
            _, i, j, mode = best
            A, B = groups[i], groups[j]
            if mode == "reference":
                A.references.extend(B.members + B.references)
                n_ref += len(B.members)
                groups.pop(j)
                merged_any = True
                continue
            seq, rep = scs_merge(A.seq, A.repeats, B.seq, B.repeats)
            cores = list(dict.fromkeys(A.cores + B.cores))
            A.seq, A.repeats, A.cores = seq, rep, cores
            A.members.extend(B.members)
            A.references.extend(B.references)
            A.rounds = max(A.rounds, rd)
            groups.pop(j)
            merged_any = True
        print(f"  轮 {rd}：组数 → {len(groups)}")
        if not merged_any:
            break
    if n_ref:
        print(f"  参考挂靠 {n_ref} 弧")
    return groups


def merge_flat(arcs: list[Arc], thr_min: float, thr_max: float, max_len: int,
               cross_first: bool = False) -> list[Group]:
    """扁平归并（对照模式）：只看**弧对弧**判据，union-find 成组，组代表不再参与归并。
    动机（2026-09-19 实测）：链式全局贪心会让「书内 1.00 分对」抢先吸光大组，
    把跨书对全挤掉（凡人内部模式化程度高）。flat 模式让跨书组由弧对弧关系直接决定。
    边分两类：merge 边（更新组代表）优先于 reference 边（连通但不改骨架）。
    🔴 cross_first（2026-09-19）：跨书边**整体先于**书内边处理——归并目的是跨书，
    书内聚合是副产品；否则书内 1.0 分边先吸成巨组，跨书 0.67 边轮到时对端已变成
    巨组代表，被「单向核心 + 长组段门槛」双重拦死（斗×太 0.67 桥实测死因）。"""
    n = len(arcs)
    parent = list(range(n))
    rep_seq: list[list[str]] = [a.seq[:] for a in arcs]
    rep_rep: list[list[int]] = [a.repeats[:] for a in arcs]
    ref_into: dict[int, list[int]] = {}   # root -> 参考弧 idx

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            Gi = Group(gid=0, seq=rep_seq[i], repeats=rep_rep[i], cores=arcs[i].cores)
            Gj = Group(gid=0, seq=rep_seq[j], repeats=rep_rep[j], cores=arcs[j].cores)
            mode, score, _ = can_merge(Gi, Gj, thr_min, thr_max, max_len)
            if mode != "reject":
                cross = 0 if (cross_first and arcs[i].book != arcs[j].book) else 1
                edges.append((cross, score, 0 if mode == "merge" else 1, mode, i, j))
    # 升序：cross=0(跨书) 在 cross=1(书内) 前；组内分数降序、merge 边先于 reference 边
    edges.sort(key=lambda e: (e[0], -e[1], e[2]))
    for _, score, _, mode, i, j in edges:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        A = Group(gid=0, seq=rep_seq[ri], repeats=rep_rep[ri], cores=[])
        B = Group(gid=0, seq=rep_seq[rj], repeats=rep_rep[rj], cores=[])
        mode2, _, _ = can_merge(A, B, thr_min, thr_max, max_len)
        if mode2 == "reject":
            continue
        if mode2 == "reference":
            parent[rj] = ri
            ref_into.setdefault(ri, []).extend([rj] + ref_into.pop(rj, []))
            continue
        seq, rep = scs_merge(A.seq, A.repeats, B.seq, B.repeats)
        parent[rj] = ri
        rep_seq[ri], rep_rep[ri] = seq, rep
        if rj in ref_into:                       # 参考随分量搬家
            ref_into.setdefault(ri, []).extend(ref_into.pop(rj))
    groups_map: dict[int, list[int]] = {}
    for i in range(n):
        groups_map.setdefault(find(i), []).append(i)
    groups = []
    for root, members_idx in groups_map.items():
        cores = list(dict.fromkeys(c for mi in members_idx for c in arcs[mi].cores))
        ref_idx = [x for x in ref_into.get(root, []) if x not in members_idx]
        groups.append(Group(gid=0, seq=rep_seq[root], repeats=rep_rep[root], cores=cores,
                            members=[arcs[mi] for mi in members_idx],
                            references=[arcs[x] for x in ref_idx]))
    n_ref = sum(len(g.references) for g in groups)
    print(f"  flat：{n} 弧 → {len(groups)} 组（骨架成员 {sum(len(g.members) for g in groups)}"
          f"｜参考挂靠 {n_ref}）")
    return groups


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", nargs="+", required=True, help="书名列表（找 outputs/_atomic_raw/win_<书>.json）")
    ap.add_argument("--min-short", type=float, default=0.65,
                    help="动态 min·短弧段（较长方 <short-lt 环；拍板②：弧短降 min 帮扩充，解锁 3环 0.67 档）")
    ap.add_argument("--min-mid", type=float, default=_T_MID,
                    help="动态 min·常规段（[short-lt, long-ge) 环；拍板②原值）")
    ap.add_argument("--min-long", type=float, default=_T_LONG,
                    help="动态 min·长组段（≥long-ge 环；使 5/6=0.83 拦、6/7=0.857 过）")
    ap.add_argument("--short-lt", type=int, default=_SHORT_LT, help="短弧段上界（环数，<此值用 min-short）")
    ap.add_argument("--long-ge", type=int, default=_LONG_GE, help="长组段下界（环数，≥此值用 min-long）")
    ap.add_argument("--max-ratio", type=float, default=0.5,
                    help="LCS/max(len) 门槛（2026-09-19 拍板：拦长短悬殊；0.5=提案合并例锚点）")
    ap.add_argument("--max-len", type=int, default=15,
                    help="链长上限；超限时仅「完全包含」可挂参考成员，否则另起模板（2026-09-19 拍板）")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", default=None, help="JSON 输出路径")
    ap.add_argument("--diagnose", action="store_true", help="只诊断不归并：双口径直方 + 跨书 top 对")
    ap.add_argument("--no-clean-cores", action="store_true", help="关闭核心清洗（默认清洗飘标核心）")
    ap.add_argument("--flat", action="store_true",
                    help="扁平归并（对照）：只看弧对弧判据 union-find 成组，组代表不再参与归并")
    ap.add_argument("--cross-first", action="store_true",
                    help="flat 模式下跨书边整体先于书内边处理（默认纯分数降序，书内 1.0 边会抢先吸成巨组）")
    args = ap.parse_args()
    # 分段策略常量 → GATE（唯一出处；t_short 不进 GATE，走参数显式传给 min_threshold_for）
    GATE["t_mid"], GATE["t_long"] = args.min_mid, args.min_long
    GATE["short_lt"], GATE["long_ge"] = args.short_lt, args.long_ge
    gate_desc = (f"动态min(短<{args.short_lt}→{args.min_short}/中→{args.min_mid}/"
                 f"长≥{args.long_ge}→{args.min_long}) max≥{args.max_ratio}")

    vocab = load_vocab()
    clean = not args.no_clean_cores
    all_arcs: list[Arc] = []
    for b in args.books:
        p = RAW_DIR / f"win_{b}.json"
        arcs = load_book(p, vocab, clean_cores=clean)
        all_arcs.extend(arcs)
        cov_cores = sum(1 for a in arcs if a.cores)
        print(f"{b}: {len(arcs)} 弧（有核心标注 {cov_cores}）")
    print(f"合计 {len(all_arcs)} 条单弧模板")

    if args.diagnose:
        diagnose(all_arcs, args.min_short, args.max_ratio)
        return 0

    if args.flat:
        print(f"扁平归并（{gate_desc} max_len={args.max_len} cross_first={args.cross_first}）")
        groups = merge_flat(all_arcs, args.min_short, args.max_ratio, args.max_len,
                            cross_first=args.cross_first)
    else:
        print(f"开始归并（{gate_desc} max_len={args.max_len} rounds={args.rounds}）")
        groups = [Group(gid=i, seq=a.seq, repeats=a.repeats, cores=list(a.cores), members=[a])
                  for i, a in enumerate(all_arcs)]
        groups = merge_groups(groups, args.min_short, args.max_ratio, args.max_len, args.rounds)

    # 分类统计
    def books_of(g: Group):
        return sorted({m.book for m in g.members})

    stat = {"单弧": 0, "单书多弧": 0, "两书": 0, "三书": 0}
    for g in groups:
        nb = len(books_of(g))
        if len(g.members) == 1:
            stat["单弧"] += 1
        elif nb == 1:
            stat["单书多弧"] += 1
        elif nb == 2:
            stat["两书"] += 1
        else:
            stat["三书"] += 1
    print("组分类:", stat, "｜总组数", len(groups),
          f"｜参考挂靠 {sum(len(g.references) for g in groups)}")

    # 结构化落盘
    out_groups = []
    for gi, g in enumerate(groups):
        bks = books_of(g)
        covers = [[] for _ in g.seq]
        mem_rows = []
        for m in g.members:
            pos = align_positions(m.seq, g.seq)
            for pi, pl in enumerate(pos):
                for q in pl:
                    covers[q].append(f"{m.book}·{m.arc_name}")
            mem_rows.append({
                "book": m.book, "arc_name": m.arc_name, "ch_lo": m.ch_lo, "ch_hi": m.ch_hi,
                "cores": m.cores, "seq": m.seq, "repeats": m.repeats,
                "summaries": m.summaries, "tags": m.tags,
            })
        ref_rows = [{"book": m.book, "arc_name": m.arc_name, "ch_lo": m.ch_lo, "ch_hi": m.ch_hi,
                     "cores": m.cores, "seq": m.seq, "repeats": m.repeats,
                     "summaries": m.summaries, "tags": m.tags}
                    for m in g.references]
        out_groups.append({
            "gid": gi, "n_members": len(g.members), "n_references": len(g.references),
            "books": bks, "ref_books": sorted({m.book for m in g.references}),
            "seq": g.seq, "repeats": g.repeats, "cores": g.cores,
            "rounds": g.rounds,
            "rings": [{"atomic_id": aid, "name": vocab.get(aid, {}).get("name", aid),
                       "cat": vocab.get(aid, {}).get("cat", ""),
                       "repeat": g.repeats[ri], "covered_by": covers[ri]}
                      for ri, aid in enumerate(g.seq)],
            "members": mem_rows, "references": ref_rows,
        })
    out = {"books": args.books,
           "gate": {"mode": "dynamic_min", "min_short": args.min_short, "min_mid": args.min_mid,
                    "min_long": args.min_long, "short_lt": args.short_lt, "long_ge": args.long_ge},
           "max_ratio": args.max_ratio, "max_len": args.max_len, "flat": bool(args.flat),
           "cross_first": bool(args.cross_first),
           "n_arcs": len(all_arcs), "n_groups": len(groups), "stat": stat, "groups": out_groups}
    outp = Path(args.out) if args.out else RAW_DIR / f"merge_{'_'.join(args.books)}.json"
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 已写 {outp}（{outp.stat().st_size/1024:.0f} KB）｜总弧 {len(all_arcs)} → 组 {len(groups)}")

    # 控制台挑代表
    def fmt_ring(r):
        rep = f"×{r['repeat']}" if r["repeat"] > 1 else ""
        return f"{r['atomic_id']} {r['name']}{rep}"

    def show(gd, title):
        print(f"\n【{title}】组#{gd['gid']}｜成员 {gd['n_members']}｜书 {gd['books']}｜核心 {gd['cores']}")
        print("  合并骨架: " + " → ".join(fmt_ring(r) for r in gd["rings"]))

    picked = {"单弧": None, "单书多弧": None, "两书": None, "三书": None, "三书含一书多弧": None}
    for gd in out_groups:
        nb = len(gd["books"])
        if gd["n_members"] == 1 and picked["单弧"] is None and gd["cores"]:
            picked["单弧"] = gd
        if gd["n_members"] > 1 and nb == 1 and picked["单书多弧"] is None:
            picked["单书多弧"] = gd
        if gd["n_members"] > 1 and nb == 2 and picked["两书"] is None:
            picked["两书"] = gd
        if gd["n_members"] > 1 and nb >= 3 and picked["三书"] is None:
            picked["三书"] = gd
        if nb >= 3 and any(sum(1 for m in gd["members"] if m["book"] == b) >= 2 for b in gd["books"]) \
                and picked["三书含一书多弧"] is None:
            picked["三书含一书多弧"] = gd
    for k, gd in picked.items():
        if gd:
            show(gd, k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
