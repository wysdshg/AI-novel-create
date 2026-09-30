# -*- coding: utf-8 -*-
"""实测保存链路：打开编辑→直接点保存（原值回写）→确认成功且数据不变"""
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

    # 首行名称与描述
    row = page.locator(".el-table__body tr.el-table__row").first
    name = row.locator("td").first.inner_text()
    desc_before = row.locator("td").nth(2).inner_text()
    res["name"] = name

    row.get_by_text("编辑").click()
    page.wait_for_timeout(600)
    page.locator(".el-dialog").get_by_text("保存").click()
    page.wait_for_timeout(1500)
    res["dialog_closed"] = page.locator(".el-dialog").count() == 0
    res["toast"] = page.locator(".el-message").inner_text() if page.locator(".el-message").count() else "(已消失)"

    page.wait_for_timeout(800)
    row2 = page.locator(".el-table__body tr.el-table__row").first
    desc_after = row2.locator("td").nth(2).inner_text()
    res["desc_unchanged"] = (desc_before == desc_after)
    res["pageerrors"] = errors
    browser.close()

print(json.dumps(res, ensure_ascii=False))
