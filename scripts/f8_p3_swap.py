# -*- coding: utf-8 -*-
"""
F8 P3 换血总控（用户已拍板）：
  0. 备份 DB 主文件 → outputs/_backup/
  1. 退役现役 100 条 active（retire_plot_templates.py --apply --status active，可 --unarchive 回滚）
  2. 官方模板 47 个（36 脱敏 + 11 聚合）→ LLM 起功能名（去专名+查重）→ 写入 plot_templates
     （create() 自动三路向量化：plot_template / plot_cast / char_archetype）
  3. 终审：新行 name/logline/structure 对块名单 grep（通用称呼白名单外必须 0 命中）
输出：outputs/f8_p0/p3_report.json + 控制台摘要
⚠️ 动生产库：先备份；回滚 = retire --unarchive + 手工删新行（id 记录在报告里）
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, r"E:\AI小说创作\backend")
PROJ = r"E:\AI小说创作"
OFFICIAL = os.path.join(PROJ, "outputs", "f8_p0", "official")
REPORT = os.path.join(PROJ, "outputs", "f8_p0", "p3_report.json")

# 与 official_pipeline 同一份块名单（略）
sys.path.insert(0, os.path.join(PROJ, "scripts"))
from f8_official_pipeline import PROPER_NOUNS, LABEL2KEY, normalize_traits  # noqa: E402

GENERIC_TITLES = {"少主", "长老", "门主", "堂主", "护法", "前辈", "道友", "小姐", "公子",
                  "仙子", "圣女", "老祖", "夫人", "王爷", "陛下", "大人", "主上", "师尊",
                  "弟子", "修士", "魔头", "妖女", "圣祖", "使者", "公子爷", "大姐", "小子"}

def chat_once(prompt, api_key, base_url="http://127.0.0.1:9377/v1", model="qwen3.8-flash-next",
              max_tokens=4000, temperature=0.3):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "stream": True, "temperature": temperature,
                       "max_tokens": max_tokens}).encode("utf-8")
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

def main():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {"stamp": stamp}

    # ---- 0. 备份 DB 主文件 ----
    db_path = os.path.expanduser(r"~\.ai_novel\data\novel_agent.db")
    bak = os.path.join(PROJ, "outputs", "_backup", f"db_before_f8_p3_{stamp}.db")
    shutil.copy2(db_path, bak)
    report["db_backup"] = bak
    print(f"[0] DB 备份 -> {bak}", flush=True)

    # ---- 1. 退役现役 active ----
    r = subprocess.run([sys.executable, "scripts/retire_plot_templates.py", "--apply",
                        "--status", "active"], cwd=os.path.join(PROJ, "backend"),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout[-800:], flush=True)
    if r.returncode != 0:
        sys.exit(f"退役失败: {r.stderr[-500:]}")
    report["retired"] = True

    # ---- 2a. 载入官方模板（去重 + traits 归一化落盘） ----
    def canonical(nm):
        return re.sub(r"（[^）]*）$", "", nm).strip()

    by_canon = {}
    for p in sorted(os.listdir(OFFICIAL)):
        if not p.startswith("anon_") or not p.endswith(".json"):
            continue
        t = json.load(open(os.path.join(OFFICIAL, p), encoding="utf-8"))
        m = t.pop("_meta", {})
        src = m.get("source_name", "")
        key = canonical(src)
        # 优先无括号后缀的版本
        if key not in by_canon or "（" not in src:
            by_canon[key] = {"tpl": t, "grade": m.get("grade"), "src": key, "file": p}
    n_variant_skipped = 0
    for p in sorted(os.listdir(OFFICIAL)):
        if not p.startswith("agg_") or not p.endswith(".json"):
            continue
        t = json.load(open(os.path.join(OFFICIAL, p), encoding="utf-8"))
        m = t.pop("_meta", {})
        fixed, dropped = normalize_traits(t.get("traits"))
        t["traits"] = fixed
        issues = list(m.get("issues", []))
        if dropped:
            issues.append(f"剔除自造维度: {dropped}")
        m["issues"] = issues
        t["_meta"] = m
        json.dump(t, open(os.path.join(OFFICIAL, p), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        by_canon[t.get("slot") or p] = {"tpl": t, "grade": 2, "src": t.get("slot") or p,
                                        "file": p, "cluster": True}
    print(f"[2a] 官方模板 {len(by_canon)} 个（档5~3 去重后 {sum(1 for v in by_canon.values() if not v.get('cluster'))}"
          f" + 聚合 {sum(1 for v in by_canon.values() if v.get('cluster'))}）", flush=True)

    # ---- 2b. LLM 起功能名（一次调用） ----
    db_path_ro = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(db_path_ro, uri=True)
    # 块名单（姓名级，用于名字核账）
    blocklist = set(PROPER_NOUNS)
    for (s,) in con.execute("SELECT structure FROM plot_templates"):
        pass  # 旧模板 cast 是匿名的，不需要
    con.close()
    agg_rows = json.load(open(os.path.join(PROJ, "outputs", "f8_p0", "aggregate_final",
                                           "aggregate.json"), encoding="utf-8"))["rows"]
    for rrow in agg_rows:
        blocklist.add(rrow["name"])
        blocklist.update(rrow.get("aliases") or [])
    blocklist = {w for w in blocklist if 1 < len(w) <= 8} - GENERIC_TITLES

    items = []
    for key, v in by_canon.items():
        t = v["tpl"]
        top = sorted(t.get("traits", {}).items(), key=lambda x: -abs(x[1]))[:3]
        items.append({"key": key, "slot": t.get("slot"), "mode": t.get("mode"),
                      "desc": (t.get("desc") or "")[:80],
                      "top": [f"{k}{val:+d}" for k, val in top]})
    api_key = json.loads(sqlite3.connect(f"file:{os.path.expanduser(r'~\.ai_novel\data\novel_agent.db')}?mode=ro", uri=True)
                         .execute("SELECT value FROM app_configs WHERE key='llm.gateway_key'").fetchone()[0])
    naming_prompt = ("下面是 JSON 数组，每项是一条角色功能模板的功能信息。给每条起一个「模板名」：≤10 字、"
                     "体现功能位与性格气质（如：隐忍算计型主角、护短冲动型挚友）、禁止任何人名/书名/门派名等专名、"
                     "彼此不重名。输出严格 JSON：{\"names\":[{\"key\":\"原key原样\",\"name\":\"模板名\"}]}，"
                     "必须覆盖全部 " + str(len(items)) + " 条。\n\n"
                     + json.dumps(items, ensure_ascii=False))
    names_map = {}
    try:
        content = chat_once(naming_prompt, api_key)
        text = re.sub(r"```(?:json)?", "", content).strip()
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        for it in data.get("names", []):
            k, nm = it.get("key"), (it.get("name") or "").strip()
            if k and nm and 1 < len(nm) <= 14:
                names_map[k] = nm
    except Exception as e:  # noqa: BLE001
        print(f"[2b] 命名失败走兜底: {e}", flush=True)
    # 核账 + 查重
    seen = set()
    for key, v in by_canon.items():
        nm = names_map.get(key)
        if not nm or (set(re.findall(r"[\u4e00-\u9fff]+", nm)) & {w for w in blocklist if len(w) >= 2}):
            v["tpl"].get("slot")
            nm = f"{v['tpl'].get('slot') or '角色'}·{v['grade']}档"
        base = nm
        i = 2
        while nm in seen:
            nm = f"{base}{i}"
            i += 1
        seen.add(nm)
        v["row_name"] = nm
    print(f"[2b] 命名完成（LLM {len(names_map)}/{len(items)}，其余兜底）", flush=True)

    # ---- 2c. 写库（StaticPool 单连接 + create() 自动三路向量化） ----
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import app.core.database as dbmod
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
        desc = (t.get("desc") or "").strip()
        data = {
            "name": v["row_name"], "scale": "character",
            "genre_tags": ["角色模板", f"档{g}"] + (["聚合"] if v.get("cluster") else []),
            "logline": desc[:100],
            "structure": {"cast": [item]},
            "pitfalls": [], "rhythm": None,
            "source_stats": {"origin": "f8_p0_anonymized", "book": "凡人修仙传",
                              "grade": g, "source_name": v["src"],
                              "member_count": t.get("member_count") or 1},
            "status": "active",
        }
        o = tpl_crud.create(db, data)
        inserted.append({"id": o.id, "name": o.name, "grade": g, "src": v["src"]})
    db.close()
    report["inserted"] = inserted
    print(f"[2c] 写库 {len(inserted)} 条完成", flush=True)

    # ---- 3. 终审（跨进程只读核账 + 向量计数） ----
    con = sqlite3.connect(db_path_ro, uri=True)
    con.row_factory = sqlite3.Row
    active = con.execute("SELECT id, name, logline, structure, source_stats FROM plot_templates "
                          "WHERE status='active'").fetchall()
    audit = []
    for r in active:
        blob = (r["name"] or "") + (r["logline"] or "") + (r["structure"] or "")
        hits = sorted({w for w in blocklist if len(w) >= 2 and w in blob})
        audit.append({"id": r["id"], "name": r["name"], "hits": hits})
    bad = [a for a in audit if a["hits"]]
    vec = dict(con.execute("SELECT source_type, COUNT(*) FROM vector_chunks WHERE source_type IN "
                           "('plot_template','plot_cast','char_archetype') GROUP BY source_type").fetchall())
    con.close()
    report["audit_bad"] = bad
    report["vec_counts"] = vec
    report["active_count"] = len(active)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"[3] active={len(active)}｜三路向量块={vec}｜核账不通过 {len(bad)} 条", flush=True)
    for a in bad[:10]:
        print(f"  ⚠ {a['name']}: {a['hits'][:6]}", flush=True)
    print(f"报告 -> {REPORT}", flush=True)

if __name__ == "__main__":
    main()
