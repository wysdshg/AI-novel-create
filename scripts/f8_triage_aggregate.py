# -*- coding: utf-8 -*-
"""
F8 P0 ③ 聚合脚本（机制文档 §4.3 第二层全书聚合）

输入：密度 JSON + 全部批次分诊 JSON（batch_*.json）
流程：
  1. 合并批次 → 每角色观测汇总（在场章并集/行为摘要/别称/档位建议）
  2. 新角色补漏合并（跨批同名）
  3. 异名归并：别名∩角色名 → 合并身份（带证据，可回滚）
  4. 信号A：合并身份花名册重跑密度脚本（子进程，零成本）
  5. 信号B：分诊档位建议聚合（同意率/调整方向）
  6. 双信号定档 + QA 旗标（密度命中但分诊长期不确认 → 误命中候选）
输出：花名册合并文件 / aggregate JSON+MD / 分档抽检对照页 HTML
"""
import argparse
import json
import glob
import html
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

GRADE_NAME = {0: "无关配角", 1: "断续配角", 2: "篇章级", 3: "卷级", 4: "小说级", 5: "全书级/主角"}

# 泛称黑名单（精确匹配）：这些词谁都能被叫，进别称集合会把密度扫成全文命中
# （青元子 45→1081 事故根因）。带区分度的称谓（厉师兄/千秋圣女/墨老）不受影响。
GENERIC_ALIASES = frozenset({
    "主人", "主上", "大人", "前辈", "道友", "小友", "仙子", "公子", "小姐", "小婢",
    "丫鬟", "丫头", "本妃", "王妃", "公主", "大公主", "二公主", "殿下", "魔头", "妖女",
    "小妖狐", "小狐狸", "小狐", "人族修士", "下界修士", "下界飞升修士", "下界小子",
    "修士", "魔修", "朋友", "贵客", "客人", "上仙", "尊者", "圣祖", "老祖", "圣女",
    "圣子", "长老", "门主", "堂主", "护法", "使者", "师尊", "师叔", "师伯", "师祖",
    "师妹", "师姐", "师弟", "师兄", "兄弟", "晚辈", "小子", "小家伙", "怪物", "妖孽",
    "真人", "道长", "这位道友", "这位前辈", "此女", "此子", "那人", "那人影",
    # —— 2026-10-01 全量跑后按实伤扩充（青元子 45→1076 等事故）——
    "老者", "老人", "老妪", "老魔", "老怪物", "少女", "姑娘", "女子", "少妇", "妇人",
    "男子", "青年", "中年人", "中年男子", "大汉", "汉子", "师傅", "师父", "义父",
    "义母", "夫君", "夫人", "妾身", "本宫", "祖师", "祖师爷", "队长", "领队",
    "新任队长", "白狐", "银狼", "巨狼", "魔像", "血灵", "圣祖大人", "大长老", "二长老",
    "飞升修士", "神秘修士", "披发修士", "天南修士", "海外散修", "外族人", "外来人",
    "异族人", "夺宝之人", "青色身影", "青衫青年", "青袍人", "黑袍青年", "灰袍老者",
    "白袍少女", "白衣女子", "黑袍女子", "宫装女子", "蓝袍女子", "银衫女子", "银发女子",
    "银杉女子", "黄脸修士", "紫脸大汉", "虬须大汉", "妖猿", "妖修", "小怪物", "小辈",
    "人族小子", "仙师", "寒仙师", "韩仙师", "大长老", "此獠", "这厮", "客人", "来客",
})

def clean_alias(a):
    """泛称过滤：黑名单精确命中或超长修饰串（>6字多半是描述不是称呼）一律丢弃。"""
    a = (a or "").strip()
    if not a or len(a) > 6 or a in GENERIC_ALIASES:
        return None
    return a

# ---------- 1~3. 批次合并 ----------

