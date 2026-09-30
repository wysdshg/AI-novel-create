# -*- coding: utf-8 -*-
"""探测网关可用模型列表与 404 错误体。"""
import json
import sqlite3
import urllib.request
import urllib.error

DB = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"


def gw_key():
    db = sqlite3.connect(DB, uri=True)
    (v,) = db.execute(
        "SELECT value FROM app_configs WHERE key='llm.gateway_key'"
    ).fetchone()
    db.close()
    return json.loads(v) if v.strip().startswith(('"', "'", "[")) else v.strip()


op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
key = gw_key()
hdrs = {"Authorization": f"Bearer {key}"}

for url in ["http://127.0.0.1:9377/v1/models",
            "http://127.0.0.1:9377/quota-status"]:
    try:
        r = op.open(urllib.request.Request(url, headers=hdrs), timeout=10)
        print("==", url, r.status)
        print(r.read().decode("utf-8")[:1500])
    except urllib.error.HTTPError as e:
        print("==", url, e.code)
        print(e.read().decode("utf-8", "replace")[:800])
    except Exception as e:  # noqa: BLE001
        print("==", url, type(e).__name__, str(e)[:200])
