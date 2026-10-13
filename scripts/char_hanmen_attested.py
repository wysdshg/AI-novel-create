# -*- coding: utf-8 -*-
"""为薄证据角色生成「原文直取」可证章集（正名在正文出现的章），供校验闸与 triage 观测层合并使用。"""
import glob, json, os, re, collections

H = "outputs/char_hanmen/"
TXT = {}
for f in glob.glob("小说/寒门枭士/*.txt"):
    b = os.path.basename(f)
    m = re.match(r"(\d+)_", b)
    if not m:
        continue
    t = ""
    for enc in ("utf-8", "gb18030"):
        try:
            t = open(f, encoding=enc).read()
            break
        except UnicodeDecodeError:
            pass
    TXT[int(m.group(1))] = t

note = json.load(open(H + "_余量包注记.json", encoding="utf-8"))
thin = [t.split("(档")[0] for t in note["记录偏少"]]
out = {}
for r in thin:
    out[r] = sorted(n for n, t in TXT.items() if r in t)
json.dump({"说明": "分诊记录偏少的角色，其正名在原文出现的章号（可作在场证据的候选集；同名歧义仍需逐句判）",
           "角色": out}, open(H + "_原文可证章.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
for r in ("李凌轩", "徐灵儿", "丹珠", "太平", "老汪"):
    print(r, len(out.get(r, [])), out.get(r, [])[:12])
