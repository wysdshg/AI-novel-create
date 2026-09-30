# -*- coding: utf-8 -*-
"""为原子骨架模板生成「角色功能位」(cast)（2026-09-19，用户提出：新模板没有角色槽位）。

输入 = 每条 active 骨架的 环序列 + 各环实证内容（`variants[].desc`，入库 v2 已带）；
输出 = `cast: [{slot, desc, mode, beats}]` —— slot/mode 体系对齐旧链路
（mode ∈ 助力 / 阻碍 / 对手 / 见证；槽位数 2~5；必含一个主角位）。

🔴 数据安全（用户强调「别直接给我删了还找不回来」）：
- **只写 `structure["cast"]` 一个键**，phases / skeleton / 其他字段一律不动；
- `--apply` 前自动把全部 structure 备份到 `outputs/_backup/`（带时间戳）；
- `--rollback <backup.json>` 从备份还原 structure（只还原该字段）；
- **默认 dry-run**：只跑 --limit 条并把生成结果打印出来，不写库。

用法：
  $PY scripts/label_skeleton_casts.py --limit 2            # 试跑看质量
  $PY scripts/label_skeleton_casts.py --apply              # 全量写库（先自动备份）
  $PY scripts/label_skeleton_casts.py --rollback <file>    # 从备份还原
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

MODES = {"助力", "阻碍", "对手", "见证"}

PROMPT = """你在为一个**通用情节骨架**提炼「角色功能位」（cast）。

## 骨架
名称：{name}
环序列（已是多本书实证的通用顺序）：
{rings}

## 要求
输出 JSON（不要多余文字）：
{{"cast": [
  {{"slot": "功能位名（2~6 字，功能定位命名，如 主角 / 敌对长老 / 引路人师长 / 被救盟友）",
    "desc": "一句话说明该位在这个骨架里承担什么功能",
    "mode": "助力|阻碍|对手|见证",
    "beats": ["该位参与的关键环名，1~3 个，用上面环序列里的环名原文"]}}
]}}

