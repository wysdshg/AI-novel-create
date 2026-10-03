# -*- coding: utf-8 -*-
"""F4 组装演示（POC P2）：指定骨架 → 逐环取 N 路走法 → 产出可注入的「组装块」样张。

纯确定性：取数、去重、脱敏、排版全是代码，零生成式 LLM 调用；样张里每一段文字都是库内原文。

## 去重口径（2026-10-03 用户拍板，实测依据见 outputs/f4_poc/盘点报告.md §4/§5）

**两段式合并**：
1. **归一化全等合并**——去空白后同文本并为一束。必须做：两料源表面 2648 条里真实唯一走法
   只有 489 条（同一条 desc 最多在 46 处重复），且 `shaosong_singles_v1` 的 25 条模板内嵌
   324 条与 `atomic_variants` 绍宋 324 条是**同原子同文本的副本**，不按文本合并就会把绍宋
   走法双计。
2. **语义合并 sim ≥ 0.85**——bge-m3（网关 127.0.0.1:9377）现算余弦，单链接聚类。

为什么不用字面相似度：实测同原子下的真实走法两两比对，字符二元组余弦 / Jaccard /
SequenceMatcher **没有任何一对 ≥0.5**（中位 0.05~0.19），阈值从 0.7 扫到 0.95 剩余路数
完全一致——字面口径下「≥0.85 合并」退化成「只并全等」，0.85 这条线形同虚设。
bge-m3 才让这条线有意义：人工对标定同一事件不同写法 cos=0.872（该并）、不同事件
cos=0.628（该留），真实语料同原子两两中位 0.63~0.66，0.85 落在分布尾部，只切真近似重复。

embedding 不是生成式调用（不改写任何文本），任务单第 3 条已显式允许；向量落
`outputs/f4_poc/vec_cache.json`，二次运行零网络。**单测一律注入假 sim 函数**，不依赖网关。

用法（在 backend/ 下执行）：
    ..\\.venv\\Scripts\\python.exe scripts/f4_assemble.py --skeleton dfe33eee
    ..\\.venv\\Scripts\\python.exe scripts/f4_assemble.py --skeleton 越阶硬撼 --max-per-ring 8
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[2]
POC_DIR = ROOT / "outputs" / "f4_poc"
DB_RO = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"
VEC_CACHE_PATH = POC_DIR / "vec_cache.json"
PROPER_NOUN_GLOBS = ("f8_p0/proper_nouns_*.txt", "f4_poc/proper_nouns_extra.txt")

SIM_THRESHOLD = 0.85
DEFAULT_MAX_PER_RING = 8
MIN_ROUTE_CHARS = 40

# beat 标签两种形态：merge 侧「A03 群殴混战×2」，绍宋单弧侧裸号「G05」（324/666 = 49%），
# 另有 2 个空串。🔴 只用严格式会静默丢掉一半的环（2026-10-03 盘点第一版就栽在这里）。
BEAT_ATOM = re.compile(r"^([A-H]\d{2})\b")

# 脱敏占位：命中词按词类人工指定，未列到的用默认值。词表来自 盘点报告.md §6。
PLACEHOLDER_DEFAULT = "某处"
PLACEHOLDER_BY_WORD = {
    # 本次盘点抽检出的、任务单指定黑名单未覆盖的真专名
    "青铜殿": "某势力殿宇", "不死山": "某地", "神魂潭": "某地", "泰山": "某地",
    "灵墟洞": "某地", "北域神城": "某城", "仙域": "某域", "地府": "某势力",
    "北玄域": "某域", "中玄域": "某域", "血杀殿": "某势力殿宇", "光明死城": "某城",
    "庐山": "某地", "鬼水河": "某条河", "雷域": "某域",
    # 任务单指定黑名单在本次样张语料里的实际命中词
    "蓬莱": "某地", "灵族": "某族", "灵界": "某地",
}


# --------------------------------------------------------------------------
# 数据形状
# --------------------------------------------------------------------------
@dataclass
class Source:
    """一条走法的一个出处。channel 区分料源，书名用于跨书排序与「出自」行。"""
    book: str
    ref: str          # 表侧=「绍宋#5 章48」；内嵌=「弧名（c1~13）」
    channel: str      # 表侧 / 内嵌·跨书骨架 / 内嵌·单弧骨架

    @property
    def label(self) -> str:
        return f"{self.book}｜{self.ref}（{self.channel}）"


@dataclass
class Variant:
    text: str
    sources: list[Source] = field(default_factory=list)


@dataclass
class Route:
    """去重后的一「路」走法。members 是被并进同一束的全部原始走法。seq 保留首次出现序。"""
    text: str
    members: list[Variant]
    seq: int
    merged_sim: float | None = None   # 语义合并进来了别的束时的最高余弦；None=只做了全等合并

    @property
    def books(self) -> list[str]:
        out: list[str] = []
        for m in self.members:
            for s in m.sources:
                if s.book not in out:
                    out.append(s.book)
        return out

    @property
    def sources(self) -> list[Source]:
        out: list[Source] = []
        for m in self.members:
            for s in m.sources:
                if s not in out:
                    out.append(s)
        return out


@dataclass
class Ring:
    ordinal: int                  # 全局环位（1 起）
    phase: str
    atom_id: str | None
    atom_name: str
    atom_status: str              # active / candidate / 不在词表 / 无原子号
    beat_label: str
    raw_variants: list[Variant]
    ring_repeat: int = 1
    n_embedded: int = 0           # 该环自带内嵌条数（去全等前）


# --------------------------------------------------------------------------
# 去重（纯函数，单测覆盖）
# --------------------------------------------------------------------------
def norm_text(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def merge_identical(variants: Iterable[Variant]) -> list[Variant]:
    """第一段：归一化全等合并。同文本的多条并成一条，出处全部保留，顺序按首次出现。"""
    order: list[str] = []
    by_key: dict[str, Variant] = {}
    for v in variants:
        key = norm_text(v.text)
        if not key:
            continue
        if key not in by_key:
            by_key[key] = Variant(text=v.text.strip(), sources=list(v.sources))
            order.append(key)
        else:
            tgt = by_key[key]
            for s in v.sources:
                if s not in tgt.sources:
                    tgt.sources.append(s)
    return [by_key[k] for k in order]


def cosine(a: list[float], b: list[float]) -> float:
    """调用方保证向量已 L2 归一化（embedding_client 已做），此时点积即余弦。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def cluster_by_similarity(routes: list[Route], sim: Callable[[str, str], float],
                          threshold: float = SIM_THRESHOLD) -> list[Route]:
    """第二段：语义合并。单链接聚类，簇内取最长文本作代表（信息最全）。

    输入是「已做过全等合并」的 Route，同一束内成员只差空白，所以拿代表两两比对与
    全成员两两比对等价。sim 由调用方注入 —— 单测用假函数，跑样张用 bge-m3 余弦。
    """
    n = len(routes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    sims: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            s = sim(routes[i].text, routes[j].text)
            sims[(i, j)] = s
            if s >= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    out: list[Route] = []
    for root in sorted(groups, key=lambda r: min(groups[r])):
        idxs = sorted(groups[root])
        members = [m for i in idxs for m in routes[i].members]
        best = max(idxs, key=lambda i: (len(routes[i].text), -routes[i].seq))
        max_sim = max((sims[(a, b)] for x, a in enumerate(idxs) for b in idxs[x + 1:]),
                      default=None)
        out.append(Route(text=routes[best].text, members=members, seq=routes[idxs[0]].seq,
                         merged_sim=max_sim if len(idxs) > 1 else None))
    return out


def build_routes(variants: Iterable[Variant], sim: Callable[[str, str], float] | None,
                 threshold: float = SIM_THRESHOLD) -> list[Route]:
    merged = merge_identical(variants)
    routes = [Route(text=v.text, members=[v], seq=i) for i, v in enumerate(merged)]
    if sim is not None:
        routes = cluster_by_similarity(routes, sim, threshold)
    return routes


def sort_routes(routes: list[Route]) -> list[Route]:
    """跨书优选（非硬条件）：覆盖书多的排前，其次正文长的，其余保持首次出现序。"""
    return sorted(routes, key=lambda r: (-len(r.books), -len(r.text), r.seq))


# --------------------------------------------------------------------------
# 取料（只读 mode=ro）
# --------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_RO, uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_atoms(con: sqlite3.Connection) -> dict[str, dict]:
    return {r["id"]: {"name": r["name"], "status": r["status"]}
            for r in con.execute("SELECT id, name, status FROM atomic_events")}


def parse_beat(label: str) -> tuple[str | None, int]:
    """→ (原子号 or None, 重复次数)。兼容「A03 群殴混战×2」「G05」「（空）」三种形态。"""
    s = (label or "").strip()
    m = BEAT_ATOM.match(s)
    if not m:
        return None, 1
    rep = re.search(r"×(\d+)$", s)
    return m.group(1), int(rep.group(1)) if rep else 1


def load_table_variants(con: sqlite3.Connection) -> dict[str, list[Variant]]:
    """表侧 atomic_variants → {原子号: [Variant]}。"""
    out: dict[str, list[Variant]] = defaultdict(list)
    for r in con.execute("SELECT atomic_id, book_name, arc_ref, segment_no, text "
                         "FROM atomic_variants"):
        text = (r["text"] or "").strip()
        if not text:
            continue
        book = r["book_name"] or (r["arc_ref"] or "").split("#")[0]
        out[r["atomic_id"]].append(Variant(
            text=text, sources=[Source(book=book, ref=f"{r['arc_ref']} 章{r['segment_no']}",
                                       channel="表侧")]))
    return out


def load_skeleton(con: sqlite3.Connection, key: str) -> sqlite3.Row:
    """骨架定位：id 前缀（≥4 位）优先，其次名称包含；歧义时报候选。"""
    rows = list(con.execute("SELECT id, name, logline, structure, source_stats FROM plot_templates "
                            "WHERE scale='arc' AND status='active'"))
    hit = [r for r in rows if r["id"].startswith(key)] or [r for r in rows if key in r["name"]]
    if not hit:
        raise SystemExit(f"❌ 没找到骨架「{key}」（active arc 模板 {len(rows)} 条）。"
                         f"给 id 前缀（≥4 位）或名称片段。")
    if len(hit) > 1:
        raise SystemExit("❌ 「%s」匹配到 %d 条骨架，请给更长的前缀或更准的名称：\n  %s"
                         % (key, len(hit),
                            "\n  ".join(f"{r['id'][:8]}  {r['name']}" for r in hit[:12])))
    return hit[0]


def load_rings(row: sqlite3.Row, table_variants: dict[str, list[Variant]],
               atoms: dict[str, dict]) -> list[Ring]:
    """逐环取走法：该环内嵌（骨架自带的环对齐实证）∪ 表侧同原子全部。

    2026-10-03 拍板合并两料源（后果已知：绍宋的历史/军事走法会进玄幻环）。
    """
    structure = json.loads(row["structure"])
    origin = (json.loads(row["source_stats"]) or {}).get("origin") or ""
    channel = {"atomic_merge_v1": "内嵌·跨书骨架",
               "shaosong_singles_v1": "内嵌·单弧骨架"}.get(origin, f"内嵌·{origin}")
    rings: list[Ring] = []
    for ph in structure.get("phases", []):
        for b in ph.get("beats", []):
            atom_id, rep = parse_beat(b.get("beat"))
            emb: list[Variant] = []
            for v in b.get("variants", []):
                desc = (v.get("desc") or "").strip()
                if desc:
                    emb.append(Variant(text=desc, sources=[Source(
                        book=v.get("src") or "?", ref=v.get("how") or "?", channel=channel)]))
            info = atoms.get(atom_id) if atom_id else None
            if atom_id is None:
                status = "无原子号"
            elif info is None:
                status = "不在词表"
            else:
                status = info["status"]
            rings.append(Ring(
                ordinal=len(rings) + 1, phase=ph.get("phase") or "", atom_id=atom_id,
                atom_name=(info or {}).get("name", "") or (b.get("beat") or "").strip(),
                atom_status=status, beat_label=(b.get("beat") or "").strip(),
                raw_variants=emb + table_variants.get(atom_id, []),
                ring_repeat=rep, n_embedded=len(emb)))
    return rings


# --------------------------------------------------------------------------
# 专名核账
# --------------------------------------------------------------------------
def load_blacklist() -> dict[str, str]:
    """词 → 占位词。任务单指定 3 份 f8_p0/proper_nouns_*.txt ＋ 本次盘点补充清单。"""
    words: set[str] = set()
    for pat in PROPER_NOUN_GLOBS:
        for p in sorted((ROOT / "outputs").glob(pat)):
            for w in p.read_text(encoding="utf-8").splitlines():
                w = w.strip()
                if w and not w.startswith("#"):
                    words.add(w)
    return {w: PLACEHOLDER_BY_WORD.get(w, PLACEHOLDER_DEFAULT) for w in words}


def scrub(text: str, blacklist: dict[str, str]) -> tuple[str, list[str]]:
    """命中专名 → 脱敏替换（长词优先，避免子串抢先替换）。返回（脱敏后文本，命中词列表）。"""
    hits: list[str] = []
    out = text
    for w in sorted(blacklist, key=len, reverse=True):
        if w in out:
            out = out.replace(w, blacklist[w])
            hits.append(w)
    return out, hits


def find_proper_nouns(text: str, blacklist: dict[str, str]) -> list[str]:
    return [w for w in blacklist if w in text]


# --------------------------------------------------------------------------
# 相似度提供者（bge-m3 via 网关；向量本地缓存，二次运行零调用）
# --------------------------------------------------------------------------
def _decode_cfg(raw: str):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def load_vector_cache(path: Path = VEC_CACHE_PATH) -> dict[str, list[float]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def ensure_vectors(texts: list[str], cache: dict[str, list[float]],
                   cache_path: Path = VEC_CACHE_PATH) -> dict:
    """把没缓存的唯一文本向量化（网关或硅基直连），写回缓存。"""
    todo = [t for t in dict.fromkeys(texts) if t not in cache]
    stats = {"n_texts": len(set(texts)), "n_cached": len(set(texts)) - len(todo),
             "n_called": len(todo), "endpoint": "cache"}
    if not todo:
        return stats
    sys.path.insert(0, str(ROOT / "backend"))
    from app.services import embedding_client as ec  # noqa: PLC0415
    con = connect()
    cfg = {r["key"]: _decode_cfg(r["value"]) for r in con.execute("SELECT key, value FROM app_configs")}
    con.close()
    gkey = str(cfg.get("llm.gateway_key") or "").strip()
    if cfg.get("llm.use_gateway") and gkey:
        base, key, stats["endpoint"] = ec.GATEWAY_BASE, gkey, "网关"
    else:
        base, key, stats["endpoint"] = ec.BASE, ec.get_api_key(), "硅基直连"
    if not key:
        raise SystemExit("❌ 需要 embedding 但没有可用 key（app_configs llm.gateway_key / "
                         "retrieval.siliconflow_key / env NA_SILICONFLOW_KEY）。"
                         "离线降级请用 --exact-only。")
    for i in range(0, len(todo), ec._MAX_BATCH):  # noqa: SLF001
        batch = todo[i:i + ec._MAX_BATCH]
        for t, v in zip(batch, ec._post_embeddings(base, batch, key)):
            cache[t] = v
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return stats


def make_embedding_sim(cache: dict[str, list[float]]) -> Callable[[str, str], float]:
    """注入式 sim：查归一化文本的缓存向量算余弦。缺向量返回 0（永不合并，不会误并）。"""
    by_norm = {norm_text(k): v for k, v in cache.items()}

    def sim(a: str, b: str) -> float:
        return cosine(by_norm.get(norm_text(a), []), by_norm.get(norm_text(b), []))

    return sim


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
@dataclass
class Audit:
    scrubbed: int = 0
    dropped: int = 0
    hidden: int = 0
    sem_clusters: int = 0
    exact_clusters: int = 0
    injected_texts: list[str] = field(default_factory=list)
    emb: dict = field(default_factory=dict)


def render_route(num: int, route: Route, blacklist: dict[str, str],
                 audit: Audit) -> list[str]:
    text, hits = scrub(route.text, blacklist)
    if hits:
        audit.scrubbed += 1
    if find_proper_nouns(text, blacklist) or len(text) < MIN_ROUTE_CHARS:
        audit.dropped += 1
        return []
    audit.injected_texts.append(text)
    srcs = route.sources
    lines = [f"**路 {num}**（{len(text)} 字｜{len(route.books)} 书："
             f"{'、'.join(route.books)}）", "", f"> {text}", ""]
    for s in srcs[:6]:
        lines.append(f"- 出自：{s.label}")
    if len(srcs) > 6:
        lines.append(f"- 出自：另有 {len(srcs) - 6} 处同文本出处")
    if route.merged_sim is not None:
        lines.append(f"- 去重：{len(route.members)} 条近似走法并为一路"
                     f"（最高余弦 {route.merged_sim:.3f} ≥ {SIM_THRESHOLD}，共 {len(srcs)} 处出处）")
        lines.append("- 合并明细：")
        for m in route.members:
            tag = " ← 本路代表" if m.text.strip() == route.text.strip() else ""
            who = m.sources[0]
            lines.append(f"  - {m.text[:60]}…｜{who.book}｜{who.ref}{tag}")
    elif len(srcs) > 1:
        lines.append(f"- 去重：同一条走法在库内有 {len(srcs)} 处出处"
                     f"（归一化全等合并，不计为多路）")
    lines.append("")
    return lines


def render(row: sqlite3.Row, rings: list[Ring], routes_per_ring: list[list[Route]],
           blacklist: dict[str, str], max_per_ring: int, audit: Audit) -> str:
    ss = json.loads(row["source_stats"])
    books_all: list[str] = []
    for rs in routes_per_ring:
        for r in rs:
            for bk in r.books:
                if bk not in books_all:
                    books_all.append(bk)
    path_total = sum(len(r) for r in routes_per_ring)
    raw_total = sum(len(ring.raw_variants) for ring in rings)
    sem_by_ring = [sum(1 for r in routes if r.merged_sim is not None) for routes in routes_per_ring]
    exact_by_ring = [sum(1 for r in routes if r.merged_sim is None and len(r.sources) > 1)
                     for routes in routes_per_ring]
    audit.sem_clusters = sum(sem_by_ring)
    audit.exact_clusters = sum(exact_by_ring)
    sem_detail: list[tuple[Ring, Route]] = []

    out = [f"# 组装样张 · {row['name']}", "",
           f"> F4 组装演示（POC P2）｜骨架 id `{row['id']}`｜"
           f"生成方式：纯确定性取数＋去重＋排版，零生成式 LLM 调用", "",
           f"- **一句话**：{row['logline'] or '（库内无 logline）'}",
           f"- **骨架来源**：{ss.get('origin')}｜{ss.get('books')} 书 "
           f"{ss.get('n_members')} 条弧实证｜{ss.get('n_references', '—')} 次引用",
           f"- **环数**：{len(rings)}（重复推进环 {sum(1 for r in rings if r.ring_repeat > 1)} 个；"
           f"非 active 原子环 {sum(1 for r in rings if r.atom_status != 'active')} 个）",
           f"- **各路覆盖**：{sum(1 for r in routes_per_ring if r)}/{len(rings)} 环有走法，"
           f"其中 **{sum(1 for r in routes_per_ring if len(r) >= 2)} 环 ≥2 路**；"
           f"合并后共 **{path_total} 路**（原始素材 {raw_total} 条）",
           f"- **去重成效**：原始 {raw_total} 条 → 全等重复合并 {audit.exact_clusters} 束"
           f"（同一条走法在库内最多 {max([len(r.sources) for rs in routes_per_ring for r in rs] or [1])} 处重复）"
           f" → 语义合并 {audit.sem_clusters} 束 → **{path_total} 路**",
           f"- **涉及书**：{len(books_all)} 本（{'、'.join(books_all)}）",
           f"- **去重口径**：两段式 —— ① 归一化全等合并 ② bge-m3 余弦 ≥{SIM_THRESHOLD} 语义合并"
           f"（{audit.emb.get('endpoint', '网关')}：唯一文本 {audit.emb.get('n_texts', 0)} 条 / "
           f"缓存命中 {audit.emb.get('n_cached', 0)} / 本次新调用 {audit.emb.get('n_called', 0)}）",
           f"- **每环展示上限**：{max_per_ring} 路（超出者折叠计数，不丢弃）", "",
           "---", ""]

    for i, (ring, routes) in enumerate(zip(rings, routes_per_ring)):
        sem_here, exact_here = sem_by_ring[i], exact_by_ring[i]
        title = f"## {ring.phase} · 环{ring.ordinal}｜{ring.beat_label or '（beat 标签为空）'}"
        if ring.atom_status != "active":
            title += f" ⚠️ 原子状态={ring.atom_status}"
        out += [title, ""]
        if not routes:
            out += ["**本环暂无走法素材**（两料源均无该原子走法——显式标注，不静默省略）", ""]
            continue
        out.append(f"本环合并后 **{len(routes)} 路**（原始 {len(ring.raw_variants)} 条 = "
                   f"环内自带实证 {ring.n_embedded} 条 + 同原子其他 {len(ring.raw_variants) - ring.n_embedded} 条）"
                   f"，其中 **语义合并 {sem_here} 束**、全等重复合并 {exact_here} 束")
        out.append("")
        num = 0
        dropped_before = audit.dropped
        for r in routes[:max_per_ring]:
            num += 1
            out += render_route(num, r, blacklist, audit)
        for r in routes:
            if r.merged_sim is not None:
                sem_detail.append((ring, r))
        dropped_here = audit.dropped - dropped_before
        hidden = max(0, len(routes) - max_per_ring)
        audit.hidden += hidden
        notes = []
        if hidden:
            notes.append(f"本环另有 {hidden} 路未展开（受每环展示上限约束，未丢弃）")
        if dropped_here:
            notes.append(f"本环 {dropped_here} 路因专名脱敏不净或正文 <{MIN_ROUTE_CHARS} 字被剔除")
        if notes:
            out += [f"> {'；'.join(notes)}", ""]

    if sem_detail:
        out += ["---", "", "## 附 · 语义合并明细（sim ≥ 0.85 实际并掉了什么）", "",
                f"共 {len(sem_detail)} 束。这一节不受每环展示上限约束，"
                "用来核对「去重键=走法相似度」有没有切错。", ""]
        for ring, r in sem_detail:
            out += [f"**环{ring.ordinal} {ring.beat_label}**"
                    f"（并为一路，最高余弦 {r.merged_sim:.3f}）", ""]
            for m in r.members:
                who = m.sources[0]
                tag = " ← 本路代表" if m.text.strip() == r.text.strip() else ""
                out.append(f"- {m.text[:70]}…｜{who.book}｜{who.ref}{tag}")
            out.append("")
    return "\n".join(out) + "\n"


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:40]


# --------------------------------------------------------------------------
# 组装入口（CLI 与单测/复用共用）
# --------------------------------------------------------------------------
def assemble(skeleton_key: str, max_per_ring: int = DEFAULT_MAX_PER_RING,
             threshold: float = SIM_THRESHOLD, exact_only: bool = False,
             out_dir: Path | None = None, verbose: bool = True) -> tuple[Path, Audit, dict]:
    con = connect()
    atoms = load_atoms(con)
    table_variants = load_table_variants(con)
    row = load_skeleton(con, skeleton_key)
    rings = load_rings(row, table_variants, atoms)
    con.close()

    blacklist = load_blacklist()
    if verbose:
        print(f"骨架「{row['name']}」{len(rings)} 环｜"
              f"表侧走法 {sum(len(v) for v in table_variants.values())} 条｜"
              f"黑名单 {len(blacklist)} 词")

    texts = [v.text for ring in rings for v in ring.raw_variants]
    if exact_only:
        sim, emb = None, {"n_texts": 0, "n_cached": 0, "n_called": 0, "endpoint": "--exact-only"}
    else:
        cache = load_vector_cache()
        emb = ensure_vectors(texts, cache)
        sim = make_embedding_sim(cache)
    if verbose:
        print(f"  embedding：唯一文本 {emb['n_texts']} 条｜缓存命中 {emb['n_cached']}｜"
              f"新调用 {emb['n_called']}｜端点 {emb['endpoint']}")

    routes_per_ring = [sort_routes(build_routes(ring.raw_variants, sim, threshold)) for ring in rings]
    audit = Audit(emb=emb)
    md = render(row, rings, routes_per_ring, blacklist, max_per_ring, audit)

    out_dir = Path(out_dir or POC_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"组装样张_{safe_filename(row['name'])}.md"

    residual = {w for line in audit.injected_texts for w in find_proper_nouns(line, blacklist)}
    md += ("\n---\n\n## 核账\n\n"
           f"- 注入正文段数：{len(audit.injected_texts)}（每段都是库内原文，"
           f"仅专名按占位词脱敏）\n"
           f"- 专名脱敏：{audit.scrubbed} 路命中并替换；"
           f"因脱敏不净或正文 <{MIN_ROUTE_CHARS} 字被剔除：{audit.dropped} 路\n"
           f"- 去重：全等重复合并 {audit.exact_clusters} 束 ＋ 语义（余弦 ≥{SIM_THRESHOLD}）"
           f"合并 {audit.sem_clusters} 束\n"
           f"- 折叠未展示：{audit.hidden} 路（受每环展示上限约束）\n"
           f"- 黑名单词数：{len(blacklist)}"
           f"（任务单指定 outputs/f8_p0/proper_nouns_*.txt ＋ 本次盘点补充 "
           f"outputs/f4_poc/proper_nouns_extra.txt）\n"
           f"- 终核 grep：注入正文残留专名 {len(residual)} 词 → "
           f"{'0 命中 ✅' if not residual else '❌ ' + str(sorted(residual))}\n")
    out_path.write_text(md, encoding="utf-8")
    summary = {
        "path": str(out_path.relative_to(ROOT)),
        "n_rings": len(rings),
        "n_covered": sum(1 for r in routes_per_ring if r),
        "n_multi": sum(1 for r in routes_per_ring if len(r) >= 2),
        "n_paths": sum(len(r) for r in routes_per_ring),
        "scrubbed": audit.scrubbed, "dropped": audit.dropped, "hidden": audit.hidden,
        "sem_clusters": audit.sem_clusters, "exact_clusters": audit.exact_clusters,
        "residual": sorted(residual), "emb": emb,
    }
    if verbose:
        print(f"✅ 样张 → {summary['path']}")
        print(f"   环路覆盖 {summary['n_covered']}/{summary['n_rings']}｜≥2 路 {summary['n_multi']} 环｜"
              f"共 {summary['n_paths']} 路｜全等合并 {summary['exact_clusters']} 束｜"
              f"语义合并 {summary['sem_clusters']} 束")
        print(f"   专名：脱敏 {summary['scrubbed']} 条｜剔除 {summary['dropped']} 条｜"
              f"折叠未展示 {summary['hidden']} 路｜"
              f"注入正文 {len(audit.injected_texts)} 段，黑名单残留 {len(residual)} 词 → "
              f"{'0 命中 ✅' if not residual else '❌ ' + str(sorted(residual))}")
    return out_path, audit, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="F4 组装演示：骨架逐环 N 路走法样张（零生成式 LLM）")
    ap.add_argument("--skeleton", required=True, help="骨架 id 前缀（≥4 位）或名称片段")
    ap.add_argument("--max-per-ring", type=int, default=DEFAULT_MAX_PER_RING)
    ap.add_argument("--threshold", type=float, default=SIM_THRESHOLD)
    ap.add_argument("--exact-only", action="store_true",
                    help="只做归一化全等合并，不调 embedding（离线降级口径）")
    ap.add_argument("--out-dir", default=str(POC_DIR))
    args = ap.parse_args()
    _, _, summary = assemble(args.skeleton, args.max_per_ring, args.threshold,
                             args.exact_only, Path(args.out_dir))
    return 0 if not summary["residual"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
