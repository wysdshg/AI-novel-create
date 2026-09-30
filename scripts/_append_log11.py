# -*- coding: utf-8 -*-
"""追加 2026-09-26 日志：波浪号删除线 bug"""
text = """

## 设定模板划线 bug（用户截图「为什么有划线」）
- 根因：content 里成对的 ASCII `~`（如「1~2 倍…200~300」同段两个）被 Markdown 渲染器按 GFM strikethrough 扩展解析成删除线，且第二个 `~` 被吞（显示「200300」）。单格内单个 ~ 安全，同段成对即中招。
- 修复：三份模板 MD 的 ASCII `~` 全部替换为全角 `～`（玄幻 60 处、架空历史 61 处、高武 0），文件与库同步，跨进程核账残留 0。脚本 setting_template_fix_tilde.py。
- 教训：模板类 MD 正文一律用全角 ～ 做区间号；写入 content 前可加校验（含 ASCII ~ 即警告）。
"""
with open("E:/AI小说创作/.workbuddy/memory/2026-09-26.md", "a", encoding="utf-8") as f:
    f.write(text)
print("ok")