def merge_batches(density, triage_dir):
    roster = {r["name"]: r for r in density["results"]}
    roster_alias = {r["name"]: set(r["aliases"]) for r in density["results"]}
    # 别称→主名映射（累积式：密度原始别称 + 各批新登记）
    alias2host = {}
    for m, als in roster_alias.items():
        for a in als:
            alias2host.setdefault(a, m)
    obs = {nm: {"chapters": set(), "behaviors": [], "aliases": set(), "suggests": [],
                "agree": 0, "adjust": 0} for nm in roster}
    new_chars = defaultdict(lambda: {"chapters": set(), "behaviors": [], "importance": [],
                                     "batches": set()})
    for p in sorted(glob.glob(os.path.join(triage_dir, "batch_*.json"))):
        d = json.load(open(p, encoding="utf-8"))
        bno = d["_meta"]["batch"]
        for c in d.get("characters", []):
            nm = c["name"]
            if nm not in obs:
                host = alias2host.get(nm)
                if host:
                    nm = host
                else:
                    continue
            o = obs[nm]
            o["chapters"].update(c.get("present_chapters", []))
            if c.get("behavior"):
                chs = c.get("present_chapters") or []
                o["behaviors"].append({"batch": bno, "span": [min(chs), max(chs)] if chs else [],
                                        "behavior": c["behavior"]})
            for a in c.get("aliases", []):
                a = clean_alias(a)
                if not a:
                    continue
                o["aliases"].add(a)
                alias2host.setdefault(a, nm)
            sg = c.get("grade_suggest")
            if sg is not None:
                o["suggests"].append({"batch": bno, "suggest": sg, "reason": c.get("reason", "")})
                if c.get("grade_agree") is True:
                    o["agree"] += 1
                else:
                    o["adjust"] += 1
        for c in d.get("new_characters", []):
            nm = c["name"]
            if alias2host.get(nm) and alias2host[nm] in obs:
                # 新角色名其实是已观测角色的别称 → 归到主名（防双计）
                nm = alias2host[nm]
                o = obs[nm]
                o["chapters"].update(c.get("present_chapters", []))
                if c.get("behavior"):
                    chs = c.get("present_chapters") or []
                    o["behaviors"].append({"batch": bno, "span": [min(chs), max(chs)] if chs else [],
                                            "behavior": c["behavior"]})
                continue
            nc = new_chars[nm]
            nc["chapters"].update(c.get("present_chapters", []))
            if c.get("behavior"):
                chs = c.get("present_chapters") or []
                nc["behaviors"].append({"batch": bno, "span": [min(chs), max(chs)] if chs else [],
                                         "behavior": c["behavior"]})
            if c.get("importance"):
                nc["importance"].append(c["importance"])
            nc["batches"].add(bno)
    return obs, new_chars, roster_alias

def resolve_merges(obs, new_chars, roster_alias):
    """异名归并（带防护，2026-10-01 厉飞雨被误并入韩立教训）：
    - 自动归并：仅限「新角色并入花名册角色」（b 非花名册且 b ∈ a 的别称集合）。
    - 花名册角色互相之间的别名证据 → 只出「归并提案」，等用户拍板（张铁=曲魂 走这条）。
    返回 (auto_merged: {b: a}, proposals: [text], notes)。"""
    all_names = set(obs) | set(new_chars)
    auto_merged, proposals, notes = {}, [], []
    for a in list(obs):
        a_alias = roster_alias.get(a, set()) | obs[a]["aliases"]
        for b in list(all_names):
            if b == a or b in auto_merged or a in auto_merged:
                continue
            if b in a_alias and b not in roster_alias:
                auto_merged[b] = a
                notes.append(f"自动归并：「{b}」并入「{a}」（别称证据）")
    # 花名册×花名册：A 的【分诊新登记】别称里有 B 的名字 → 提案
    for a in obs:
        for b in obs:
            if b == a or b in roster_alias.get(a, set()):
                continue
            if b in obs[a]["aliases"]:
                proposals.append(f"「{b}」并入「{a}」？（分诊别称登记：{sorted(obs[a]['aliases'])[:6]}…）")
    return auto_merged, proposals, notes

# ---------- 4. 重跑密度 ----------

def rerun_density(book_dir, names_file, out_json, script_path, extra=None):
    cmd = [sys.executable, script_path, "--book-dir", book_dir, "--names", names_file,
           "--out", out_json, "--batch-size", "0"]
    if extra:
        cmd += extra
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"密度重跑失败:\n{r.stdout}\n{r.stderr}")
    return json.load(open(out_json, encoding="utf-8"))

