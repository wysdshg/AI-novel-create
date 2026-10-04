# -*- coding: utf-8 -*-
"""[DEV-P3] 路径 A：篇规划链实测（现网消费端，全程走 HTTP 8000，零代码改动）。

只建临时测试书「P3注入实测」，绝不碰现役书；不写 plot_templates / event_skeletons。

🔴 三个踩过的坑（都写进代码注释，别再犯）：
  1. 响应信封是 `{code,message,data,trace_id}`，**列表在 `data.items`**（不是 data 本身）。
  2. `ArticleCreate.volume_id` 既在路径里也**必填在 body 里**，缺了就 422。
  3. SSE 帧是 `event: <名>\\ndata: <json>\\n\\n` —— 事件名在 **`event:` 行**，
     载荷里没有；且必须按 `\\n\\n` 切帧，逐行读会粘帧丢事件。
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
HINT = "主角在宗门比试擂台上一战成名夺魁"
BOOK = "P3注入实测"
SETTING = ("古典修仙。少年沈砚出身没落世家，灵根驳杂，被逐出家门后流落青石城，在坊市摆摊卖符为生。"
           "他有一位收废纸的哑巴老仆阿桑，和一枚会说话的血玉残片。宗门每三年一次外门大比，"
           "胜者可入内门、得功法与丹药，败者遣返。沈砚目标是在这一届大比上夺魁，"
           "为病重的母亲争一味续命丹。")

L: list[str] = []


def w(*a):
    s = " ".join(str(x) for x in a)
    L.append(s)
    print(s.encode("gbk", "replace").decode("gbk"))


def req(method: str, path: str, body: dict | None = None, *, timeout: int = 900):
    """调 API；拆 `{code,message,data,trace_id}` 信封，出错把 detail 原文打出来。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(r, timeout=timeout) as resp:
            env = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[HTTP {e.code}] {method} {path}\n  body={body}\n"
                         f"  detail={e.read().decode('utf-8', 'replace')[:600]}")
    if env.get("code") not in (0, None):
        raise SystemExit(f"[API code={env.get('code')}] {method} {path} → {env.get('message')}")
    return env.get("data")


