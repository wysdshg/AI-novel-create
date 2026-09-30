# -*- coding: utf-8 -*-
"""PUT 接口回归测试：no-op 回写 + 非法类目拦截。结果写盘 _gr_put_test.json"""
import json
import urllib.request

BASE = "http://127.0.0.1:8011/api/v1/global-ref"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 04 §B17 直连


def req(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = None
        return e.code, payload


out = {}

# 1) 取第一条 item（看真实结构）
st, d = req("GET", "/items?limit=1")
out["list_status"] = st
items = d["data"] if isinstance(d.get("data"), list) else (d["data"].get("items") if isinstance(d.get("data"), dict) else None)
it = items[0]
out["first_item"] = {k: it.get(k) for k in ("id", "name", "category", "brief", "genre", "status", "aliases", "updated_at")}

# 2) no-op PUT：原样回写
iid = it["id"]
noop_body = {k: it.get(k) for k in ("name", "category", "brief", "genre", "status", "aliases")}
st2, d2 = req("PUT", f"/items/{iid}", noop_body)
out["noop_status"] = st2
out["noop_resp"] = d2 if st2 != 200 else {k: (d2.get("data") or {}).get(k) if isinstance(d2.get("data"), dict) else d2.get("data") for k in ("id", "name", "category", "brief", "genre", "status", "aliases")}

# 3) 非法类目 PUT：期望 400
st3, d3 = req("PUT", f"/items/{iid}", {"category": "不存在的类目"})
out["badcat_status"] = st3
out["badcat_resp"] = d3

# 4) 非法 status PUT：期望 400（顺带验证）
st4, d4 = req("PUT", f"/items/{iid}", {"status": "bogus"})
out["badstatus_status"] = st4
out["badstatus_resp"] = d4

# 5) 技能侧 no-op：取第一条 skill
st5, d5 = req("GET", "/skills?limit=1")
sk = (d5["data"] if isinstance(d5.get("data"), list) else d5["data"].get("skills"))[0]
sid = sk["id"]
sk_body = {k: sk.get(k) for k in ("name", "category", "brief", "genre", "status", "aliases")}
st6, d6 = req("PUT", f"/skills/{sid}", sk_body)
out["skill_noop_status"] = st6

out["all_pass"] = (st2 == 200 and st3 == 400 and st4 == 400 and st6 == 200)

with open("E:/AI小说创作/outputs/_gr_put_test.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(json.dumps({k: out[k] for k in ("list_status", "noop_status", "badcat_status", "badstatus_status", "skill_noop_status", "all_pass")}, ensure_ascii=False))
