# -*- coding: utf-8 -*-
"""[DEV-P3] A/B 客观对比：只出**可数的机械事实**与短引用，不打分（评分留给用户）。

口径：
  · 注入骨架 = 模板《擂台大比--宗门擂台比试》的 3 拍（A02 擂台比试 / A05 越阶硬撼 / C03 拜师结盟）
  · 每拍取一组**该拍语义的可grep 关键词**，统计正文里出现与否 + 首次出现位置（占全文百分比）
    —— 用来回答「这一拍在正文里有没有对应物、出现在哪」，而不是「写得好不好」
  · 章末收束：正文最后 60 字里是否出现「下一场/还有/未完」这类悬置标记，或句末是否断在动作中
引用一律 ≤50 字。
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "p3_inject"
DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")

# 每拍的语义关键词（从该拍 v4 走法概括里抽的实词，不是拍标签本身 —— 标签不会进正文）
BEAT_PROBES = {
    "A02 擂台比试": ["擂台", "上台", "对手", "第一轮"],
    "A05 越阶硬撼": ["长老", "灰袍", "法杖", "符纸", "护体灵气", "震得"],
    "C03 拜师结盟": ["静室", "玉简", "法则", "跟我走", "召见", "秘法"],
}
HANGING = ("下一场", "还有三轮", "还没完", "下一张", "刀尖指向", "未完")


def load_body(p: Path) -> str:
    """样张 md 里 `---` 之后的部分才是正文。"""
    txt = p.read_text(encoding="utf-8")
    return txt.split("\n---\n", 1)[1] if "\n---\n" in txt else txt


def analyze(name: str, body: str) -> dict:
    n = len(body)
    paras = [x for x in body.split("\n\n") if x.strip()]
    out = {
        "样张": name, "字数": n, "段落数": len(paras),
        "首段": paras[0][:50] if paras else "",
        "末段": paras[-1][:50] if paras else "",
        "拍对应": {},
    }
    for beat, kws in BEAT_PROBES.items():
        hits = {}
        for k in kws:
            c = body.count(k)
            if c:
                hits[k] = {"次数": c, "首现位置": f"{body.find(k) * 100 // max(1, n)}%"}
        out["拍对应"][beat] = {"命中关键词": hits, "命中数": len(hits)}
    tail = body[-60:]
    out["章末60字"] = tail
    out["章末有悬置标记"] = [k for k in HANGING if k in tail]
    out["章末是否以句号收束"] = bool(re.search(r"[。！？]\s*$", body.strip()))
    return out


def main() -> int:
    L: list[str] = []

    def w(*a):
        s = " ".join(str(x) for x in a)
        L.append(s)
        print(s.encode("gbk", "replace").decode("gbk"))

    rec = json.loads((OUT / "路径B_记录.json").read_text(encoding="utf-8"))
    w(f"源模板：{rec['template']}")
    w(f"注入拍序列：{rec['beat_seq']}")
    w(f"骨架块 {rec['skeleton_chars']} 字；两组字数："
      f"A={rec['results']['A']['chars']} B={rec['results']['B']['chars']}")
    w(f"两组耗时：A={rec['results']['A']['sec']}s B={rec['results']['B']['sec']}s")

    res = {}
    for tag, fn in (("A_注入", "样张A_注入.md"), ("B_无注入", "样张B_无注入.md")):
        body = load_body(OUT / fn)
        res[tag] = analyze(tag, body)

    w("\n================ A/B 机械事实 ================")
    for tag in ("A_注入", "B_无注入"):
        r = res[tag]
        w(f"\n【{tag}】{r['字数']} 字 / {r['段落数']} 段")
        w(f"  首段（≤50字）：{r['首段']}")
        w(f"  末段（≤50字）：{r['末段']}")
        w(f"  章末以句号收束：{r['章末是否以句号收束']}｜章末悬置标记：{r['章末有悬置标记'] or '无'}")
        for beat, d in r["拍对应"].items():
            ks = d["命中关键词"]
            summ = "、".join(f"{k}×{v['次数']}@{v['首现位置']}" for k, v in ks.items())
            w(f"    {beat:14s} 命中 {d['命中数']} 类：{summ or '（无）'}")

    # 书级上下文串扰检查：A 的计划角色有没有漏进 B
    con = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bA = load_body(OUT / "样张A_注入.md")
    bB = load_body(OUT / "样张B_无注入.md")
    plan = json.loads((OUT / "计划表_A.json").read_text(encoding="utf-8"))
    cast = sorted({c for ln in plan["lines"] for c in (ln.get("new_chars") or [])
                   if not str(c).endswith("）")})
    w("\n---- 串扰检查：路径A 计划角色名是否也出现在 B 组正文 ----")
    shared = []
    for c in cast:
        c = str(c).split("（")[0]
        if c and c in bB:
            shared.append(c)
            w(f"  ⚠️ {c}：A 组 {bA.count(c)} 次 / B 组 {bB.count(c)} 次（书级上下文共享）")
    if not shared:
        w("  无")
    w(f"\n  A 组角色名 {cast}")
    con.close()

    (OUT / "AB_机械事实.json").write_text(json.dumps(
        {"template": rec["template"], "beat_seq": rec["beat_seq"],
         "skeleton_chars": rec["skeleton_chars"],
         "results_meta": rec["results"], "analysis": res,
         "shared_names_AB": shared, "pathA_cast": cast},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "AB_机械事实.txt").write_text("\n".join(L), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())