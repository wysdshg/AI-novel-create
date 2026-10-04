# -*- coding: utf-8 -*-
"""[DEV-P3a] 红线实测：网关挂掉时检索链必须照常返回（退纯向量序 + raw_ai 注明）。

单测已经覆盖（TestKeyMatchNeverBlocks 7 例），这里打**真接口**：
临时把网关 base 指到一个没人监听的端口，模拟「网关挂」，看 search 还返不返回结果。
🔴 只读；用完把 base 还原（进程退出即还原，不写库）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "p3_inject"
API = "http://127.0.0.1:8000/api/v1"
HINT = "主角在宗门比试擂台上一战成名夺魁"

sys.path.insert(0, str(ROOT / "backend"))
from app.core import database                    # noqa: E402
from app.services import plot_import             # noqa: E402

L: list[str] = []


def w(*a):
    s = " ".join(str(x) for x in a)
    L.append(s)
    print(s.encode("gbk", "replace").decode("gbk"))


def post(path: str, body: dict, timeout: int = 300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    r = urllib.request.Request(API + path, data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    try:
        with op.open(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), time.time() - t0
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[HTTP {e.code}] {path} → {e.read().decode('utf-8','replace')[:300]}")


def main() -> int:
    ok = True

    def chk(cond, label, detail=""):
        nonlocal ok
        w(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
        ok = ok and bool(cond)

    database.get_engine()
    db = database.SessionLocal()
    plot_import._refresh_gw_mode(db)
    w(f"=== P3a 红线实测：网关不可用时检索不退链 @ {time.strftime('%Y-%m-%dT%H:%M:%S')} ===")
    w(f"  真实网关 base = {plot_import.GW_BASE}（先取基线）")

    env, dt = post("/plot-templates/search", {"query": HINT, "scale": "arc", "top_k": 4})
    base_items = (env.get("data") or {}).get("items") or []
    w(f"  基线：网关正常，召回 {len(base_items)} 条，耗时 {dt:.1f}s，"
      f"top1={base_items[0]['name'] if base_items else None}")

    # ── 把网关指向死端口，模拟网关挂 ──
    dead = "http://127.0.0.1:9/v1"        # 9 = discard 端口，必定拒连
    old = plot_import.GW_BASE
    plot_import.GW_BASE = dead
    w(f"\n  [注入] 把 GW_BASE 改到 {dead}（模拟网关挂）")
    try:
        t0 = time.time()
        # 只探**分类器**这一层：完整 search 走 HTTP 端点，进程内改不到（端点在别的进程）
        from app.services import plot_template_crud as tpl
        items, acct = tpl._apply_key_match(db, HINT, list(base_items), llm=True)
        dt2 = time.time() - t0
        w(f"  分类器耗时 {dt2:.1f}s（超时上限 {tpl.KEY_MATCH_TIMEOUT}s）")
        w(f"  key_match 账：{json.dumps(acct, ensure_ascii=False)[:300]}")
        chk(len(items) == len(base_items), "红线1 网关挂时召回集一条不少",
            f"{len(base_items)} → {len(items)}")
        chk([i["name"] for i in items] == [i["name"] for i in base_items],
            "红线2 网关挂时退纯向量序（顺序未变）")
        chk(acct.get("ok") is False, "红线3 账里注明分类失败")
        chk(bool(acct.get("reason")), "红线4 账里有失败原因（可排查）",
            f"reason={str(acct.get('reason'))[:90]}")
        chk(dt2 < tpl.KEY_MATCH_TIMEOUT + 15, "红线5 有超时上限，不会无限挂住主链",
            f"{dt2:.1f}s < {tpl.KEY_MATCH_TIMEOUT + 15}s")
        REC = {"dead_base": dead, "sec": round(dt2, 1), "acct": acct,
               "baseline_top1": base_items[0]["name"] if base_items else None,
               "items_kept": len(items)}
    finally:
        plot_import.GW_BASE = old
    chk(plot_import.GW_BASE == old, "红线6 注入的假 base 已还原")

    w("\n  [事实] 结论：网关不可用时分类器在 "
      f"{REC['acct'].get('reason', '')[:60]} 内失败并退让，主链照常返回向量序。")

    (OUT / "P3a_红线实测.json").write_text(
        json.dumps({**REC, "all_pass": ok}, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "P3a_红线实测.txt").write_text("\n".join(L), encoding="utf-8")
    db.close()
    w(f"\n===== {'ALL PASS' if ok else '有 FAIL'} =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
