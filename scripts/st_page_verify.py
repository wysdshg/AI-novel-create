# -*- coding: utf-8 -*-
"""验证设定库页：模板卡片渲染 + 查看弹窗全文"""
import json
from playwright.sync_api import sync_playwright

OUT = "E:/AI小说创作/outputs"
res = {}

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto("http://127.0.0.1:5199/workspace/setting", wait_until="networkidle")
    page.wait_for_timeout(1800)

    # 模板卡片数（名称出现）
    for name in ("玄幻小说设定", "架空历史设定", "都市高武设定"):
        res[f"card_{name}"] = page.get_by_text(name, exact=True).count()
    # 点开玄幻查看
    card = page.locator(".sv-tpl-card", has_text="玄幻小说设定").first
    card.get_by_text("查看").click()
    page.wait_for_timeout(1200)
    dlg = page.locator(".el-dialog")
    res["dialog_title_ok"] = dlg.get_by_text("设定模板：玄幻小说设定").count() > 0
    ta = dlg.locator("textarea").first
    res["content_len"] = len(ta.input_value())
    res["readonly"] = ta.get_attribute("disabled") is not None
    page.screenshot(path=f"{OUT}/_st_tpl_dialog.png")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)

    # 旧版 tab
    page.get_by_text("旧版单条设定").click()
    page.wait_for_timeout(1000)
    res["legacy_rows"] = page.locator(".el-table__body tr.el-table__row").count()
    page.screenshot(path=f"{OUT}/_st_legacy.png")

    res["pageerrors"] = errors
    browser.close()

print(json.dumps(res, ensure_ascii=False))
