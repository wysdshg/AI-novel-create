# -*- coding: utf-8 -*-
"""[DEV-P3] 补录：把已生成的路径A 计划表按**正确的扁平响应结构**重读并落证据。

为什么不重跑 p3_path_a.py：正文已生成落库（2276 字），重跑会再花一次网关调用；
本脚本只做 GET，把 `plan_crud.get_plan` 的真实字段补进 `计划表_A.json` 与日志。
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "p3_inject"
API = "http://127.0.0.1:8000/api/v1"

L: list[str] = []


def w(*a):
    s = " ".join(str(x) for x in a)
    L.append(s)
    print(s.encode("gbk", "replace").decode("gbk"))


def req(method: str, path: str, body: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(r, timeout=300) as resp:
            env = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[HTTP {e.code}] {method} {path} → "
                         f"{e.read().decode('utf-8', 'replace')[:400]}")
    return env.get("data")


def as_items(d) -> list:
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("items", "projects", "volumes", "articles", "chapters"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def main() -> int:
    book = json.loads((OUT / "路径A_book.json").read_text(encoding="utf-8"))
    pid, aid = book["project_id"], book["article_id"]
    w(f"补录 project={pid} article={aid}")
    pld = req("GET", f"/projects/{pid}/articles/{aid}/plan")
    lines = pld.get("lines") or []
    w(f"status={pld.get('status')} origin={pld.get('origin')} 行数={len(lines)}")
    w(f"template_names={pld.get('template_names')}")
    w(f"notes={pld.get('notes')!r}")
    for ln in lines:
        w(f"  #{ln['no']:>2} beat={ln['beat']!r}")
        w(f"       ref={ln.get('template_ref')!r} hook={ln.get('hook')!r}")
        w(f"       summary: {ln['summary']}")
    injected = [ln.get("template_ref") for ln in lines if (ln.get("template_ref") or "").strip()]
    w(f"\n🔎 注入痕迹：{len(injected)}/{len(lines)} 行填了 template_ref")
    for x in injected:
        w(f"    · {x}")

    (OUT / "计划表_A.json").write_text(json.dumps(
        {"project_id": pid, "article_id": aid, "origin": pld.get("origin"),
         "status": pld.get("status"), "template_ids": pld.get("template_ids"),
         "template_names": pld.get("template_names"), "notes": pld.get("notes"),
         "n_lines": len(lines), "lines": lines, "carryover": pld.get("carryover")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "注入痕迹_A.json").write_text(json.dumps(
        {"n_lines": len(lines), "n_with_template_ref": len(injected),
         "template_refs": injected}, ensure_ascii=False, indent=1), encoding="utf-8")

    # 追加进路径A 日志（不覆盖）
    log = OUT / "路径A_日志.txt"
    prev = log.read_text(encoding="utf-8") if log.exists() else ""
    log.write_text(prev + "\n\n=== 补录（正确读取 get_plan 扁平结构）===\n"
                   + "\n".join(L), encoding="utf-8")
    w(f"\n已更新 {OUT}/计划表_A.json 与 注入痕迹_A.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())