# ---------- 6. 双信号定档 + QA ----------

def dual_grade(dres, o):
    """o 为聚合观测（可为空）。返回 (final_grade, flag, note)。"""
    dg = dres["grade"] if dres else None
    sugs = [s["suggest"] for s in (o.get("suggests") or [])] if o else []
    if not sugs or dg is None:
        return dg, "", "无分诊建议" if dg is not None else "零出场"
    dev = [s for s in sugs if s != dg]
    if not dev:
        return dg, "", f"分诊{len(sugs)}批全部同意"
    up = sum(1 for s in dev if s > dg)
    dn = sum(1 for s in dev if s < dg)
    if up >= 2 and up > dn:
        return dg, "上调候选", f"分诊{up}/{len(sugs)}批建议>{dg}"
    if dn >= 2 and dn > up:
        return dg, "下调候选", f"分诊{dn}/{len(sugs)}批建议<{dg}"
    return dg, "", f"偶发分歧{len(dev)}/{len(sugs)}批（以密度为准）"

def qa_false_positive(dres, o, covered_set, chapter_nums):
    """密度命中但分诊在场章确认率极低 → 误命中候选（血光案例自动化）。
    🔴 分母只用「已跑批次覆盖的章节窗口」；presence_runs 是文件序位置，
    需经 chapter_nums 映射成章号再与分诊章号求交。"""
    if not dres or not o or not o.get("chapters") or not covered_set:
        return ""
    dset = set()
    for a, b in dres["presence_runs"]:
        dset.update(chapter_nums[a - 1: b])
    dset &= covered_set
    if len(dset) < 10:
        return ""
    cover = len(o["chapters"] & dset) / len(dset)
    if cover < 0.4:
        return f"误命中候选（覆盖窗内密度{len(dset)}章，分诊仅确认{cover:.0%}）"
    return ""

# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", required=True)
    ap.add_argument("--density", required=True, help="初版密度 JSON（含 78 人花名册结果）")
    ap.add_argument("--triage-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--density-script", default=os.path.join(os.path.dirname(__file__), "f8_density_stats.py"))
    ap.add_argument("--min-new-importance", default="low", help="新角色入库花名册的最低重要度")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    density = json.load(open(args.density, encoding="utf-8"))
    obs, new_chars, roster_alias = merge_batches(density, args.triage_dir)
    # 把密度档位带进 obs
    for nm, r in density_result_map(density).items():
        if nm in obs:
            obs[nm]["grade"] = r["grade"]

    auto_merged, proposals, merge_notes = resolve_merges(obs, new_chars, roster_alias)
    merged_into = auto_merged

    # 新角色花名册准入：importance 投票 high/mid 准入，low 仅 low 且零票的丢弃
    keep_new = {}
    for nm, nc in new_chars.items():
        if nm in merged_into:
            continue
        votes = nc["importance"]
        if votes.count("high") + votes.count("mid") >= 1:
            keep_new[nm] = nc
        elif not votes:
            keep_new[nm] = nc  # 无票先保留，密度说了算
        # 全 low → 不入册（走通用脸谱）

    # 合并身份花名册（主名<TAB>别称）
    names_lines, merge_log = [], []
    members_of = defaultdict(list)
    for b, a in merged_into.items():
        members_of[a].append(b)
    all_ids = {nm for nm in obs if nm not in merged_into} | set(keep_new)

    def safe_aliases(nm, raw):
        """防串门 2.0：剔除其他角色主名 + 剔除与其他角色名构成包含关系的别称
        （器灵⊂器灵子 事故：银月 290 vs 真实 197 的差额来源）。"""
        out = set()
        for a in raw:
            if a == nm or a in all_ids:
                continue
            if any((a in b or b in a) and a != b for b in all_ids):
                continue
            if clean_alias(a):
                out.add(a)
        return out

    for nm in obs:
        if nm in merged_into:
            continue
        # 🔴 别称必须并集：密度原始别称 ∪ 分诊登记别称（丢了前者=汪凝档案0事故）
        raw = (roster_alias.get(nm, set()) | obs[nm]["aliases"]) - {nm}
        aliases = safe_aliases(nm, raw)
        for b in members_of.get(nm, []):
            aliases.add(b)  # 被并者主名降为别称
            raw_b = (roster_alias.get(b, set()) | obs.get(b, {}).get("aliases", set())) - {b}
            aliases |= safe_aliases(nm, raw_b)
        aliases = {a for a in aliases if clean_alias(a)}
        names_lines.append(nm + (("\t" + ",".join(sorted(aliases))) if aliases else ""))
    for nm in keep_new:
        names_lines.append(nm)
    names_path = os.path.join(args.out_dir, "roster_merged.txt")
    open(names_path, "w", encoding="utf-8").write(
        "# F8 聚合花名册（密度78人 + 分诊补漏 + 异名归并）\n" + "\n".join(names_lines) + "\n")

    # 信号A：重跑密度
    den2_path = os.path.join(args.out_dir, "density_merged.json")
    den2 = rerun_density(args.book_dir, names_path, den2_path, args.density_script)
    d2map = {r["name"]: r for r in den2["results"]}

    # 双信号定档 + QA（QA 只在已跑批次覆盖的章号窗口内判）
    import re as _re
    done_batches = {int(_re.search(r"batch_(\d+)\.json", p).group(1))
                    for p in glob.glob(os.path.join(args.triage_dir, "batch_*.json"))}
    chapter_nums = density["chapter_nums"]
    covered = set()
    for b in density["batches"]:
        if b["batch"] in done_batches:
            covered.update(chapter_nums[b["idx_range"][0] - 1: b["idx_range"][1]])
    rows = []
    for nm, dres in d2map.items():
        if dres["grade"] is None and nm not in obs and nm not in keep_new:
            continue
        o = obs.get(nm) or keep_new.get(nm) or {}
        g, flag, note = dual_grade(dres, o)
        qa = qa_false_positive(dres, o, covered, chapter_nums)
        rows.append({"name": nm, "density_grade": dres["grade"], "final_grade": g, "flag": flag,
                     "note": note, "qa": qa, "total_on": dres["total_on"],
                     "triage_chapters": len(o.get("chapters", set())),
                     "behaviors": (o.get("behaviors") or [])[:6],
                     "aliases": sorted(o.get("aliases", set()) if "aliases" in o else set()),
                     "eff_segs": dres["eff_segs"], "coverage": dres["coverage"]})
    rows.sort(key=lambda r: (-(r["final_grade"] or 0), -r["total_on"]))

    # 输出 JSON + MD
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "n_batches_used": len(glob.glob(os.path.join(args.triage_dir, "batch_*.json"))),
               "merge_notes": merge_notes, "merge_proposals": proposals, "rows": rows}
    json_path = os.path.join(args.out_dir, "aggregate.json")
    json.dump(payload, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=list)

    L = [f"# F8 全书分档清单（凡人修仙传）", "",
         f"- 批次覆盖：{payload['n_batches_used']} 批｜花名册 {len(names_lines)} 人"
         f"（密度 78 + 补漏 {len(keep_new)} − 归并 {len(merged_into)}）",
         f"- 异名归并（自动）：{'；'.join(merge_notes) if merge_notes else '无'}",
         f"- 归并提案（花名册间，待拍板）：{'；'.join(proposals) if proposals else '无'}", ""]
    dist = defaultdict(int)
    for r in rows:
        if r["final_grade"] is not None:
            dist[r["final_grade"]] += 1
    L += ["## 分档分布", "", "| 档 | 人数 |", "|---|---|"]
    for g in range(6):
        L.append(f"| {g} {GRADE_NAME[g]} | {dist[g]} |")
    L += ["", "| 角色 | 定档 | 密度档 | 出场章 | QA/旗标 |", "|---|---|---|---|---|"]
    for r in rows:
        if r["final_grade"] is None and not r["qa"]:
            continue
        L.append(f"| {r['name']} | {r['final_grade']} | {r['density_grade']} | {r['total_on']}"
                 f" | {r['flag']} {r['qa']} |".replace("  |", " |"))
    open(os.path.join(args.out_dir, "aggregate.md"), "w", encoding="utf-8").write("\n".join(L))

    # 分档抽检对照页
    render_review_html(rows, keep_new, merge_notes, proposals,
                       os.path.join(args.out_dir, "review_page.html"))
    print(f"聚合完成：{len(rows)} 角色 -> {json_path}")

