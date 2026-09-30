# -*- coding: utf-8 -*-
"""【试验版·不落库】从「逐章概括」直接切原子事件 + 合成故事弧（docs/10 §11 验证）。

与 P1 的区别（关键）：
- P1 吃 `segment_summary`（= v3 的**弧内节拍说明**，规格 20~40 字，且 3 本书是旧路径残留）→ **作废**；
- 本脚本吃 `chapter_summaries.summary`（**逐章概括**，覆盖 100%、中位 112 字、已匿名化）。
- 一次调用同时产出：`atoms`（原子事件：词表 id + 章区间 + 起承转合概括）与
  `arcs`（故事弧：原子序号分组 + 核心原子）。**第二步「合成弧」在同一输出里给出** ——
  单纯把原子再分组不产生新信息，一次输出更省且能让模型同时权衡两级结构。

🔴 **本脚本只做试验：不写任何表**（不碰 `chapter_summaries.arc_no`、不碰 `atomic_variants`）。
产出 JSON + markdown 对照表到 `outputs/`，人工拍板后再定表结构与落库方案。

用法（在 backend/ 下执行）：
    ..\\.venv\\Scripts\\python.exe scripts/cut_atoms_from_chapters.py --start 1 --end 12
    ..\\.venv\\Scripts\\python.exe scripts/cut_atoms_from_chapters.py --start 1 --end 99 --report outputs/xxx.md
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import create_engine, text                        # noqa: E402
from sqlalchemy.orm import sessionmaker                           # noqa: E402
from sqlalchemy.pool import StaticPool                            # noqa: E402

MAX_OUT_TOKENS = 16384          # 🔴 魔搭硬上限
SUM_MIN, SUM_MAX = 80, 420      # 原子概括的验收区间（按「每章约 40 字」弹性给，下限 80）

ATOM_TABLE_HINT = """■ A 战斗（有对手身体对抗）/ ■ B 交易（有对价交换）/ ■ C 社交（有公开场合与身份关系变动）
■ D 危机（主体处于被动受损状态）/ ■ E 移动（空间位置改变）/ ■ F 修炼（自身能力值改变）
■ G 探索（获取未知物或信息）/ ■ H 情感（人际关系状态改变）"""


def parse_items(raw: str) -> dict:
    """从模型输出里抠出 JSON 对象（容错：代码围栏 / 前后废话 / 尾逗号 / 全角引号 / 截断）。"""
    if not raw:
        return {}
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)

    def _try(txt: str):
        for fix in (lambda x: x,
                    lambda x: re.sub(r",\s*([}\]])", r"\1", x),
                    lambda x: x.replace("“", '"').replace("”", '"')):
            try:
                return json.loads(fix(txt))
            except (json.JSONDecodeError, ValueError):
                continue
        return None

    i, j = s.find("{"), s.rfind("}")
    if i != -1:
        # 先试整体；失败再试逐个补右括号（截断修复）
        for cand in ([s[i:j + 1]] if j > i else []) + [s[i:] + "]" * k for k in (1, 2, 3)]:
            d = _try(cand)
            if isinstance(d, dict):
                return d
        # 最后一招：把 atoms/arcs 数组单独抠出来
        out = {}
        for k in ("atoms", "arcs"):
            m = re.search(rf'"{k}"\s*:\s*\[', s)
            if not m:
                continue
            st = m.end() - 1
            depth, end = 0, None
            for p in range(st, len(s)):
                if s[p] == "[":
                    depth += 1
                elif s[p] == "]":
                    depth -= 1
                    if depth == 0:
                        end = p + 1
                        break
            seg = s[st:end] if end else s[st:] + "]"
            arr = _try(seg)
            if isinstance(arr, list):
                out[k] = arr
        return out
    return {}


def build_atom_table(db) -> str:
    from app.models.orm import AtomicCategoryORM, AtomicEventORM
    cats = db.query(AtomicCategoryORM).order_by(AtomicCategoryORM.sort_order).all()
    lines = []
    for c in cats:
        rows = (db.query(AtomicEventORM)
                .filter(AtomicEventORM.category_id == c.id)
                .order_by(AtomicEventORM.id).all())
        lines.append(f"■ {c.id} {c.name}（{c.note or ''}）")
        for a in rows:
            flag = "" if a.status == "active" else "（待定，可选用）"
            lines.append(f"{a.id} {a.name}{flag}：{a.definition}")
            lines.append(f"    起：{a.beat_start}｜承：{a.beat_mid}｜"
                         f"转：{a.beat_turn}｜合：{a.beat_end}")
    return "\n".join(lines)


def load_originals(db, book: str) -> list[str]:
    rows = db.execute(text(
        "SELECT DISTINCT original FROM book_aliases "
        "WHERE book_name = :b AND LENGTH(original) >= 2"), {"b": book}).fetchall()
    return [r[0] for r in rows]


def build_prompt(book: str, c0: int, c1: int, chapters: list[tuple[int, str]],
                 atom_table: str) -> str:
    body = "\n".join(f"第{no}章：{sm}" for no, sm in chapters)
    return f"""你是小说结构分析员。下面是一部小说**第 {c0}~{c1} 章**的逐章概括
（已做专名匿名化：人名→「主角」「友·配角1」「敌·配角2」，势力→「友·势力1」，物品→「友·物品5」，境界→「境界2」）。

请完成两件事：

**第一步：切「原子事件」。** 一个原子事件 = 一件**有头有尾的事**，自带起承转合。
把第 {c0}~{c1} 章**连续切成若干原子事件**，每个原子事件给出：
  · `atomic_id`：原子编号，**必须严格取自下面的词表**（形如 A02 / B12 / H07）
  · `chapter_start` / `chapter_end`：章区间
  · `summary`：按**章数给长度** —— 每章约 40 字，**下限 80 字**
    （例：3 章的原子写 ~120 字，8 章的原子写 ~320 字）。
    按「起：…｜承：…｜转：…｜合：…」四拍写，**写足细节，不要只写一句结论**
  · `tags`：2~4 个手法标签（如 扮猪吃虎 / 借刀杀人 / 抬价试探 / 扮弱诱敌），不要复述原子名

