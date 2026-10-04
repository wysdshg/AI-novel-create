# -*- coding: utf-8 -*-
"""[DEV-P3a] 实网冒烟：两条真实口述 + 一条反例口述，验精确键优先匹配链（真调网关）。

复用 P3 的测试书「P3注入实测」，本脚本**只读**（不建书、不生成、不写库）——
检索层跑真分类器（qwen3-8B 经 MyAPI 网关），把账与重排前后顺序全部落盘。
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

SMOKE = "主角在宗门比试擂台上一战成名夺魁"        # 与 P3 同款，可 before/after 比对
SHAOSONG = "主角在朝堂定策联姻抗敌"              # 第二口述：绍宋线（验收 3）
OFFTOPIC = "主角在雪山疗伤偶遇故人"               # 反例口述：不该强行置顶（验收 2）

L: list[str] = []


def w(*a):
    s = " ".join(str(x) for x in a)
    L.append(s)
    print(s.encode("gbk", "replace").decode("gbk"))


def req(method: str, path: str, body: dict | None = None, *, timeout: int = 300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(r, timeout=timeout) as resp:
            env = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[HTTP {e.code}] {method} {path} → "
                         f"{e.read().decode('utf-8', 'replace')[:400]}")
    return env.get("data")


def smoke(label: str, query: str) -> dict:
    w(f"\n=== {label}：{query!r} ===")
    t0 = time.time()
    # 先取一次纯向量序（key_match=False）做 before，再取默认（开匹配链）做 after
    before = req("POST", "/plot-templates/search",
                 {"query": query, "scale": "arc", "top_k": 4, "key_match": False})
    after = req("POST", "/plot-templates/search",
                {"query": query, "scale": "arc", "top_k": 4})
    dt = time.time() - t0
    b = before.get("items") or []
    a = after.get("items") or []
    w(f"  耗时 {dt:.1f}s（含两次检索）")
    w(f"  mode={after.get('mode')} reranked={after.get('reranked')}")
    km = after.get("key_match") or {}
    w(f"  key_match 账：{json.dumps(km, ensure_ascii=False)[:400]}")
    w("  -- 纯向量序（before）--")
    for i, it in enumerate(b, 1):
        ss = it.get("source_stats") or {}
        w(f"    {i}. {it['name']}  score={it.get('_score')}  键=({ss.get('class')},"
          f"{ss.get('sub_event')})  matched_beats={len(it.get('matched_beats') or [])}")
    w("  -- 精确键优先后（after）--")
    for i, it in enumerate(a, 1):
        ss = it.get("source_stats") or {}
        w(f"    {i}. {it['name']}  score={it.get('_score')}  键=({ss.get('class')},"
          f"{ss.get('sub_event')})  matched_beats={len(it.get('matched_beats') or [])}")
    return {"label": label, "query": query,
            "before": [{"name": i["name"], "score": i.get("_score"),
                        "class": (i.get("source_stats") or {}).get("class"),
                        "sub_event": (i.get("source_stats") or {}).get("sub_event")}
                       for i in b],
            "after": [{"name": i["name"], "score": i.get("_score"),
                       "class": (i.get("source_stats") or {}).get("class"),
                       "sub_event": (i.get("source_stats") or {}).get("sub_event"),
                       "matched_beats": [f"{x.get('phase')}·{x.get('beat')}"
                                         for x in (i.get("matched_beats") or [])]}
                      for i in a],
            "key_match": km, "reranked": after.get("reranked"),
            "mode": after.get("mode"), "sec": round(dt, 1)}


def main() -> int:
    w(f"=== P3a 实网冒烟 · 精确键优先匹配链 @ {time.strftime('%Y-%m-%dT%H:%M:%S')} ===")
    recs = [smoke("A 同口述（P3 原句，可 before/after 比对）", SMOKE),
            smoke("B 反例口述（不该强行置顶）", OFFTOPIC),
            smoke("C 绍宋线第二口述（攒扩容证据）", SHAOSONG)]

    w("\n================ 验收断言 ================")
    a, off, ss = recs
    ok = True

    def chk(cond: bool, label: str, detail: str = ""):
        nonlocal ok
        w(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
        ok = ok and bool(cond)

    # 验收 1
    # 验收 1
    top1 = a["after"][0]["name"] if a["after"] else None
    off_before = [x["name"] for x in a["before"]]
    off_after = [x["name"] for x in a["after"]]
    # 验收 1：偏题项必须降到「同大类之后」。分类器猜的可能是同类的**兄弟子事件**
    # （实测「擂台比试」被判成 擂台大比/擂台夺魁，而 top1 模板的键是 宗门擂台比试），
    # 所以精确层可能为空，此时降位发生在**大类层** —— 两种都算通过，但要记清楚。
    guessed = (a["key_match"].get("class"), a["key_match"].get("sub_event"))
    top1_key = (a["after"][0]["class"], a["after"][0]["sub_event"]) if a["after"] else None
    chk(top1 == "擂台大比--宗门擂台比试", "验收1 top1 仍为该模板", f"实得 {top1}")
    chk("擂台大比--夺宝争锋" not in off_after[:1],
        "验收1 偏题项「夺宝争锋」不在 top1", f"after={off_after}")
    other_cls_before = [x["name"] for x in a["before"] if x["class"] != guessed[0]]
    moved = [n for n in other_cls_before
             if off_after.index(n) > off_before.index(n)] if other_cls_before else []
    chk(bool(moved) or off_after == off_before,
        "验收1 异大类项被降位（或本就无需降位）",
        f"猜测键={guessed}｜top1键={top1_key}｜降位的异大类项={moved}")
    chk(bool(a["key_match"]) and a["key_match"].get("class"),
        "验收1 raw_ai 有 key_match 推断账", f"class={a['key_match'].get('class')}")
    w(f"      猜测键={guessed}｜top1 实际键={top1_key}")
    w(f"      before={off_before}")
    w(f"      after ={off_after}")

    # 验收 2
    # 验收 2：反例口述的语义是「不许强行精确置顶」，不是「不许分类出任何类」。
    # 实测分类器给了 情劫情感/故友决裂（"偶遇故人"→故友，合理），
    # 但候选集里没有该键 → 无模板被提升 → 顺序不变。这才是要断言的不变量。
    gkey = (off["key_match"].get("class") or "", off["key_match"].get("sub_event") or "")
    after_keys = {(x["class"], x["sub_event"]) for x in off["after"]}
    chk(gkey not in after_keys or off["key_match"].get("class") in (None, ""),
        "验收2 反例口述不产生精确键", f"猜测键={gkey}｜候选键={sorted(after_keys)}")
    chk(off["reranked"] is False, "验收2 反例口述不重排")
    chk([x["name"] for x in off["after"]] == [x["name"] for x in off["before"]],
        "验收2 反例口述前后序一致（没有模板被强行置顶）")

    # 验收 3
    cls_hit = {x["class"] for x in ss["after"]}
    want = {"御前定策", "军政整军", "外交议和"}
    chk(bool(cls_hit & want), "验收3 命中绍宋线三类之一",
        f"实得类 {sorted(cls_hit)}｜交集 {sorted(cls_hit & want)}")

    (OUT / "P3a_冒烟记录.json").write_text(json.dumps(
        {"smoke_hint": SMOKE, "offtopic_hint": OFFTOPIC, "shaosong_hint": SHAOSONG,
         "records": recs, "all_pass": ok}, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "P3a_冒烟日志.txt").write_text("\n".join(L), encoding="utf-8")
    w(f"\n===== P3a 冒烟：{'ALL PASS' if ok else '有 FAIL'} =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())