## 纪律
1. **2~5 个槽位，必须含一个「主角」位**（slot 就写「主角」）。
2. slot 用**功能定位**命名；**禁止**出现具体人名、门派名、书名、专有名词。
3. desc 是对该位功能的**归纳**（20~40 字），不要照抄实证原文。
4. mode 四选一：助力（帮主角）/ 阻碍（设障但非直接敌对）/ 对手（正面敌对）/ 见证（旁观或衬托）。
5. beats 只填环序列里真实存在的环名（含原子号，如「A01 单挑决斗」）。
"""


def build_rings_block(t: dict) -> str:
    """环序列 + 每环 2 条实证内容（截断），供 LLM 提炼角色位。"""
    out = []
    i = 0
    for ph in t.get("phases") or []:
        for b in ph.get("beats") or []:
            i += 1
            lines = [f"{i}. [{ph.get('phase')}] {b.get('beat')}"]
            for v in (b.get("variants") or [])[:2]:
                d = (v.get("desc") or "").strip()
                if d:
                    lines.append(f"   - 实证（{v.get('src')}）：{d[:160]}")
            out.append("\n".join(lines))
    return "\n".join(out)


def attach_srcs(cast: list, structure: dict) -> None:
    """给每个槽位补 `srcs`（出处 = 该槽位 beats 所涉环的实证弧来源书）。
    旧模板 srcs 形如 [{book, alias}]；新骨架无逐书代称映射 → alias 用 slot 名本身
    （骨架的实证文本里角色本就是「主角/长老」这类泛称）。"""
    beat_srcs: dict[str, list[str]] = {}
    for ph in structure.get("phases") or []:
        for b in ph.get("beats") or []:
            books = []
            for v in (b.get("variants") or []):
                if v.get("src") and v["src"] not in books:
                    books.append(v["src"])
            beat_srcs[b.get("beat") or ""] = books
    for c in cast:
        books: list[str] = []
        for bn in (c.get("beats") or []):
            for bk in beat_srcs.get(bn, []):
                if bk not in books:
                    books.append(bk)
        c["srcs"] = [{"book": bk, "alias": str(c.get("slot") or "")} for bk in books[:6]]


def validate(cast: list) -> tuple[bool, str]:
    if not isinstance(cast, list) or not cast:
        return False, "cast 为空"
    if len(cast) < 2 or len(cast) > 5:
        return False, f"槽位数 {len(cast)} 不在 2~5"
    slots = [str(c.get("slot") or "").strip() for c in cast]
    if any(not s for s in slots):
        return False, "存在空 slot"
    if not any("主角" in s for s in slots):
        return False, "缺主角位"
    for c in cast:
        if not str(c.get("desc") or "").strip():
            return False, f"{c.get('slot')} 缺 desc"
        if str(c.get("mode") or "").strip() not in MODES:
            return False, f"{c.get('slot')} mode 非法：{c.get('mode')}"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条（dry-run 建议 2）")
    ap.add_argument("--only-missing", action="store_true",
                    help="只处理**还没有 cast 的**模板（2026-09-21 加：避免覆盖已生成好的 75 条）")
    ap.add_argument("--rollback", default=None, help="从备份 JSON 还原 structure")
    ap.add_argument("--outdir", default=str(ROOT / "outputs" / "_backup"))
    args = ap.parse_args()

    from sqlalchemy.pool import StaticPool
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.models.orm import PlotTemplateORM
    from app.services.plot_import import _ds_post, ds_key
    from app.services.plot_distill import parse_json_loose

    db_path = "C:/Users/w3013/.ai_novel/data/novel_agent.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db = sessionmaker(bind=engine)()

    # ---- 回滚模式 ----
    if args.rollback:
        bak = json.loads(Path(args.rollback).read_text(encoding="utf-8"))
        rows = bak.get("templates") or []
        n = 0
        for r in rows:
            t = db.query(PlotTemplateORM).filter_by(id=r["id"]).first()
            if t is None:
                print(f"  ⚠️ 跳过（模板不存在）：{r['id']}")
                continue
            t.structure = r["structure"]
            n += 1
        db.commit()
        db.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
        db.close()
        print(f"✅ 已从备份还原 {n} 条 structure（{args.rollback}）")
        return 0

    rows = (db.query(PlotTemplateORM)
            .filter(PlotTemplateORM.status == "active")
            .order_by(PlotTemplateORM.created_at).all())
    if args.only_missing:
        rows = [t for t in rows if not (t.structure or {}).get("cast")]
        print(f"[only-missing] 过滤后 {len(rows)} 条（无 cast 的模板）")
    if args.limit:
        rows = rows[:args.limit]
    print(f"待处理 active 模板：{len(rows)} 条（{'APPLY' if args.apply else 'DRY-RUN'}）")

    # ---- 写库前自动备份（全量 structure，可 --rollback 还原）----
    bak_path = None
    if args.apply:
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = outdir / f"plot_templates_active_structure_{ts}.json"
        all_rows = db.query(PlotTemplateORM).filter_by(status="active").all()
        bak_path.write_text(json.dumps({
            "note": "骨架 cast 生成前的 structure 全量备份（--rollback 用）",
            "created_at": ts,
            "templates": [{"id": t.id, "name": t.name, "structure": t.structure} for t in all_rows],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ 已备份 {len(all_rows)} 条 structure → {bak_path}")

    key = ds_key(db)
    ok_n, fail_n, results = 0, 0, []
    for t in rows:
        st = dict(t.structure or {})
        prompt = PROMPT.format(name=t.name, rings=build_rings_block(st))
        cast = None
        for attempt in range(2):
            try:
                raw = _ds_post(key, prompt, max_tokens=1200, temperature=0.3)
                data = parse_json_loose(raw) or {}
                cast = data.get("cast")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️ {t.name}: 调用异常（第 {attempt + 1} 次）{type(e).__name__}: {str(e)[:80]}")
                continue
            good, why = validate(cast or [])
            if good:
                break
            print(f"  ⚠️ {t.name}: 第 {attempt + 1} 次校验不过（{why}），重试…")
            cast = None
        if not cast:
            fail_n += 1
            print(f"  🔴 {t.name}: 生成失败（跳过，不影响其他模板）")
            continue
        results.append((t.name, cast))
        if args.apply:
            # 🔴 只改 cast 一个键
            attach_srcs(cast, st)
            st["cast"] = cast
            t.structure = st
            ok_n += 1
        else:
            print(f"\n■ {t.name}")
            print(json.dumps(cast, ensure_ascii=False, indent=1)[:900])

    if args.apply:
        db.commit()
        db.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
        print(f"\n✅ 写入 {ok_n} 条｜失败 {fail_n}｜备份 {bak_path}")
    else:
        print(f"\n（DRY-RUN）成功 {len(results)}｜失败 {fail_n}｜写库请加 --apply")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
