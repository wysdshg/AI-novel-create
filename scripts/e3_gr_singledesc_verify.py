# -*- coding: utf-8 -*-
"""验证弹窗合并：单一「描述」框、无 50 字限制提示、预填全文"""
import json
from playwright.sync_api import sync_playwright

OUT = "E:/AI小说创作/outputs"
res = {}

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto("http://127.0.0.1:5173/workspace/global-ref?tab=item",
              wait_until="networkidle")
    page.wait_for_timeout(1500)

    page.locator(".el-table__body tr.el-table__row").first.get_by_text("编辑").click()
    page.wait_for_timeout(800)

    res["label_desc"] = page.locator(".el-dialog").get_by_text("描述", exact=True).count()
    res["label_brief_gone"] = page.locator(".el-dialog").get_by_text("简述").count()
    res["label_fulldesc_gone"] = page.locator(".el-dialog").get_by_text("详细描述").count()
    # 第一个 textarea 即描述框
    ta = page.locator(".el-dialog textarea").first
    val = ta.input_value()
    res["desc_len"] = len(val)
    res["desc_head"] = val[:50]
    # maxlength 属性应为 500
    res["maxlength"] = ta.get_attribute("maxlength")
    page.screenshot(path=f"{OUT}/_gr_single_desc_dialog.png")
    res["pageerrors"] = errors
    browser.close()

print(json.dumps(res, ensure_ascii=False))
