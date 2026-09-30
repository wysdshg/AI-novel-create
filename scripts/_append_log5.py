# -*- coding: utf-8 -*-
"""追加 2026-09-26 日志：5173 双监听事故"""
text = """

## 「改了没生效」根因：5173 双 vite 监听（IPv4/IPv6 各一个）
- 现象：用户 Ctrl+Shift+R / 重开前端后仍看到旧版（50 字简介）。
- 取证：netstat 显示 127.0.0.1:5173（沙箱 vite，代理 8011 新后端）与 [::1]:5173（用户自己起的 vite，代理真机 8000）同时监听；浏览器访问 localhost 优先 ::1 → 用户打到自己的 vite → 8000 旧后端（无 full_desc，实测返回 keys 无该字段）；我的 playwright 用 127.0.0.1 显式 IPv4 → 沙箱链路 → 全是新效果。两边看到的世界不同。
- 处理：停掉沙箱 vite（SZM5g4）与 8011（2AVUVA），5173 归还用户进程；用户重启真机后端 8000 即生效（full_desc 数据已在库里，init_db 列已加）。
- 教训：localhost ≠ 127.0.0.1（IPv6 优先）；同端口双服务时「我看是好的你看是坏的」先 netstat 查双监听；playwright 验证一律写明用 127.0.0.1 且结论只对那条链路负责。
"""
with open("E:/AI小说创作/.workbuddy/memory/2026-09-26.md", "a", encoding="utf-8") as f:
    f.write(text)
print("ok")