def density_result_map(density):
    return {r["name"]: r for r in density["results"]}

def render_review_html(rows, keep_new, merge_notes, proposals, out_path):
    """每档展示代表角色+代表行为，供用户 yes/no 抽检。"""
    by_grade = defaultdict(list)
    for r in rows:
        if r["final_grade"] is not None:
            by_grade[r["final_grade"]].append(r)
    sections = []
    for g in range(5, -1, -1):
        rs = by_grade.get(g, [])
        if not rs:
            continue
        show = rs[:10] if g >= 2 else rs[:5]
        items = []
        for r in show:
            bhs = "".join(f'<li>{html.escape(b["behavior"])}</li>' for b in r["behaviors"][:3])
            al = f"（别称：{'、'.join(r['aliases'][:6])}）" if r["aliases"] else ""
            qa = f'<span style="color:#b45309">⚠ {html.escape(r["qa"])}</span>' if r["qa"] else ""
            flag = f'<span style="color:#1d4ed8">{html.escape(r["flag"])}</span>' if r["flag"] else ""
            items.append(f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;'
                         f'padding:10px 14px;margin:8px 0">'
                         f'<b>{html.escape(r["name"])}</b>{html.escape(al)} '
                         f'<span style="color:#6b7280;font-size:12px">出场{r["total_on"]}章 '
                         f'密度档{r["density_grade"]} {flag} {qa}</span>'
                         f'<ul style="font-size:13px;color:#374151;margin:6px 0;padding-left:18px">{bhs}</ul></div>')
        more = f'<p style="color:#9ca3af;font-size:12px">…本档共 {len(rs)} 人，仅展示前 {len(show)}</p>' if len(rs) > len(show) else ""
        sections.append(f'<h2 style="font-size:17px;margin:18px 0 6px">档{g} {GRADE_NAME[g]}'
                        f'<span style="color:#6b7280;font-size:13px;font-weight:400">（{len(rs)} 人）</span></h2>'
                        + "".join(items) + more)
    flags = "".join(f'<li>{html.escape(r["name"])}：{html.escape(r["flag"])}—{html.escape(r["note"])}</li>'
                    for r in rows if r["flag"])
    qas = "".join(f'<li>{html.escape(r["name"])}：{html.escape(r["qa"])}</li>'
                  for r in rows if r["qa"])
    merges = "".join(f'<li>{html.escape(m)}</li>' for m in merge_notes)
    props = "".join(f'<li>{html.escape(p)} <b style="color:#1d4ed8">[待你拍板]</b></li>' for p in proposals)
    page = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>F8 分档抽检对照页</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f3f4f6;margin:0;padding:24px;max-width:960px;margin:0 auto">
<h1 style="font-size:21px">F8 分档抽检对照页 <span style="font-size:13px;color:#6b7280;font-weight:400">每档看几条 yes/no+备注，不过关的维度/角色重跑</span></h1>
<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px;margin:10px 0;font-size:13px">
<b>异名归并（自动）</b>：{merges or "无"}<br>
<b>归并提案</b>：{"<ul>" + props + "</ul>" if props else "无"}<br>
<b>调档候选</b>：{"<ul>" + flags + "</ul>" if flags else "无"}<br>
<b>QA 旗标</b>：{"<ul>" + qas + "</ul>" if qas else "无"}
</div>
{''.join(sections)}
<p style="color:#9ca3af;font-size:12px">生成于 {datetime.now():%m-%d %H:%M}｜行为摘要来自分诊批次（真书观测），仅前 3 条</p>
</body></html>'''
    open(out_path, "w", encoding="utf-8").write(page)

if __name__ == "__main__":
    main()