**第二步：编「故事弧」。** 一条弧 = 一个完整的套路或爽点周期，通常含 **3~8 个**原子事件。
把上面切出的原子（用其 `seq` 序号）编成若干条弧，每条弧给出：
  · `arc_name`：4~8 字弧名
  · `atoms`：包含哪些原子的 seq 序号数组（**必须连续**）
  · `core_atomics`：本弧的**核心原子**（去掉它整条弧不成立），可 1~2 个
  · `chapter_start` / `chapter_end`

【原子事件词表】
{atom_table}

【第 {c0}~{c1} 章逐章概括】
{body}

【输出要求】
只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块围栏：
{{"atoms":[{{"seq":1,"atomic_id":"A02","chapter_start":1,"chapter_end":5,"summary":"……","tags":["……"]}}],
 "arcs":[{{"arc_name":"……","atoms":[1,2,3],"core_atomics":["A02"],"chapter_start":1,"chapter_end":30}}]}}

规则（逐条遵守）：
1. **章区间必须连续、不重叠、不遗漏**，`atoms` 合起来正好覆盖 {c0}~{c1} 全部章。
   🔴 **必须处理完本批给出的每一个章号，不许跳过、不许提前结束** ——
   若本批是第 90~189 章，就必须一直切到第 189 章，不能只切到中途就停。
   （实测踩坑：中段窗口时模型只切到第 145 章就停了，146~189 章整段丢失。）
   🔴 **每个章号只能属于一个原子**（严格不重叠）。若某一章里挤了两件事，
   把它归给你判断为**主要**的那一件，另一件在 tags 里提一句即可 —— **不要用重叠区间处理**。
2. `atomic_id` 必须严格取自词表，不得自创编号、不得改大小写、不得写原子名。
   若某段与词表中任何原子都不贴合，`atomic_id` 写空字符串 ""，并在 tags 里加一项 "无合适原子"。
3. 🔴 **同一个原子可以在相邻位置连续出现多次**（例：连续几轮比试都归 `A02`）——
   每次各自成一条 atom（章区间不同），`atomic_id` 相同。**不要为了"不重复"而硬套别的原子。**
4. `summary` 与 `tags` **不得出现任何专名**：人名、势力名、功法名、地名、宝物名一律不许出现；
   可用「主角」「同行者」「对手」「某一势力」「一处秘境」这类通用词，或沿用输入的匿名代号。
5. `atoms` 的 `seq` 从 1 开始连续编号；`arcs` 里的 `atoms` 必须引用这些 seq。
6. 若末尾剩不足 3 个原子，并入上一条弧（不要单独成弧）。"""


def cut_window(db, args, win_start: int, win_end: int,
               all_chapters: list[tuple[int, str]], atom_table: str, key: str):
    """跑一个窗口：返回 (atoms, arcs, prompt字符数, 返回字符数)。

    窗口 = §11.11 协议的最小单元：发 [win_start, win_end] 的**全部**逐章概括，
    让模型在这个完整上下文里切原子 + 切弧（最后一条弧下次回炉重切）。
    """
    from app.services import plot_import as pi
    chs = [(n, s) for n, s in all_chapters if win_start <= n <= win_end]
    if not chs:
        return [], [], 0, 0
    prompt = build_prompt(args.book, chs[0][0], chs[-1][0], chs, atom_table)
    raw = pi._ms_post(key, prompt, max_tokens=MAX_OUT_TOKENS, temperature=0.2,
                      timeout=900, thinking=False)
    data = parse_items(raw)
    return (data.get("atoms") or []), (data.get("arcs") or []), len(prompt), len(raw)


def _default_state_path(book: str) -> Path:
    """状态文件默认落在项目 outputs/_atomic_raw/（与其它中间产物同处）。"""
    root = Path(__file__).resolve().parents[2]     # 项目根
    return root / "outputs" / "_atomic_raw" / f"win_{book}.state.json"


def _save_state(path: Path, st: dict) -> None:
    """每个窗口跑完立刻落盘 —— 这是断点续跑的唯一依据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)          # 原子替换，防写一半被打断


