# -*- coding: utf-8 -*-
"""[DEV-P3] 路径 B：章节上下文直注入 A/B（同书、同模型、同参数，单变量=有没有骨架块）。

A 组：prompt_hint 前置 ≤300 字紧凑骨架块 + 「按此骨架写，节奏对齐拍序列」
B 组：prompt_hint 只有同款写作指令，**不含任何骨架内容**
两样张各落一份 md，事件/配置/耗时全记录，供报告逐条比对。
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "p3_inject"
OUT.mkdir(parents=True, exist_ok=True)
API = "http://127.0.0.1:8000/api/v1"
BOOK = "P3注入实测"
TPL_NAME = "擂台大比--宗门擂台比试"
# 两组共用的写作指令（**唯一差异就是有没有骨架块**）
# ⚠️ 跨行拼接字符串必须带括号，否则第二行会被当成新语句 → IndentationError
WRITE_INSTR = (
    "写沈砚在宗门外门大比擂台上一战成名的第 1 章：写清他如何被刁难、如何报名、"
    "首轮如何险胜，章末留钩子。"
)

L: list[str] = []


def w(*a):
    s = " ".join(str(x) for x in a)
    L.append(s)
    print(s.encode("gbk", "replace").decode("gbk"))


def req(method: str, path: str, body: dict | None = None, *, timeout: int = 900):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(r, timeout=timeout) as resp:
            env = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[HTTP {e.code}] {method} {path}\n  detail="
                         f"{e.read().decode('utf-8', 'replace')[:600]}")
    if env.get("code") not in (0, None):
        raise SystemExit(f"[API code={env.get('code')}] {method} {path} → {env.get('message')}")
    return env.get("data")


def as_items(d) -> list:
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("items", "projects", "volumes", "articles", "chapters", "templates"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def sse_generate(path: str, body: dict) -> tuple[str, dict]:
    r = urllib.request.Request(API + path,
                              data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              method="POST",
                              headers={"Content-Type": "application/json",
                                       "Accept": "text/event-stream"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    txt, events, saved = "", [], {}
    with op.open(r, timeout=1800) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8", "replace")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                kind, payload = None, None
                for ln in frame.splitlines():
                    if ln.startswith("event:"):
                        kind = ln[6:].strip()
                    elif ln.startswith("data:"):
                        try:
                            payload = json.loads(ln[5:].strip())
                        except Exception:
                            payload = None
                if not kind:
                    continue
                events.append(kind)
                p = payload if isinstance(payload, dict) else {}
                if kind == "chunk":
                    txt += str(p.get("text") or "")
                elif kind in ("saved", "done", "error", "stopped", "validate"):
                    saved[kind] = p
    return txt, {"events": events, "n_chunk": events.count("chunk"), "saved": saved}


def skeleton_block(tpl: dict, limit: int = 300) -> tuple[str, list]:
    """拍序列 + 每拍一句走法，压成 ≤limit 字的紧凑骨架块。

    走法取该拍第一个 variant 的 desc 首句（v4 库里每拍 1 个变体 = 唯一实证走法）。
    """
    beats = [(ph.get("phase"), b) for ph in (tpl["structure"].get("phases") or [])
             for b in (ph.get("beats") or [])]
    seq = " → ".join((b.get("beat") or "").split("×")[0].strip() for _, b in beats)
    rows = []
    head = f"骨架《{tpl['name']}》：{seq}。逐拍走法："
    budget = limit - len(head)
    per = max(18, budget // max(1, len(beats)))
    for ph, b in beats:
        vs = b.get("variants") or []
        desc = (vs[0].get("desc") if vs else "") or ""
        first = desc.replace("｜", "。").split("。")[0][:per]
        rows.append(f"{b.get('beat')}={first}")
    block = head + "；".join(rows)
    return block[:limit], [b.get("beat") for _, b in beats]


def main() -> int:
    w(f"=== P3 路径B · 章节上下文直注入 A/B @ {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n")

    # ---------- 1) 找测试书 ----------
    proj = next((p for p in as_items(req("GET", "/projects")) if p.get("name") == BOOK), None)
    if proj is None:
        raise SystemExit("测试书不存在 —— 先跑 p3_path_a.py")
    pid = proj["id"]
    vid = as_items(req("GET", f"/projects/{pid}/volumes"))[0]["id"]
    w(f"测试书 {BOOK}  project={pid}  volume={vid}")

    # ---------- 2) 取模板骨架 ----------
    w(f"\n--- [B1] 取模板《{TPL_NAME}》骨架 ---")
    lst = as_items(req("GET", "/plot-templates?scale=arc&status=active&page_size=2000"))
    tpl = next((t for t in lst if t.get("name") == TPL_NAME), None)
    if tpl is None:
        raise SystemExit(f"模板 {TPL_NAME} 未找到")
    tpl = req("GET", f"/plot-templates/{tpl['id']}")
    st = tpl.get("structure") or {}
    n_beats = sum(len(ph.get("beats") or []) for ph in (st.get("phases") or []))
    w(f"  scale={tpl.get('scale')} status={tpl.get('status')} beats={n_beats} "
      f"display_top={st.get('display_top')} 成员={len((tpl.get('source_stats') or {}).get('member_arcs') or [])}")
    block, beat_seq = skeleton_block(tpl)
    w(f"  拍序列：{beat_seq}")
    w(f"  骨架块（{len(block)} 字）：{block}")
    (OUT / "骨架块.md").write_text(
        f"# 路径B 注入用骨架块（{len(block)} 字，源模板 {TPL_NAME}）\n\n```\n{block}\n```\n",
        encoding="utf-8")

    # ---------- 3) 两组各建一篇，各自生成第 1 章 ----------
    results = {}
    for tag, art_name, hint in (
        ("A", "第二篇_注入", f"{block}\n\n【按此骨架写第 1 章，节奏对齐拍序列】\n{WRITE_INSTR}"),
        ("B", "第三篇_无注入", WRITE_INSTR),
    ):
        w(f"\n--- [B{tag}] 组{tag}（{'注入骨架' if tag == 'A' else '无注入'}）---")
        aid = req("POST", f"/projects/{pid}/volumes/{vid}/articles",
                  {"volume_id": vid, "name": art_name, "summary": "", "sort_order": 0})["id"]
        body = {"chapter_no": 1, "article_id": aid, "prompt_hint": hint,
                "from_discussion": False, "word_range": {"min": 2600, "max": 3000},
                "temperature": 0.4, "ingest_level": "lite"}
        t0 = time.time()
        txt, ev = sse_generate(f"/projects/{pid}/chapters/generate", body)
        dt = time.time() - t0
        saved = ev["saved"].get("saved") or ev["saved"].get("done") or {}
        w(f"  article={aid}  耗时 {dt:.1f}s  正文 {len(txt)} 字  "
          f"chunk={ev['n_chunk']}  saved={saved}")
        if ev["saved"].get("error"):
            w(f"  ⚠️ error：{ev['saved']['error']}")
        name = "样张A_注入.md" if tag == "A" else "样张B_无注入.md"
        (OUT / name).write_text(
            f"# 样张{tag}（{'注入骨架' if tag == 'A' else '无注入'}）\n\n"
            f"- 测试书：{BOOK}｜篇：{art_name}\n"
            f"- 源模板：{TPL_NAME}（注入组用）/ 无（对照组）\n"
            f"- prompt_hint 字数：{len(hint)}\n"
            f"- 生成耗时 {dt:.1f}s｜正文 {len(txt)} 字｜SSE chunk {ev['n_chunk']}\n"
            f"- 落库：{saved}\n\n"
            f"## 注入的 prompt_hint 原文\n\n```\n{hint}\n```\n\n---\n\n{txt}\n",
            encoding="utf-8")
        results[tag] = {"article_id": aid, "chars": len(txt), "sec": round(dt, 1),
                        "hint_chars": len(hint), "saved": saved}
        w(f"  已写 {name}")

    (OUT / "路径B_记录.json").write_text(json.dumps(
        {"template": TPL_NAME, "beat_seq": beat_seq, "skeleton_block": block,
         "skeleton_chars": len(block), "write_instr": WRITE_INSTR, "results": results},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "路径B_日志.txt").write_text("\n".join(L), encoding="utf-8")
    w("\n完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())