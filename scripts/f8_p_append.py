# -*- coding: utf-8 -*-
"""
F8 追加入库（P3 的 append 形态，不退役现役模板）：
  0. 备份 DB 主文件
  1. 载入官方模板（official_dir/anon_*.json + agg_*.json，canonical 去重）
  2. LLM 功能命名（一次调用）+ 专名核账
  3. plot_template_crud.create 逐条入库（自动三路向量化）→ 🔴 循环后显式 db.commit()
     → 逐行核验向量块（index_template 不 commit 的坑：缺块行补索引后再 commit）
  4. 跨进程终审（黑名单 = 专有名词文件 + 花名册姓名别称 − 通用称呼白名单）
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, r"E:\AI小说创作\backend")
PROJ = r"E:\AI小说创作"

GENERIC = {"少主", "长老", "门主", "堂主", "护法", "前辈", "道友", "小姐", "公子", "仙子",
           "圣女", "老祖", "夫人", "王爷", "陛下", "大人", "主上", "师尊", "弟子", "修士",
           "魔头", "妖女", "圣祖", "使者", "公子爷", "大姐", "小子", "族长", "老家伙",
           "师公", "院长", "神秘人", "副院长", "副堂主", "阁主", "副阁主", "谷主", "宗主",
           "殿主", "统领", "都统", "大长老", "太上长老", "导师", "老者", "少女", "少妇",
           "盟主", "教主", "岛主", "城主", "家主", "副盟主", "香主", "舵主",
           "峰主", "掌门", "兄长", "少年", "遗孤", "族正", "母亲", "父亲",
           "少族长", "道人", "王子", "金甲男子", "元商", "叔父", "伯父", "箓气", "仙基"}

def chat_once(prompt, api_key, base_url, model, max_tokens=4000):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "stream": True, "temperature": 0.3, "max_tokens": max_tokens}).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    content = []
    with opener.open(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            for ch in chunk.get("choices") or []:
                if (ch.get("delta") or {}).get("content"):
                    content.append(ch["delta"]["content"])
    return "".join(content)

def gw_key():
    db = os.path.expanduser(r"~\.ai_novel\data\novel_agent.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("SELECT value FROM app_configs WHERE key='llm.gateway_key'").fetchone()
    con.close()
    return json.loads(row[0]) if row[0].strip().startswith('"') else row[0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--official-dir", required=True)
    ap.add_argument("--aggregate", required=True, help="该书 aggregate.json（花名册核账用）")
    ap.add_argument("--proper-nouns", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--gateway", default="http://127.0.0.1:9377/v1")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--api-key", default="", help="直连供应商 key（缺省读 llm.gateway_key）")
    ap.add_argument("--db-backup-dir", default=os.path.join(PROJ, "outputs", "_backup"))
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_path = os.path.expanduser(r"~\.ai_novel\data\novel_agent.db")

    # ---- 0. 备份 ----
    bak = os.path.join(args.db_backup_dir, f"db_before_append_{args.book}_{stamp}.db")
    shutil.copy2(db_path, bak)
    print(f"[0] DB 备份 -> {bak}", flush=True)

    # ---- 1. 载入官方模板 ----
    def canonical(nm):
        return re.sub(r"（[^）]*）$", "", nm).strip()

    by_canon = {}
    for p in sorted(os.listdir(args.official_dir)):
        if p.startswith("anon_") and p.endswith(".json"):
            t = json.load(open(os.path.join(args.official_dir, p), encoding="utf-8"))
            m = t.pop("_meta", {})
            key = canonical(m.get("source_name", ""))
            if key and key not in by_canon:
                by_canon[key] = {"tpl": t, "grade": m.get("grade"), "src": key, "cluster": False}
        elif p.startswith("agg_") and p.endswith(".json"):
            t = json.load(open(os.path.join(args.official_dir, p), encoding="utf-8"))
            m = t.pop("_meta", {})
            key = t.get("slot") or os.path.basename(p)
            by_canon[key] = {"tpl": t, "grade": 2, "src": key, "cluster": True}
    print(f"[1] 官方模板 {len(by_canon)} 个", flush=True)

    # 黑名单
    agg = json.load(open(args.aggregate, encoding="utf-8"))
    pn = {l.strip() for l in open(args.proper_nouns, encoding="utf-8")
          if l.strip() and not l.startswith("#")}
    for r in agg["rows"]:
        pn.add(r["name"])
        pn.update(r.get("aliases") or [])
    pn = {w for w in pn if 1 < len(w) <= 8} - GENERIC

    # ---- 2. LLM 功能命名 ----
    items = []
    for key, v in by_canon.items():
        t = v["tpl"]
        top = sorted(t.get("traits", {}).items(), key=lambda x: -abs(x[1]))[:3]
        items.append({"key": key, "slot": t.get("slot"), "mode": t.get("mode"),
                      "desc": (t.get("desc") or "")[:80],
                      "top": [f"{k}{val:+d}" for k, val in top]})
    api_key = args.api_key or gw_key()
    names_map = {}
    try:
        content = chat_once(
            "下面是 JSON 数组，每项是一条角色功能模板。给每条起「模板名」：≤10 字、体现功能位与性格气质、"
            "禁止任何人名/书名/门派名等专名、彼此不重名。输出严格 JSON："
            "{\"names\":[{\"key\":\"原key原样\",\"name\":\"模板名\"}]}，必须覆盖全部 "
            + str(len(items)) + " 条。\n\n" + json.dumps(items, ensure_ascii=False),
            api_key, args.gateway, args.model)
        text = re.sub(r"```(?:json)?", "", content).strip()
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        for it in data.get("names", []):
            k, nm = it.get("key"), (it.get("name") or "").strip()
            if k and nm and 1 < len(nm) <= 14:
                names_map[k] = nm
    except Exception as e:  # noqa: BLE001
        print(f"[2] 命名失败走兜底: {e}", flush=True)
    seen = set()
    for key, v in by_canon.items():
        nm = names_map.get(key)
        if not nm or (set(re.findall(r"[\u4e00-\u9fff]+", nm)) & {w for w in pn if len(w) >= 2}):
            nm = f"{v['tpl'].get('slot') or '角色'}·{v['grade']}档"
        base, i = nm, 2
        while nm in seen:
            nm = f"{base}{i}"
            i += 1
        seen.add(nm)
        v["row_name"] = nm
    print(f"[2] 命名完成（LLM {len(names_map)}/{len(items)}）", flush=True)

    # ---- 3. 写库（append，不退役） ----
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import app.core.database as dbmod
    from app.models.orm import PlotTemplateORM, VectorChunkORM
    from app.services import plot_template_crud as tpl_crud

    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    dbmod.init_db()

    inserted = []
    for key, v in by_canon.items():
        t, g = v["tpl"], v["grade"]
        ranks = t.get("ranks")
        ranks = [ranks] if isinstance(ranks, str) and ranks.strip() else (ranks or [])
        item = {
            "slot": t.get("slot") or "角色", "desc": t.get("desc") or "",
            "mode": t.get("mode") or "", "ranks": ranks,
            "traits": t.get("traits") or {}, "trait_basis": t.get("trait_basis") or {},
            "voice": t.get("voice") or {}, "behavior_patterns": t.get("behavior_patterns") or [],
            "relation_patterns": t.get("relation_patterns") or [],
            "slot_kind": "person", "beats": [], "srcs": [],
        }
        o = tpl_crud.create(db, {
            "name": v["row_name"], "scale": "character",
            "genre_tags": ["角色模板", f"档{g}"] + (["聚合"] if v.get("cluster") else []),
            "logline": (t.get("desc") or "").strip()[:100],
            "structure": {"cast": [item]}, "pitfalls": [], "rhythm": None,
            "source_stats": {"origin": "f8_p0_anonymized", "book": args.book,
                              "grade": g, "source_name": v["src"],
                              "member_count": t.get("member_count") or 1},
            "status": "active",
        })
        inserted.append({"id": o.id, "name": o.name, "grade": g, "src": v["src"]})
    # 🔴 index_template 不 commit：循环后必须显式提交（凡人 P3 背景节点丢块教训）
    db.commit()
    # 逐行核验向量块，缺的补索引 + 再提交
    fixed = []
    for rec in inserted:
        n = db.query(VectorChunkORM).filter(
            VectorChunkORM.source_id == rec["id"],
            VectorChunkORM.source_type.in_(("plot_template", "plot_cast", "char_archetype"))).count()
        if n < 3:
            trow = db.query(PlotTemplateORM).filter_by(id=rec["id"]).first()
            tpl_crud.index_template(db, trow)
            db.commit()
            fixed.append(rec["name"])
    print(f"[3] 写库 {len(inserted)} 条｜补索引 {fixed or '无'}", flush=True)
    db.close()

    # ---- 4. 跨进程终审 ----
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    active = con.execute("SELECT id, name, logline, structure FROM plot_templates "
                          "WHERE status='active'").fetchall()
    by_id = {r["id"]: r for r in active}
    audit_bad = []
    for rec in inserted:
        r = by_id.get(rec["id"])
        if r is None:
            audit_bad.append({"name": rec["name"], "hits": ["行不存在!"]})
            continue
        blob = (r["name"] or "") + (r["logline"] or "") + (r["structure"] or "")
        hits = sorted({w for w in pn if len(w) >= 2 and w in blob})
        if hits:
            audit_bad.append({"name": r["name"], "hits": hits[:8]})
    vec = dict(con.execute(
        "SELECT source_type, COUNT(*) FROM vector_chunks WHERE source_type IN (?,?,?) "
        "GROUP BY source_type", ("plot_template", "plot_cast", "char_archetype")).fetchall())
    total_active = len(active)
    con.close()

    report = {"stamp": stamp, "book": args.book, "db_backup": bak, "inserted": inserted,
              "audit_bad": audit_bad, "vec_counts": vec, "total_active": total_active}
    json.dump(report, open(args.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[4] active 总数={total_active}｜三路块={json.dumps(vec)}｜核账不通过 {len(audit_bad)} 条",
          flush=True)
    for a in audit_bad[:8]:
        print(f"  ⚠ {a['name']}: {a['hits']}", flush=True)
    print(f"报告 -> {args.report}", flush=True)

if __name__ == "__main__":
    main()
