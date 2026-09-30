# -*- coding: utf-8 -*-
"""浏览器级验证 GlobalRefView：skill/item 两 tab 数据渲染 + 截图"""
import json
import sys
from playwright.sync_api import sync_playwright

OUT = "E:/AI小说创作/outputs"
results = {}

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    for tab, name in (("skill", "技能库"), ("item", "物品库")):
        page.goto(f"http://127.0.0.1:5173/workspace/global-ref?tab={tab}",
                  wait_until="networkidle")
        page.wait_for_timeout(1500)
        # 页面标题
        title = page.locator(".gr-title").inner_text()
        # 表格行数（el-table__row）
        rows = page.locator(".el-table__body tr.el-table__row").count()
        # 统计条数字
        stats = page.locator(".gr-stats").inner_text().replace("\n", " ")
        # 是否还有 No Data
        nodata = page.get_by_text("No Data").count()
        # 是否还有重复的 el-tabs 头（el-tabs__header）
        tabs_header = page.locator(".el-tabs__header").count()
        # 第一行名称
        first = ""
        if rows:
            first = page.locator(".el-table__body tr.el-table__row td").first.inner_text()
        results[tab] = {"title": title, "rows": rows, "stats": stats,
                        "nodata": nodata, "el_tabs_header": tabs_header,
                        "first_row": first}
        page.screenshot(path=f"{OUT}/_gr_verify_{tab}.png", full_page=False)

    results["pageerrors"] = errors
    browser.close()

with open(f"{OUT}/_gr_verify.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(json.dumps(results, ensure_ascii=False))
sys.exit(0 if not results["pageerrors"] and results["skill"]["rows"] > 0 and results["item"]["rows"] > 0 else 1)