def _repair_partition(atoms: list[dict], c0: int, c1: int,
                      real_chapters: set[int] | None = None) -> dict:
    """把 atoms 的章区间**确定性修成 [c0, c1] 的严格划分**（连续、不重叠、不遗漏）。

    为什么必须有这一步（2026-09-18 太荒全书实测）：
    - 模型做章号记账**不可靠** —— 39 个窗口里 **15 个（38%）有缺口**，全书共缺 84 章；
    - **重试只能缓解不能根治**（实测最差窗口：缺 24 章 → 重试后仍缺 8 章）；
    - 而"严格划分"是下游的硬需求（原子区间 = 溯源坐标）。→ 用**确定性修复**兜底。

    修复规则（就近吸收，可解释）：
    1) 重叠：按起点排序后，后一条的起点推到前一条末尾 +1；
    2) 缺口：把缺口章**吸进前一条**（叙事上"上一件事还在延续"更自然）；
       开头缺口吸进第一条，末尾缺口吸进最后一条；
    3) 每条被改动过的原子标 `span_repaired=True` 并记 `absorbed=[…]`，**不掩盖修复过的事实**。

    🔴 `real_chapters`（2026-09-19 加，**必传**）：源文件里**真实存在**的章号集合。
    为什么必须要：修复的下界曾直接取请求区间端点 `c0`/`c1`，但**源书可能缺首章/跳号**
    （实测凡人修仙传源文件从第 2 章开始）→ 「首尾补齐」会补出**源里根本不存在的章**，
    造成原子区间坐标不干净（下游按坐标取内容会取到空）。
    → 补齐只能在**真实章号**上做：`c0`/`c1` 先吸附到最接近的真实章。
    """
    if not atoms:
        return {"repaired": 0, "absorbed": 0, "dup_fixed": 0}
    items = sorted(atoms, key=lambda a: int(a.get("chapter_start") or 0))
    repaired = absorbed = dup_fixed = 0

    # 把请求区间端点吸附到真实章号（防补出源里不存在的章）
    if real_chapters:
        reals = sorted(real_chapters)
        in_win = [c for c in reals if c0 <= c <= c1]
        if in_win:
            c0, c1 = in_win[0], in_win[-1]

    for k, a in enumerate(items):
        a["_s"] = int(a.get("chapter_start") or 0)
        a["_e"] = int(a.get("chapter_end") or 0)

    # 0) 🔴 越界章号吸附（源缺首章/跳号时，模型可能切出源里不存在的章）
    #    把区间端点夹回**该原子附近真实存在**的章，而不是直接补空气。
    if real_chapters:
        clamped = 0
        for a in items:
            s, e = a["_s"], a["_e"]
            ns, ne = s, e
            if s not in real_chapters:                      # 起点越界 → 向后找第一个真实章
                cand = [c for c in sorted(real_chapters) if c >= s]
                if cand:
                    ns = cand[0]
            if e not in real_chapters:                      # 终点越界 → 向前找最后一个真实章
                cand = [c for c in sorted(real_chapters) if c <= e]
                if cand:
                    ne = cand[-1]
            if ns > ne:                                     # 区间塌缩：改成附近单个真实章
                near = [c for c in sorted(real_chapters) if abs(c - s) <= 3]
                if near:
                    ns = ne = min(near, key=lambda c: abs(c - s))
            if (ns, ne) != (s, e):
                a.setdefault("absorbed", [])
                a["span_repaired"] = True
                a["clamped_from"] = [s, e]
                clamped += 1
            a["_s"], a["_e"] = ns, ne
        if clamped:
            repaired += clamped

    # 1) 重叠
    for k in range(1, len(items)):
        if items[k]["_s"] <= items[k - 1]["_e"]:
            items[k]["_s"] = items[k - 1]["_e"] + 1
            items[k].setdefault("absorbed", [])
            items[k]["span_repaired"] = True
            dup_fixed += 1
    # 2) 缺口（把 gap 吸进前一条）
    for k, a in enumerate(items):
        prev_end = items[k - 1]["_e"] if k > 0 else c0 - 1
        gap0, gap1 = prev_end + 1, a["_s"] - 1
        # 🔴 只吸收**真实存在**的章（跳号/缺首章时不补空气，见 docstring）
        if real_chapters:
            rng = [c for c in range(gap0, gap1 + 1) if c in real_chapters]
        else:
            rng = list(range(gap0, gap1 + 1))
        if rng:
            target = items[k - 1] if k > 0 else a
            target.setdefault("absorbed", [])
            target["absorbed"].extend(rng)
            target["span_repaired"] = True
            if k > 0:
                target["_e"] = rng[-1]
            else:
                a["_s"] = rng[0]
            absorbed += len(rng)
            repaired += 1
        # 首条若不在 c0 起 → 向前补（同样只补真实章）
        if k == 0 and a["_s"] > c0:
            rng = [c for c in range(c0, a["_s"])
                   if not real_chapters or c in real_chapters]
            if rng:
                a.setdefault("absorbed", [])
                a["absorbed"].extend(rng)
                a["span_repaired"] = True
                absorbed += len(rng)
                a["_s"] = rng[0]
    # 3) 末条若不到 c1 → 向后补（同样只补真实章）
    if items[-1]["_e"] < c1:
        rng = [c for c in range(items[-1]["_e"] + 1, c1 + 1)
               if not real_chapters or c in real_chapters]
        if rng:
            items[-1].setdefault("absorbed", [])
            items[-1]["absorbed"].extend(rng)
            items[-1]["span_repaired"] = True
            absorbed += len(rng)
            items[-1]["_e"] = rng[-1]

    for a in items:
        a["chapter_start"], a["chapter_end"] = a["_s"], a["_e"]
        a.pop("_s", None)
        a.pop("_e", None)
    atoms[:] = items
    return {"repaired": repaired, "absorbed": absorbed, "dup_fixed": dup_fixed}


def _cov_of(atoms: list[dict]) -> tuple[set[int], list[int]]:
    """返回 (覆盖章集合, 有重复的章号列表)。"""
    lst: list[int] = []
    for a in atoms:
        x0, x1 = a.get("chapter_start"), a.get("chapter_end")
        if isinstance(x0, int) and isinstance(x1, int):
            lst.extend(range(x0, x1 + 1))
    dup = sorted({x for x in lst if lst.count(x) > 1})
    return set(lst), dup


def cut_window_retry(db, args, win_start: int, win_end: int,
                     all_chapters, atom_table: str, key: str, retries: int = 1):
    """跑一个窗口；**覆盖不全就重试**（取缺章最少的一次）。

    实测（太荒全书 39 窗）：**15 窗（38%）有缺口**，共缺 84 章 —— 模型在窗口内做章号记账不可靠。
    重试是必需的保护；两次都残缺时取较好的那次，并如实记进 win_log。
    """
    expect = set(range(win_start, win_end + 1))
    best = None
    for att in range(retries + 1):
        atoms, arcs, plen, rlen = cut_window(db, args, win_start, win_end,
                                            all_chapters, atom_table, key)
        cov, dup = _cov_of(atoms)
        n_miss = len(expect - cov)          # 🔴 用**修复前**的缺失数判定重试（修复后永远为 0）
        tag = "完整" if not n_miss else f"缺 {n_miss} 章"
        print(f"  第 {att + 1} 次尝试：atoms {len(atoms)}｜arcs {len(arcs)}｜原生覆盖 {tag}")
        if best is None or n_miss < best[0]:
            best = (n_miss, len(dup), atoms, arcs, plen, rlen)
        if n_miss == 0:
            break
    n_miss, n_dup, atoms, arcs, plen, rlen = best
    # 🔴 确定性兜底：保证严格划分。**必须传真实章号集合**，否则「首尾补齐」会补出
    #    源里不存在的章（实测凡人修仙传源文件从第 2 章起 → 曾被补出第 1 章，坐标不干净）。
    real = {no for no, _ in all_chapters}
    rep = _repair_partition(atoms, win_start, win_end, real_chapters=real)
    if rep["absorbed"] or rep["dup_fixed"]:
        print(f"  🔧 分区修复：吸收 {rep['absorbed']} 章 / 修重叠 {rep['dup_fixed']} 处"
              f"（原生缺 {n_miss} 章）→ 现为严格划分")
    return atoms, arcs, plen, rlen, n_miss, n_dup


