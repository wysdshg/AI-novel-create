# -*- coding: utf-8 -*-
"""验证设定模板全链路：API 列表/详情 + fetch_settings_by_ids 模板兜底 + 页面渲染"""
import json
import sys
import urllib.request

sys.path.insert(0, "E:/AI小说创作/backend")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8011/api/v1"
res = {}


def get(path):
    with OPENER.open(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# 1) 模板列表
d = get("/setting-templates")
res["list"] = [{"name": t["name"], "genre": t["genre"], "chars": t["content_chars"]}
               for t in d["data"]]
res["list_count"] = len(d["data"])

# 2) 详情含正文
tpl_id = d["data"][0]["id"]
d2 = get(f"/setting-templates/{tpl_id}")["data"]
res["detail_has_content"] = len(d2.get("content") or "") > 1000

# 3) fetch_settings_by_ids 模板兜底（LOAD_SETTING 兼容）
from app.core.database import init_db  # noqa: E402
init_db()  # 惰性初始化：填上 SessionLocal 工厂
from app.core.database import SessionLocal  # noqa: E402
from app.services.reference_crud import fetch_settings_by_ids  # noqa: E402
db = SessionLocal()
pairs = fetch_settings_by_ids(db, [tpl_id])
db.close()
res["fetch_tpl_ok"] = bool(pairs) and "设定模板" in pairs[0][0] and len(pairs[0][1]) > 1000
res["fetch_tpl_name"] = pairs[0][0] if pairs else None

# 4) build_setting_catalog 含模板行
from app.core.context.layers import build_setting_catalog  # noqa: E402
db = SessionLocal()
# 取一个 project（catalog 需要 project_id 参数，可为任意存在 id；无 project 时也能编译）
from app.models.orm import ProjectORM  # noqa: E402
p = db.query(ProjectORM).first()
catalog = build_setting_catalog(db, p.id if p else "nonexistent")
db.close()
res["catalog_has_tpl"] = tpl_id in catalog

with open("E:/AI小说创作/outputs/_st_verify.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print(json.dumps(res, ensure_ascii=False))
