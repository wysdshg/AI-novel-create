# -*- coding: utf-8 -*-
"""POC 前置验证：MyAPI 网关两路 chat 可用性（ms=Flash-Next / sf=Qwen3-8B）。"""
import json
import time
import urllib.request

import sqlite3

DB = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"
GW = "http://127.0.0.1:9377/v1/chat/completions"


def gw_key():
    db = sqlite3.connect(DB, uri=True)
    (v,) = db.execute(
        "SELECT value FROM app_configs WHERE key='llm.gateway_key'"
    ).fetchone()
    db.close()
    return json.loads(v) if v.strip().startswith(('"', "'", "[")) else v.strip()


def chat(model, prompt, key, max_tokens=64, timeout=120):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        GW, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    r = op.open(req, timeout=timeout)
    data = json.loads(r.read().decode("utf-8"))
    dt = time.time() - t0
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "")
    usage = data.get("usage", {})
    return dt, content, len(reasoning), usage


if __name__ == "__main__":
    key = gw_key()
    print("key =", key[:12] + "..." + key[-4:])
    for model in ["qwen3.8-flash-next", "qwen3-8b"]:
        try:
            dt, content, rlen, usage = chat(
                model, "用一句话回答：你是什么模型？不要思考过程。", key)
            print(f"[OK] {model}  {dt:.1f}s  reasoning_len={rlen}  "
                  f"usage={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")
            print("     content:", content[:80].replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {model}  {type(e).__name__}: {str(e)[:150]}")
