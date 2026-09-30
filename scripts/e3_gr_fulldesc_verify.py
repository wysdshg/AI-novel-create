# -*- coding: utf-8 -*-
"""验证 full_desc：表格描述列显示完整描述 + 编辑弹窗有详细描述字段"""
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

    # 第一行描述单元格长度（应为完整 60+ 字而非 50 字截断）
    first_desc = page.locator(".el-table__body tr.el-table__row td").nth(2).inner_text()
    res["first_desc_len"] = len(first_desc)
    res["first_desc_head"] = first_desc[:60]

    # 点第一行编辑，检查弹窗「详细描述」字段
    page.locator(".el-table__body tr.el-table__row").first.get_by_text("编辑").click()
    page.wait_for_timeout(800)
    dlg_label = page.get_by_text("详细描述").count()
    res["dialog_has_full_desc_label"] = dlg_label
    # 取弹窗里详细描述 textarea 的值
    ta = page.locator(".el-dialog textarea").nth(1)
    res["dialog_full_desc_len"] = len(ta.input_value()) if dlg_label else -1
    page.screenshot(path=f"{OUT}/_gr_fulldesc_dialog.png")

    # 无描述行（brief 回退）情况：搜一个只有 brief 的词不必强求，记录统计即可
    res["pageerrors"] = errors
    browser.close()

with open(f"{OUT}/_gr_fulldesc_verify.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print(json.dumps(res, ensure_ascii=False))