def run_windowed(db, args, all_chapters: list[tuple[int, str]], atom_table: str,
                 valid_ids: set, originals: list[str], key: str) -> int:
    """§11.11 窗口化增量：每次只锁定「除最后一条弧之外」的弧，下个窗口从最后一条弧**回炉**。

    🔴 **断点续跑**：每个窗口跑完立刻把状态写盘（`--state`，默认 `outputs/_atomic_raw/win_<书>.state.json`）。
    重跑同一命令即从上次的 `next_win_start` 续跑；`--restart` 忽略旧状态从头来过。
    """
    from app.models.orm import AtomicEventORM
    first, book_last = all_chapters[0][0], all_chapters[-1][0]
    state_path = Path(args.state) if args.state else _default_state_path(args.book)

    locked_arcs: list[dict] = []
    locked_atoms: list[dict] = []
    win_log: list[dict] = []
    lock_ceiling = 0
    win_start = args.start
    cur_win = args.window          # 当前窗长（可被自适应扩大，见下）
    done_windows = 0

    if state_path.exists() and not args.restart:
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 状态文件解析失败（{type(e).__name__}: {e}），从头开始")
            st = {}
        if st and st.get("book") == args.book and st.get("window") == args.window:
            locked_arcs = st.get("locked_arcs") or []
            locked_atoms = st.get("locked_atoms") or []
            win_log = st.get("win_log") or []
            lock_ceiling = int(st.get("lock_ceiling") or 0)
            win_start = int(st.get("next_win_start") or args.start)
            done_windows = int(st.get("done_windows") or 0)
            print(f"\n🔁 断点续跑：读到状态 {state_path}")
            print(f"   已完成 {done_windows} 窗｜已锁定 {len(locked_arcs)} 弧 / "
                  f"{len(locked_atoms)} 原子｜已锁到第 {lock_ceiling} 章"
                  f"｜本窗从第 {win_start} 章开始")
        elif st:
            print(f"⚠️ 状态文件与本次参数不匹配（book/window 不同），从头开始")

    if win_start > book_last:
        print("✅ 状态显示已跑到书末，无需再跑（--restart 可重跑）")
    else:
        for it in range(done_windows + 1, done_windows + args.max_windows + 1):
            win_end = min(book_last, win_start + cur_win - 1)
            print(f"\n{'=' * 96}\n窗口 {it}：[{win_start}, {win_end}]"
                  f"（{win_end - win_start + 1} 章）｜上次已锁定到第 {lock_ceiling} 章")
            atoms, arcs, plen, rlen, wmiss_n, wdup_n = cut_window_retry(
                db, args, win_start, win_end, all_chapters, atom_table, key,
                retries=args.retries)
            print(f"  prompt {plen} 字符 → 返回 {rlen} 字符｜atoms {len(atoms)}｜arcs {len(arcs)}")
            if not arcs:
                print("  ❌ 该窗口未返回 arcs，停止")
                break

            reach_end = win_end >= book_last
            hold = arcs if reach_end else arcs[:-1]     # 到达书末 → 全部锁定
            if hold:
                held_seqs = {s for a in hold for s in (a.get("atoms") or [])}
                locked_arcs.extend(hold)
                locked_atoms.extend(a for i, a in enumerate(atoms, 1)
                                    if a.get("seq", i) in held_seqs)
                new_ceiling = max(int(a.get("chapter_end") or 0) for a in hold)
                print(f"  锁定 {len(hold)} 条弧（到第 {new_ceiling} 章）"
                      f"｜回炉 1 条：{arcs[-1].get('arc_name')}"
                      f"（{arcs[-1].get('chapter_start')}~{arcs[-1].get('chapter_end')}）"
                      if not reach_end else
                      f"  到达书末 → 锁定全部 {len(hold)} 条弧（到第 {new_ceiling} 章）")
                win_log.append({"window": it, "start": win_start, "end": win_end,
                                "arcs_total": len(arcs), "locked": len(hold),
                                "locked_to": new_ceiling, "reach_end": reach_end})

            # 🔴 每窗口覆盖校验（用重试后的最优结果）
            wexpect = set(range(win_start, win_end + 1))
            wcov, wdup = _cov_of(atoms)
            wmiss = sorted(wexpect - wcov)
            if wmiss:
                print(f"  ❌ 本窗口覆盖不全（重试后仍缺）：缺 {len(wmiss)} 章 → {wmiss[:16]}"
                      f"{' …' if len(wmiss) > 16 else ''}")
                if win_log:
                    win_log[-1]["miss"] = len(wmiss)
            elif wdup:
                print(f"  ⚠️ 本窗口有重叠：{len(wdup)} 章 → {wdup[:16]}")
                if win_log:
                    win_log[-1]["dup"] = len(wdup)
            else:
                print("  ✅ 本窗口覆盖完整")

            if reach_end:
                # 🔴 修 bug：这里必须把 lock_ceiling 推到本窗最大章末，否则汇总/状态会显示成上一窗的值
                if hold:
                    lock_ceiling = max(lock_ceiling,
                                       max(int(a.get("chapter_end") or 0) for a in hold))
                print(f"  ✅ 窗口已覆盖书末，结束（本次锁到第 {lock_ceiling} 章）")
                _save_state(state_path, {
                    "book": args.book, "window": args.window, "next_win_start": book_last + 1,
                    "lock_ceiling": lock_ceiling, "done_windows": it, "finished": True,
                    "locked_arcs": locked_arcs, "locked_atoms": locked_atoms, "win_log": win_log})
                break

            nxt = int(arcs[-1].get("chapter_start") or 0)
            if nxt <= win_start:      # 没前进 → 防死循环：向后挪半窗
                nxt = win_start + max(1, cur_win // 2)
                print(f"  ⚠️ 最后一条弧起点未前进，强制推进到第 {nxt} 章")

            # 🔴 自适应扩窗：窗口必须 ≥「回炉那条弧」的长度，否则它永远看不全、必被切断。
            #    实测弧长：太荒新弧 max 28 章、全库 v3 弧 max 74 章（有 5 条 ≥40）→ 固定窗长会漏。
            held = int(arcs[-1].get("chapter_end") or 0) - \
                int(arcs[-1].get("chapter_start") or 0) + 1
            if held >= cur_win - 5:
                new_win = min(max(args.window, held + 10), args.window_max)
                if new_win != cur_win:
                    print(f"  🔧 自适应扩窗：回炉弧长 {held} 章 ≥ 窗长−5 → "
                          f"下一窗 {cur_win} → {new_win} 章（上限 {args.window_max}）")
                cur_win = new_win
            else:
                cur_win = args.window

            lock_ceiling = lock_ceiling if not hold else max(
                lock_ceiling, max(int(a.get("chapter_end") or 0) for a in hold))
            win_start = nxt
            # 🔴 每个窗口跑完立刻落盘（断点续跑的唯一依据）
            _save_state(state_path, {
                "book": args.book, "window": args.window, "next_win_start": win_start,
                "lock_ceiling": lock_ceiling, "done_windows": it, "finished": False,
                "locked_arcs": locked_arcs, "locked_atoms": locked_atoms, "win_log": win_log})
            print(f"  💾 状态已保存（下一窗从第 {win_start} 章 | {state_path.name}）")
        else:      # for-else：窗口步数用尽（未跑完 → 状态已存，可续跑）
            print(f"\n⚠️ 达到 --max-windows {args.max_windows} 上限而停止（状态已保存，可续跑）")

    # ---------------- 汇总与校验 ----------------
    total_chips = sum(max(0, int(a.get("chapter_end") or 0)
                          - int(a.get("chapter_start") or 0) + 1) for a in locked_atoms)
    print(f"\n{'=' * 96}\n汇总（窗口步数 {len(win_log)}）\n{'=' * 96}")
    print(f"  锁定弧 {len(locked_arcs)}｜锁定原子 {len(locked_atoms)}"
          f"｜覆盖 {total_chips} 章（第{first}~{lock_ceiling}章）")
    print(f"  平均 {len(locked_atoms) / max(len(locked_arcs), 1):.1f} 原子/弧")

    out_of_table, leaks, bad_len = [], 0, 0
    for a in locked_atoms:
        aid = str(a.get("atomic_id") or "").strip()
        if aid and aid not in valid_ids:
            out_of_table.append(f"seq{a.get('seq')}={aid}")
        sm = str(a.get("summary") or "")
        if not (SUM_MIN <= len(sm) <= SUM_MAX):
            bad_len += 1
        blob = sm + "".join(str(t) for t in (a.get("tags") or []))
        if any(o in blob for o in originals):
            leaks += 1
    print(f"  越表 {len(out_of_table)}｜长度越界 {bad_len}｜专名疑似 {leaks}")
    if out_of_table:
        print(f"    越表样例：{out_of_table[:8]}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"book": args.book, "windows": win_log,
             "arcs": locked_arcs, "atoms": locked_atoms},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  结果 JSON → {args.json_out}")

    if args.report:
        lines = [f"# 试验（窗口化增量）：切原子 + 合成弧（{args.book} 第{first}~{lock_ceiling}章）", "",
                 f"- 窗口 {len(win_log)} 步（WINDOW={args.window}）",
                 f"- 锁定弧 **{len(locked_arcs)}**｜锁定原子 **{len(locked_atoms)}**"
                 f"（平均 {len(locked_atoms) / max(len(locked_arcs), 1):.1f} 原子/弧）",
                 f"- 越表 {len(out_of_table)}｜长度越界 {bad_len}｜专名疑似 {leaks}", "",
                 "## 窗口推进记录", "",
                 "| 窗口 | 章区间 | 本次切出弧 | 锁定 | 锁定到章 | 到书末 |",
                 "|---|---|---|---|---|---|"]
        for w in win_log:
            lines.append(f"| {w['window']} | {w['start']}~{w['end']} | {w['arcs_total']} | "
                         f"{w['locked']} | {w['locked_to']} | {'是' if w['reach_end'] else '否'} |")
        lines += ["", "## 弧一览", "", "| # | 弧名 | 章区间 | 核心原子 | 序列 |", "|---|---|---|---|---|"]
        for k, arc in enumerate(locked_arcs, 1):
            chain = []
            for s in (arc.get("atoms") or []):
                a = next((x for x in locked_atoms if x.get("seq") == s), None)
                chain.append(str(a.get("atomic_id")) if a else f"?{s}")
            lines.append(f"| {k} | {arc.get('arc_name')} | {arc.get('chapter_start')}~"
                         f"{arc.get('chapter_end')} | {arc.get('core_atomics')} | "
                         f"{' → '.join(chain)} |")
        lines += ["", "## 原子事件", "",
                  "| seq | 章区间 | 原子 | 原子名 | 概括字数 | 概括 |", "|---|---|---|---|---|---|"]
        for a in locked_atoms:
            aid = str(a.get("atomic_id") or "（空）")
            nm = db.query(AtomicEventORM.name).filter(AtomicEventORM.id == aid).scalar() or ""
            lines.append(f"| {a.get('seq')} | {a.get('chapter_start')}~{a.get('chapter_end')} | "
                         f"{aid} | {nm} | {len(str(a.get('summary') or ''))} | "
                         f"{a.get('summary')} |")
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")
        print(f"  报告 → {args.report}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="【试验】从逐章概括切原子 + 合成弧")
    ap.add_argument("--book", default="太荒吞天诀")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true", help="只打印输入与 prompt 规模，不调模型")
    ap.add_argument("--prompt-out", default=None)
    ap.add_argument("--raw-out", default=None, help="模型原始输出写到该文件")
    ap.add_argument("--json-out", default=None, help="解析后的 JSON 写到该文件")
    ap.add_argument("--report", default=None, help="markdown 对照表写到该文件")
    ap.add_argument("--from-json", default=None,
                    help="不调模型，直接读该 JSON（--json-out 的产物）重出报告/校验")
    ap.add_argument("--window", type=int, default=0,
                    help="窗口化增量模式：每次发 N 章（**建议 50**；实测 40 可靠/100 会缺章）。"
                         ">0 时启用 §11.11 协议，--end 被忽略（自动跑到书末）")
    ap.add_argument("--window-max", type=int, default=80,
                    help="自适应扩窗上限（回炉弧过长时临时扩窗；越大覆盖越不可靠）")
    ap.add_argument("--max-windows", type=int, default=200,
                    help="窗口化模式的最大窗口步数（防跑飞；先小步验证时给 2）")
    ap.add_argument("--state", default=None,
                    help="断点续跑状态文件（默认 outputs/_atomic_raw/win_<书>.state.json）")
    ap.add_argument("--restart", action="store_true", help="忽略已有状态，从头跑")
    ap.add_argument("--retries", type=int, default=1,
                    help="窗口覆盖不全时的重试次数（默认 1；实测 38% 窗口会缺章，重试必要）")
    ap.add_argument("--repair-json", default=None,
                    help="抢救模式：对已有 JSON（--json-out 产物）做确定性分区修复，不调模型")
    args = ap.parse_args()

    import app.core.database as dbmod
    from app.models.orm import AtomicEventORM

    eng = create_engine(dbmod.DEFAULT_DB_URL, poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    db = sessionmaker(bind=eng)()
    dbmod.init_db()

    # ---- 抢救模式：对已有 JSON 做确定性分区修复（不调模型）----
    if args.repair_json:
        src = json.loads(Path(args.repair_json).read_text(encoding="utf-8"))
        ats = src.get("atoms") or []
        bk = src.get("book") or args.book
        books_ch = db.execute(text(
            "SELECT MIN(chapter_no), MAX(chapter_no) FROM chapter_summaries "
            "WHERE book_name=:b AND summary IS NOT NULL"),
            {"b": bk}).fetchone()
        c0, c1 = int(books_ch[0]), int(books_ch[1])
        # 🔴 真实章号集合（源里可能存在跳号/缺首章；补齐时不能补出空气章）
        real = {int(r[0]) for r in db.execute(text(
            "SELECT DISTINCT chapter_no FROM chapter_summaries "
            "WHERE book_name=:b AND summary IS NOT NULL"), {"b": bk}).fetchall()}
        span = {c for c in real if c0 <= c <= c1}
        pre_cov, _ = _cov_of(ats)
        pre_miss = len(span - pre_cov)
        rep = _repair_partition(ats, c0, c1, real_chapters=real)
        post_cov, post_dup = _cov_of(ats)
        post_miss = len(span - post_cov)
        print(f"分区修复：[{c0},{c1}]（真实章 {len(span)} 个）"
              f"｜修复前缺 {pre_miss} 章 → 修复后缺 {post_miss}"
              f"｜吸收 {rep['absorbed']} 章｜修重叠 {rep['dup_fixed']} 处"
              f"｜重叠残留 {len(post_dup)}"
              f"｜越界 {len(post_cov - span)}")
        out = Path(args.json_out) if args.json_out else Path(args.repair_json)
        src["atoms"] = ats
        src["partition_repair"] = {**rep, "pre_missing": pre_miss, "post_missing": post_miss,
                                   "real_chapters": len(span), "out_of_span": len(post_cov - span)}
        out.write_text(json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写回 {out}")
        db.close()
        return 0

    rows = db.execute(text("""
        SELECT chapter_no, summary, arc_no FROM chapter_summaries
        WHERE book_name=:b AND chapter_no BETWEEN :a AND :z AND summary IS NOT NULL
        ORDER BY chapter_no"""), {"b": args.book, "a": args.start, "z": args.end}).fetchall()
    chapters = [(int(r[0]), str(r[1]).strip()) for r in rows if r[1]]
    if not chapters and args.window <= 0:
        print("❌ 该章区间无逐章概括")
        return 2
    c0, c1 = (chapters[0][0], chapters[-1][0]) if chapters else (args.start, args.end)
    chars = sum(len(s) for _, s in chapters)
    if chapters:
        print(f"{args.book} 第{c0}~{c1}章：{len(chapters)} 条逐章概括，合计 {chars} 字"
              f"（约 {int(chars * 0.7)} token 输入）")

    # 现有弧（对比基线）
    base: dict[int, tuple] = {}
    for arc_no, c_lo, c_hi, n in db.execute(text("""
            SELECT arc_no, MIN(chapter_no), MAX(chapter_no), COUNT(*)
            FROM chapter_summaries WHERE book_name=:b AND chapter_no BETWEEN :a AND :z
              AND arc_no IS NOT NULL GROUP BY arc_no ORDER BY arc_no"""),
            {"b": args.book, "a": args.start, "z": args.end}).fetchall():
        base[arc_no] = (c_lo, c_hi, n)
    print(f"现有弧基线：{len(base)} 条 → "
          + "｜".join(f"弧{k}({v[0]}~{v[1]})" for k, v in base.items()))

    atom_table = build_atom_table(db)
    valid_ids = {r[0] for r in db.query(AtomicEventORM.id).all()}
    originals = load_originals(db, args.book)
    print(f"词表 {len(valid_ids)} 条（{len(atom_table)} 字符）｜专名原名表 {len(originals)} 条")

    # ---- 窗口化增量模式（§11.11）：自动跑到书末，不落库 ----
    if args.window > 0 and not args.from_json:
        all_rows = db.execute(text("""
            SELECT chapter_no, summary FROM chapter_summaries
            WHERE book_name=:b AND chapter_no >= :a AND summary IS NOT NULL
            ORDER BY chapter_no"""), {"b": args.book, "a": args.start}).fetchall()
        all_chapters = [(int(r[0]), str(r[1]).strip()) for r in all_rows if r[1]]
        if not all_chapters:
            print("❌ 无逐章概括")
            return 2
        print(f"窗口化模式：WINDOW={args.window}｜{args.book} 第{all_chapters[0][0]}~"
              f"{all_chapters[-1][0]} 章（{len(all_chapters)} 条概括）")

        def _fake(ch, chs):   # 不需要真书目录；--book 已足够（prompt 只用概括）
            return None
        if args.dry_run:
            print("[dry-run] 窗口化模式不调模型")
            db.close()
            return 0
        from app.services import plot_import as pi
        key = pi.ms_key(db)
        if not key:
            print("❌ 未取到魔搭 key")
            return 3
        rc = run_windowed(db, args, all_chapters, atom_table, valid_ids, originals, key)
        db.close()
        return rc

    prompt = build_prompt(args.book, c0, c1, chapters, atom_table)
    print(f"prompt 合计 {len(prompt)} 字符（约 {int(len(prompt) * 0.7)} token）")
    if args.prompt_out:
        Path(args.prompt_out).write_text(prompt, encoding="utf-8")
        print(f"prompt 已写入 {args.prompt_out}")
    if args.dry_run and not args.from_json:
        print("[dry-run] 不调模型")
        db.close()
        return 0

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        atoms = data.get("atoms") or []
        arcs = data.get("arcs") or []
        print(f"[from-json] 读入 {args.from_json}：atoms {len(atoms)}｜arcs {len(arcs)}")
    else:
        from app.services import plot_import as pi
        key = pi.ms_key(db)
        if not key:
            print("❌ 未取到魔搭 key")
            return 3
        # 🔴 打印密钥指纹：多进程并行时用的是不同 key（NA_MS_KEY 注入），
        #    不显示就分不清"两个进程到底是不是两套额度"（2026-09-19 踩过：
        #    两进程误用同一把 key → 共享额度、互相限流，且日志上看不出来）。
        #    2026-09-25 网关模式（llm.use_gateway=true）：三路全走本地统一网关一把 Key，
        #    NA_MS_KEY 被忽略（额度轮换由网关内部完成），来源打印为 llm.gateway_key。
        if pi.GW_ACTIVE:
            _model_label, _key_src = f"网关 {pi.GW_MS_MODEL}", "网关 llm.gateway_key"
        else:
            _model_label = f"魔搭 {pi.MS_MODEL}"
            _key_src = ('ENV NA_MS_KEY' if __import__('os').environ.get('NA_MS_KEY')
                        else 'app_configs')
        print(f"调用{_model_label}（max_tokens={MAX_OUT_TOKENS}，关思考）"
              f"｜key=…{key[-8:]}｜来源={_key_src}")
        raw = pi._ms_post(key, prompt, max_tokens=MAX_OUT_TOKENS, temperature=0.2,
                          timeout=900, thinking=False)
        print(f"返回 {len(raw)} 字符")
        if args.raw_out:
            Path(args.raw_out).write_text(raw, encoding="utf-8")

        data = parse_items(raw)
        atoms = data.get("atoms") or []
        arcs = data.get("arcs") or []
        print(f"解析：atoms {len(atoms)}｜arcs {len(arcs)}")
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps({"atoms": atoms, "arcs": arcs}, ensure_ascii=False, indent=2),
                encoding="utf-8")

    # ---------------- 校验 ----------------
    print("\n" + "=" * 96)
    print("校验")
    print("=" * 96)
    covered: list[int] = []
    leaks = 0
    bad_len = 0
    out_of_table: list[str] = []
    no_atom: list[int] = []
    for i, a in enumerate(atoms, 1):
        seq = a.get("seq", i)
        aid = str(a.get("atomic_id") or "").strip()
        s0, s1 = a.get("chapter_start"), a.get("chapter_end")
        sm = str(a.get("summary") or "")
        tags = a.get("tags") or []
        if isinstance(s0, int) and isinstance(s1, int):
            covered.extend(range(s0, s1 + 1))
        if aid and aid not in valid_ids:
            out_of_table.append(f"seq{seq}={aid}")
        if not aid:
            no_atom.append(int(seq) if str(seq).isdigit() else i)
        if not (SUM_MIN <= len(sm) <= SUM_MAX):
            bad_len += 1
        blob = sm + "".join(str(t) for t in tags)
        hit = [o for o in originals if o in blob]
        if hit:
            leaks += 1
            print(f"  ⚠️ seq{seq} 专名泄漏: {hit[:4]}")

    expect = set(range(c0, c1 + 1))
    miss = sorted(expect - set(covered))
    dup = sorted({x for x in covered if covered.count(x) > 1}) if covered else []
    print(f"  章覆盖：{len(set(covered))}/{len(expect)}｜缺 {len(miss)} 章"
          + (f" → {miss[:10]}" if miss else " ✅ 无缺口")
          + f"｜重叠 {len(dup)} 章" + (f" → {dup[:10]}" if dup else ""))
    print(f"  越表 atomic_id：{len(out_of_table)}" + (f" → {out_of_table}" if out_of_table else " ✅"))
    print(f"  无合适原子：{len(no_atom)}" + (f" → seq {no_atom}" if no_atom else " ✅"))
    print(f"  专名泄漏：{leaks} 条" + (" ✅" if not leaks else ""))
    print(f"  概括长度越界（{SUM_MIN}~{SUM_MAX}）：{bad_len} 条" + (" ✅" if not bad_len else ""))

    lens = [len(str(a.get("summary") or "")) for a in atoms]
    if lens:
        print(f"  概括字数 min/中位/max：{min(lens)}/{sorted(lens)[len(lens) // 2]}/{max(lens)}")

    # ---------------- 打印结果 ----------------
    print("\n" + "=" * 96)
    print("原子事件（切出来的边界与概括）")
    print("=" * 96)
    for i, a in enumerate(atoms, 1):
        aid = str(a.get("atomic_id") or "（空）")
        nm = ""
        e = db.query(AtomicEventORM.name).filter(AtomicEventORM.id == aid).scalar()
        if e:
            nm = e
        print(f"\n[{a.get('seq', i)}] {a.get('chapter_start')}~{a.get('chapter_end')}章 "
              f"→ {aid} {nm}")
        print(f"    {a.get('summary')}")
        print(f"    tags: {a.get('tags')}")

    print("\n" + "=" * 96)
    print("故事弧（新切出的弧 vs 现有弧基线）")
    print("=" * 96)
    for k, arc in enumerate(arcs, 1):
        print(f"\n弧{k}「{arc.get('arc_name')}」{arc.get('chapter_start')}~"
              f"{arc.get('chapter_end')}章｜核心 {arc.get('core_atomics')}"
              f"｜原子 {arc.get('atoms')}")
        seqs = arc.get("atoms") or []
        chain = []
        for s in seqs:
            a = next((x for x in atoms if x.get("seq") == s), None)
            chain.append(str(a.get("atomic_id")) if a else f"?{s}")
        print(f"    序列：{' → '.join(chain)}")
    print("\n现有弧基线：")
    for arc_no, (lo, hi, n) in base.items():
        print(f"    弧{arc_no} 第{lo}~{hi}章（{n} 章）")

    if args.report:
        # 新弧 vs 现有弧 一一对照（按章区间重叠最大的那条配对）
        pairs = []
        base_list = sorted(base.items())
        for k, arc in enumerate(arcs, 1):
            a0, a1 = arc.get("chapter_start"), arc.get("chapter_end")
            best, best_ov = None, -1
            if isinstance(a0, int) and isinstance(a1, int):
                for arc_no, (lo, hi, n) in base_list:
                    ov = max(0, min(a1, hi) - max(a0, lo) + 1)
                    if ov > best_ov:
                        best, best_ov = (arc_no, lo, hi, n), ov
            pairs.append((k, arc, best, best_ov))

        lines = [f"# 试验：从逐章概括切原子 + 合成弧（{args.book} 第{c0}~{c1}章）", "",
                 f"- 输入：{len(chapters)} 条逐章概括（{chars} 字 ≈ {int(chars * 0.7)} token）",
                 f"- 产出：atoms **{len(atoms)}**｜arcs **{len(arcs)}**"
                 f"（平均 {len(atoms) / max(len(arcs), 1):.1f} 原子/弧）",
                 f"- 章覆盖 {len(set(covered))}/{len(expect)}（缺 {len(miss)}｜重叠 {len(dup)}）"
                 f"｜越表 {len(out_of_table)}｜无合适原子 {len(no_atom)}"
                 f"｜专名疑似 {leaks}｜长度越界 {bad_len}", "",
                 "## 1. 新弧 vs 现有 v3 弧（一一对照）", "",
                 "| # | 新弧名 | 新弧章区间 | 原子数 | 现有弧 | 现有章区间 | 边界差异 |",
                 "|---|---|---|---|---|---|---|"]
        for k, arc, best, ov in pairs:
            aseq = arc.get("atoms") or []
            if best:
                bno, lo, hi, n = best
                diff = []
                if arc.get("chapter_start") != lo:
                    diff.append(f"头 {arc.get('chapter_start') - lo:+d}")
                if arc.get("chapter_end") != hi:
                    diff.append(f"尾 {arc.get('chapter_end') - hi:+d}")
                d = "、".join(diff) or "**完全一致**"
                btxt = f"弧{bno}（{n} 章）"
                brange = f"{lo}~{hi}"
            else:
                btxt, brange, d = "（无）", "—", "—"
            lines.append(f"| {k} | {arc.get('arc_name')} | "
                         f"{arc.get('chapter_start')}~{arc.get('chapter_end')} | "
                         f"{len(aseq)} | {btxt} | {brange} | {d} |")

        lines += ["", "## 2. 原子事件", "",
                  "| seq | 章区间 | 原子 | 原子名 | 概括字数 | tags | 概括 |",
                  "|---|---|---|---|---|---|---|"]
        for i, a in enumerate(atoms, 1):
            aid = str(a.get("atomic_id") or "（空）")
            nm = db.query(AtomicEventORM.name).filter(AtomicEventORM.id == aid).scalar() or ""
            lines.append(f"| {a.get('seq', i)} | {a.get('chapter_start')}~"
                         f"{a.get('chapter_end')} | {aid} | {nm} | "
                         f"{len(str(a.get('summary') or ''))} | {a.get('tags')} | "
                         f"{a.get('summary')} |")

        lines += ["", "## 3. 故事弧（新切）", "",
                  "| # | 弧名 | 章区间 | 核心原子 | 序列 |", "|---|---|---|---|---|"]
        for k, arc in enumerate(arcs, 1):
            seqs = arc.get("atoms") or []
            chain = []
            for s in seqs:
                a = next((x for x in atoms if x.get("seq") == s), None)
                chain.append(str(a.get("atomic_id")) if a else f"?{s}")
            lines.append(f"| {k} | {arc.get('arc_name')} | {arc.get('chapter_start')}~"
                         f"{arc.get('chapter_end')} | {arc.get('core_atomics')} | "
                         f"{' → '.join(chain)} |")

        lines += ["", "## 4. 现有弧基线", "", "| 弧号 | 章区间 | 章数 |", "|---|---|---|"]
        for arc_no, (lo, hi, n) in base_list:
            lines.append(f"| {arc_no} | {lo}~{hi} | {n} |")

        Path(args.report).write_text("\n".join(lines), encoding="utf-8")
        print(f"\n报告已写入 {args.report}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
