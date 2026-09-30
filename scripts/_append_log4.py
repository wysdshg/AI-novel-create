# -*- coding: utf-8 -*-
"""追加 2026-09-26 日志：去除 50 字限制"""
text = """

## 弹窗去除 50 字限制（用户拍板「不要限制50字」）
- 前端弹窗「简述（≤50）」+「详细描述」两框合并为单一「描述」框（≤500 字）；编辑时预填 full_desc || brief。
- 保存链路：full_desc=全文，brief=自动取前 50 字（写作注入用短版，用户无需手填）；页面说明文案同步更新。
- 后端不变：_norm_brief 仍自动截 50 字入 brief（注入短版机制保留），full_desc 上限 500。
- 验证：playwright 实测弹窗单描述框/预填 61 字全文/maxlength=500；点保存「已保存」+ 数据不变 + 零 JS 错误；vite build 过。
"""
with open("E:/AI小说创作/.workbuddy/memory/2026-09-26.md", "a", encoding="utf-8") as f:
    f.write(text)
print("ok")
