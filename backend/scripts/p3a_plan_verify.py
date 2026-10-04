# -*- coding: utf-8 -*-
"""[DEV-P3a] 计划链三小修的**实网**取证（②占位符 / ③重试 / ④流式）。

与 p3a_smoke.py（检索层）互补：那个走 HTTP 打真接口只读，这个**进程内**跑，
因为 ③「注入一次坏 JSON 看它重试」和 ④「抓真正发到网关的请求体」在 HTTP 层做不到。

🔴 只读 + 一次性：③ 会对 P3 测试书「第1篇」真的跑一次计划生成并落库
   （这是该接口的正常行为，不是副作用）；②④ 不写库。不碰任何现役书。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core import database                                  # noqa: E402
from app.services import plan_crud, plot_import, plot_template_crud as tpl  # noqa: E402

OUT = ROOT / "outputs" / "p3_inject"
OUT.mkdir(parents=True, exist_ok=True)
TEST_PID = "bcade8ab-c2f0-4a62-b3ed-6c38bf8e6a98"          # P3 测试书「P3注入实测」
HINT = "主角在宗门比试擂台上一战成名夺魁"

L: list[str] = []
REC: dict = {}


def w(*a):
    s = " ".join(str(x) for x in a)
    L.append(s)                       # 存档用原文（不能存 GBK 降级后的，否则证据失真）
    # 控制台是 GBK，遇到 emoji/生僻字会抛 UnicodeEncodeError —— 只影响打印，不影响取证
    print(s.encode("gbk", "replace").decode("gbk"))


def head(t: str):
    w("")
    w(f"=== {t} ===")


def main() -> int:
    ok = True

    def chk(cond, label, detail=""):
        nonlocal ok
        w(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
        ok = ok and bool(cond)

    database.get_engine()
    db = database.SessionLocal()
    plot_import._refresh_gw_mode(db)      # GW_ACTIVE 由 key getter 懒刷新，这里先刷一次好观察
    head(f"P3a 计划链取证 @ {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    w(f"  网关模式 GW_ACTIVE={plot_import.GW_ACTIVE}  base={plot_import.GW_BASE}")

    # 取测试书第 1 篇
    from app.models.orm import ArticleORM
    art = (db.query(ArticleORM).filter_by(project_id=TEST_PID)
           .order_by(ArticleORM.created_at.asc()).first())
    if art is None:
        w(f"  [事实] 测试书 {TEST_PID} 下没有文章，②③④ 需要真实 ctx，终止")
        return 2
    w(f"  测试书文章：id={art.id} name={art.name}")

    ctx = plan_crud._book_context(db, TEST_PID, art.id)
    tpls = tpl.search(db, query=HINT, scale="arc", top_k=2)["items"]

    # ---------------------------------------------------------------- ② 占位符
    head("② 计划 prompt 命名示例占位符")
    prompt = plan_crud._plan_prompt(ctx, tpls, HINT, 3, 8, mode="hint")
    REC["prompt_len"] = len(prompt)
    REC["prompt_full"] = prompt
    REC["templates_used"] = [t.get("name") for t in tpls]
    w(f"  prompt 长度 {len(prompt)} 字；注入模板={REC['templates_used']}")
    leak = "柳青岩" in prompt
    w(f"  [事实] 「柳青岩」出现在 prompt 里 = {leak}")
    if leak:                          # 定位到具体上下文，别只报一个布尔
        i = prompt.find("柳青岩")
        REC["leak_ctx_in_prompt"] = prompt[max(0, i - 200): i + 120]
        w("  泄漏处上下文：" + REC["leak_ctx_in_prompt"].replace("\n", " ⏎ "))
    # 🔴 断言只打**命名规则段**，不能打整条 prompt。
    #    实测（本次取证）：整条 prompt 里确实还有「柳青岩」，但它来自
    #    `ctx["prev_arc"]` —— 测试书自己上一章的正文（P3 路径A 生成的第1章里
    #    柳青岩已经是本书角色）。那是**书籍既有角色**，模型续写时复用它是正确的，
    #    不是 prompt 泄漏。用整条 prompt 断言会把「修对了」判成 FAIL。
    i = prompt.find("新角色命名规则")
    seg = prompt[i: i + 500] if i >= 0 else ""
    REC["naming_rule"] = seg
    REC["name_in_naming_rule"] = "柳青岩" in seg
    REC["name_in_prev_arc"] = "柳青岩" in (ctx.get("prev_arc") or "")
    w(f"  [事实] 「柳青岩」在命名规则段 = {REC['name_in_naming_rule']}"
      f"；在 prev_arc（本书既有角色）= {REC['name_in_prev_arc']}")
    chk(not REC["name_in_naming_rule"], "② 命名规则段不再拿真名当示例")
    chk("示例人名甲" in seg, "② 换成了明显占位符「示例人名甲」")
    chk("禁止直接使用" in seg, "② 带「禁止直接使用」明令")
    w("  —— 命名前后文 ——")
    w("  " + (seg or "(未找到该段)").replace("\n", "\n  "))

    # ---------------------------------------------------------------- ④ 流式
    head("④ 计划链走网关是否流式（抓真正发出的请求体）")
    sent: list[dict] = []
    real_build_opener = urllib.request.build_opener

    def spy_build_opener(*a, **kw):
        op = real_build_opener(*a, **kw)
        real_open = op.open

        def spy_open(req, timeout=None):
            try:
                body = json.loads(req.data.decode("utf-8"))
                sent.append({"url": req.full_url, "stream": body.get("stream"),
                             "model": body.get("model"), "has_messages": bool(body.get("messages"))})
            except Exception as e:  # noqa: BLE001
                sent.append({"url": req.full_url, "parse_error": f"{type(e).__name__}"})
            return real_open(req, timeout=timeout)

        op.open = spy_open
        return op

    plot_import.urllib.request.build_opener = spy_build_opener

    # ---------------------------------------------------------------- ③ 重试
    head("③ 计划行解析失败自动重试 1 次（第 1 次注入坏 JSON，第 2 次真调网关）")
    real_ds_post = plan_crud._ds_post
    state = {"n": 0}

    def flaky(key, prompt, **kw):
        state["n"] += 1
        if state["n"] == 1:
            w("  [注入] 第 1 次返回坏 JSON（模拟模型输出不可解析）")
            return "好的，这是我的计划：\n[1] 第一行\n[2] 第二行\n（以上，纯文本不是 JSON）"
        w("  [真实] 第 2 次真调网关（DeepSeek flash）")
        return real_ds_post(key, prompt, **kw)

    plan_crud._ds_post = flaky
    t0 = time.time()
    try:
        data = plan_crud._plan_llm_with_retry(ctx, tpls, HINT, 3, 8, "hint",
                                              key=plan_crud.ds_key(db))
        dt = time.time() - t0
        lines = plan_crud._valid_lines(data.get("lines"))
        REC["retry"] = {"calls": state["n"], "attempts": data.get("_attempts"),
                        "sec": round(dt, 1), "lines": len(lines)}
        w(f"  调用次数={state['n']}  data._attempts={data.get('_attempts')}  "
          f"有效行={len(lines)}  耗时={dt:.1f}s")
        chk(state["n"] == 2, "③ 坏 JSON 后确实自动重试了第 2 次", f"调用 {state['n']} 次")
        chk(data.get("_attempts") == 2, "③ 账里标明实际用了几次")
        chk(bool(lines), "③ 第 2 次真调用成功解析出行", f"{len(lines)} 行")
        REC["retry"]["first_lines"] = [
            {"no": x.get("no"), "beat": x.get("beat"), "summary": (x.get("summary") or "")[:60]}
            for x in lines[:3]]
        w("  —— 真实计划前 3 行 ——")
        for x in REC["retry"]["first_lines"]:
            w(f"    {x['no']}. [{x['beat']}] {x['summary']}")
        # ⚠️ 不能断言输出里没有「柳青岩」：它在 `prev_arc` 里，是**本书既有角色**，
        #    模型续写时复用它是正确行为（同书连贯性），不是 prompt 泄漏。
        #    该断言的正题是「占位符没被当真名用」——那才是 ② 要治的病。
        blob = json.dumps(lines, ensure_ascii=False)
        REC["retry"]["placeholder_leak"] = [p for p in ("示例人名甲", "示例人名乙")
                                            if p in blob]
        chk(not REC["retry"]["placeholder_leak"],
            "③ 真实输出没把占位符当角色名用",
            f"命中 {REC['retry']['placeholder_leak']}")
        REC["retry"]["reused_book_char"] = "柳青岩" in blob
        w(f"  [事实] 输出复用了本书既有角色「柳青岩」= {REC['retry']['reused_book_char']}"
          f"（来自 prev_arc，非 prompt 泄漏）")
    except Exception as e:  # noqa: BLE001
        w(f"  [ERR] {type(e).__name__}: {e}")
        chk(False, "③ 重试后应成功解析", f"{type(e).__name__}: {str(e)[:120]}")
    finally:
        plan_crud._ds_post = real_ds_post

    # ---------------------------------------------------------------- ④ 断言
    head("④ 流式断言（用上面真调用时抓到的请求体）")
    for s in sent:
        w(f"  发出的请求：url={s.get('url')} model={s.get('model')} "
          f"stream={s.get('stream')} messages={s.get('has_messages')}")
    # ⚠️ 必须拷贝：下面直连对照会 sent.clear()，存引用会被一起清空（我踩过）
    REC["gateway_requests"] = list(sent)
    gw = [s for s in sent if "9377" in (s.get("url") or "")]
    chk(bool(gw), "④ 计划链确实走了网关", f"网关请求 {len(gw)} 条")
    chk(bool(gw) and all(s.get("stream") is True for s in gw),
        "④ 每条网关请求都带 stream:true", f"stream 取值 {[s.get('stream') for s in gw]}")

    # 直连模式不得被流式化（回归保护）
    old = plot_import.GW_ACTIVE
    plot_import.GW_ACTIVE = False
    sent.clear()
    try:
        plot_import._stream_text = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("直连模式不许走流式"))
        plot_import._chat_post = lambda *a, **kw: "（直连桩，不真调）"
        got = plot_import._ds_post("k", "p", max_tokens=10)
        chk(got == "（直连桩，不真调）", "④ 直连模式保持非流式（不越界改动）")
    finally:
        plot_import.GW_ACTIVE = old

    head("总计")
    chk(ok, "P3a 计划链三小修取证")
    REC["all_pass"] = ok
    (OUT / "P3a_计划链取证.json").write_text(
        json.dumps(REC, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "P3a_计划链取证.txt").write_text("\n".join(L), encoding="utf-8")
    db.close()
    w(f"===== {'ALL PASS' if ok else '有 FAIL'} =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