def as_items(d) -> list:
    """列表端点：data 可能是 list，也可能是 {items:[...]}。"""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("items", "projects", "volumes", "articles", "chapters", "templates"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def sse_generate(path: str, body: dict) -> tuple[str, dict]:
    """消费 SSE 流，攒正文与事件序列（帧格式见 app/core/response.py::sse_event）。"""
    r = urllib.request.Request(API + path,
                              data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              method="POST",
                              headers={"Content-Type": "application/json",
                                       "Accept": "text/event-stream"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    txt, events, saved, thinking = "", [], {}, ""
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
                elif kind == "thinking":
                    thinking += str(p.get("text") or "")
                elif kind in ("saved", "done", "error", "stopped", "validate", "context"):
                    saved[kind] = p
    return txt, {"events": events, "n_chunk": events.count("chunk"),
                 "n_thinking": events.count("thinking"), "saved": saved,
                 "thinking_head": thinking[:200]}


def main() -> int:
    w(f"=== P3 路径A · 规划链实测 @ {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n")

    # ---------- 0) 清掉上次遗留的同名测试书（幂等；只按名字删，绝不碰别的书）----------
    w("--- [A0] 清理上次遗留的同名测试书 ---")
    for p in as_items(req("GET", "/projects")):
        if p.get("name") == BOOK:
            req("DELETE", f"/projects/{p['id']}")
            w(f"  已删残留 {p['id']} ({BOOK})")

    # ---------- 1) 检索质量记录（只读，与 plan_crud._templates_for_plan 同一调用）----------
    w(f"\n--- [A1] 口述检索记录（scale=arc, top_k=4，与 _templates_for_plan 同调用）---")
    sr = req("POST", "/plot-templates/search", {"query": HINT, "scale": "arc", "top_k": 4})
    hits = as_items(sr)
    w(f"  mode={sr.get('mode')}  命中 {len(hits)} 支")
    rec = []
    for i, h in enumerate(hits, 1):
        mb = h.get("matched_beats") or []
        w(f"  top{i}: {h['name']}  score={h.get('_score')}  matched_beats={len(mb)}")
        for b in mb[:3]:
            w(f"        · {b.get('phase')}·{b.get('beat')}")
        rec.append({"rank": i, "name": h["name"], "id": h["id"], "score": h.get("_score"),
                    "n_matched_beats": len(mb),
                    "matched_beats": [{"phase": b.get("phase"), "beat": b.get("beat"),
                                       "n_variants": len(b.get("variants") or [])}
                                      for b in mb]})
    (OUT / "检索记录_A.json").write_text(
        json.dumps({"hint": HINT, "mode": sr.get("mode"), "top4": rec},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 2) 建临时测试书 ----------
    w("\n--- [A2] 建临时测试书（1 卷 1 篇）---")
    pid = req("POST", "/projects", {"name": BOOK, "genre": "修仙", "summary": SETTING,
                                    "status": "draft", "db_backend": "sqlite"})["id"]
    w(f"  project  {pid}  {BOOK}")
    vid = req("POST", f"/projects/{pid}/volumes", {"name": "第一卷", "sort_order": 0})["id"]
    w(f"  volume   {vid}  第一卷")
    aid = req("POST", f"/projects/{pid}/volumes/{vid}/articles",
              {"volume_id": vid, "name": "第一篇",
               "summary": "沈砚在大比前的最后一个月，靠摆摊卖符与街头博弈凑齐报名费，"
                          "并摸清对手底细；大比当日一路连胜夺魁。",
               "sort_order": 0})["id"]
    w(f"  article  {aid}  第一篇")

    # ---------- 3) 触发规划生成 ----------
    w(f"\n--- [A3] 规划生成（hint={HINT!r}, n_chapters=8）---")
    t0 = time.time()
    gd = req("POST", f"/projects/{pid}/articles/{aid}/plan/generate",
             {"hint": HINT, "n_chapters": 8})
    w(f"  耗时 {time.time() - t0:.1f}s")
    # 🔴 `_finalize_plan` 返回的是**汇总**不是计划体：plan_id / lines(行数!int) /
    #    templates(名字列表) / unknown_chars / blocked_dead / carryover / population_hint …
    w(f"  plan_id={gd.get('plan_id')}  行数={gd.get('lines')}  templates={gd.get('templates')}")
    w(f"  unknown_chars={gd.get('unknown_chars')}  blocked_dead={gd.get('blocked_dead')}  "
      f"population_hint={gd.get('population_hint')!r}")

    # ---------- 4) 抽查计划表：模板痕迹 ----------
    w("\n--- [A4] 计划表：模板痕迹抽查---")
    # 🔴 `plan_crud.get_plan` 返回**扁平**结构：lines 在顶层，不在 plan 键下
    pld = req("GET", f"/projects/{pid}/articles/{aid}/plan")
    lines = pld.get("lines") or []
    w(f"  status={pld.get('status')}  origin={pld.get('origin')}  行数={len(lines)}")
    w(f"  notes={pld.get('notes')!r}")
    w(f"  template_names={pld.get('template_names')}")
    w(f"  carryover={json.dumps(pld.get('carryover') or {}, ensure_ascii=False)[:300]}")
    for ln in lines:
        w(f"   #{ln['no']:>2} beat={ln['beat']!r} ref={ln.get('template_ref')!r} "
          f"hook={ln.get('hook')!r}")
        w(f"        summary: {ln['summary']}")
    (OUT / "计划表_A.json").write_text(json.dumps(
        {"project_id": pid, "volume_id": vid, "article_id": aid,
         "origin": pld.get("origin"), "status": pld.get("status"),
         "template_ids": pld.get("template_ids"), "template_names": pld.get("template_names"),
         "notes": pld.get("notes"), "n_lines": len(lines), "lines": lines,
         "carryover": pld.get("carryover")}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # 注入痕迹的客观证据：哪些行填了 template_ref、填的是什么
    injected = [ln.get("template_ref") for ln in lines if (ln.get("template_ref") or "").strip()]
    w(f"\n  🔎 注入痕迹：{len(injected)}/{len(lines)} 行填了 template_ref")
    for x in injected:
        w(f"      · {x}")
    (OUT / "注入痕迹_A.json").write_text(json.dumps(
        {"n_lines": len(lines), "n_with_template_ref": len(injected),
         "template_refs": injected}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 5) 拍板锁定 ----------
    w("\n--- [A5] 拍板锁定 plan/confirm ---")
    cd = req("POST", f"/projects/{pid}/articles/{aid}/plan/confirm")
    w(f"  status={cd.get('status') if isinstance(cd, dict) else cd}")

    # ---------- 6) 生成第 1 章正文（流式）----------
    w("\n--- [A6] 生成第 1 章正文（SSE 流式）---")
    body = {"chapter_no": 1, "article_id": aid, "prompt_hint": None,
            "from_discussion": False, "word_range": {"min": 2600, "max": 3000},
            "temperature": 0.4, "ingest_level": "lite"}
    t0 = time.time()
    txt, events = sse_generate(f"/projects/{pid}/chapters/generate", body)
    dt = time.time() - t0
    w(f"  耗时 {dt:.1f}s｜SSE 事件 {len(events['events'])} 个（chunk {events['n_chunk']}）"
      f"｜正文 {len(txt)} 字")
    w(f"  saved={events['saved'].get('saved') or events['saved'].get('done')}")
    if events["saved"].get("error"):
        w(f"  ⚠️ error 事件：{events['saved']['error']}")
    (OUT / "正文_A_路径A.md").write_text(
        f"# 路径A · 第1章正文（篇规划链注入 v4 模板后生成）\n\n"
        f"- 口述：{HINT}\n- 注入模板：{pld.get('template_names')}\n"
        f"- 生成耗时 {dt:.1f}s｜字数 {len(txt)}｜SSE 事件 {events['events']}\n\n---\n\n{txt}\n",
        encoding="utf-8")
    (OUT / "正文A_sse事件.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=1)[:200000], encoding="utf-8")

    # ---------- 7) 落库核对 ----------
    w("\n--- [A7] 落库核对 ---")
    for it in as_items(req("GET", f"/projects/{pid}/chapters?article_id={aid}")):
        w(f"  chapter_no={it.get('chapter_no')} id={str(it.get('id'))[:8]} "
          f"title={it.get('title')!r} word_count={it.get('word_count')} "
          f"content_len={len(it.get('content') or '')}")

    (OUT / "路径A_book.json").write_text(json.dumps(
        {"project_id": pid, "volume_id": vid, "article_id": aid, "book": BOOK,
         "hint": HINT, "top4": [r["name"] for r in rec]}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    (OUT / "路径A_日志.txt").write_text("\n".join(L), encoding="utf-8")
    w(f"\n已写 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())