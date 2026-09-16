"""小说导入 CLI（Phase 7.1）：目录 → 逐章概括 → **匿名化** → 情节段 → 分类 → 故事弧 → 报告。

供 OpenCode / 人工跑批。用法（在 backend/ 下执行）：

    ..\\.venv\\Scripts\\python.exe scripts/import_novel.py ^
        --book-dir "E:\\AI小说创作\\小说\\斗破苍穹 - 天蚕土豆" ^
        --book-name "斗破苍穹" --start 1 --end 50 --stage all

阶段（建议按书分开跑，出问题好定位）：
  summarize  逐章概括（最慢，幂等断点续跑：中断后重跑同一命令自动跳过已完成章）
  anonymize  专名匿名化（**已并入 all**；单独跑用于补跑旧数据。会**重抽映射**，烧 LLM 钱）
  reanonymize 用**现有映射**补扫 summary/segment/arc 三个字段（**零 LLM 调用**；
              用于修复"弧阶段复燃"的存量数据 —— 见下方 🔴 2026-09-14 说明）
  segment    情节段切分（重算会清旧段标记）
  label      段分类
  arc        故事弧归并（DeepSeek）
  all        概括 → **匿名化** → 段 → 分类 → 弧 → **弧后补扫** + 报告（**并行灌数据用这个**）
  distill    凝练模板（**跨书全局操作**，会重建全部 draft；**只能单点串行跑**）
  full       all + distill（单点跑，一条命令从原文到模板；**禁止并行**）

🔴 **匿名化必须在凝练之前**（2026-09-12 修正）：此前 `all` **不含** anonymize，
   导致"概括/段/弧齐全但未匿名"的书接一次 distill 就凝练出**带源书专名**的模板
   （打破「反抄袭门槛：源书专名命中 0」）。现 `all` 已内置，顺序为
   概括 → 匿名化 → 段 → 分类 → 弧（下游都在干净文本上生成）。

🔴 **弧后必须补扫**（2026-09-14 新增）：上面那条只解决了"匿名化在段弧之前"，
   但**弧是最后一个 LLM 阶段**——它用 DeepSeek 重新产出 `arc_summary`，该文本
   **从未经过匿名化**。实测（北派 300/300、回明 294/294 行 arc_summary 无 raw 备份）：
   LLM 在**匿名输入**上会「认出」原著（两本都是知名网文）并把真名写回
   （「孙家兄弟」「花酒行者」），而 `book_aliases` 里其实**已注册**这些词 ——
   是清洗时机漏了最后一棒，不是抽取遗漏。故 `all` 现为：
   概括 → 匿名化 → 段 → 分类 → 弧 → **补扫（用现有映射，零 LLM）** → verify。

防限流：--interval 默认 2.0 秒（**禁止调小**），429/5xx 自动指数退避。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台中文输出
except Exception:  # noqa: BLE001
    pass        # 被重定向/被测试框架接管时可能不支持，不影响功能

import app.core.database as dbmod                          # noqa: E402
from app.services import plot_import as pi                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="小说 → 概括 → 匿名化 → 情节段 → 分类 → 故事弧 → 报告 / 模板凝练")
    ap.add_argument("--book-dir", default=None, help="小说章节目录（形如 0001_标题.txt）")
    ap.add_argument("--book-name", default=None,
                    help="入库用书名（断点续跑的幂等键）；distill 阶段省略 = 对全部书凝练")
    ap.add_argument("--book-names", default=None,
                    help="**按题材分池凝练**：逗号分隔的多本书（如 \"蛊真人,凡人修仙传,斗破苍穹\"）。"
                         "仅 distill 用；给了它就按池聚类 + 只清本池源头的 draft（池外 draft 保留）")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--stage",
                    choices=["summarize", "segment", "merge-segments", "label", "arc",
                             "anonymize", "reanonymize", "distill", "verify", "all", "full"],
                    default="all",
                    help="summarize 逐章概括 / anonymize 专名匿名化 / segment 情节段 / "
                         "merge-segments 相邻相似段合并（08-B7，修「段切太碎」；⚠️ 会清该书弧标记，"
                         "需再跑 --stage arc）/ label 段分类 / arc 故事弧 / "
                         "verify 专名残留抽检+覆盖率（纯 SQL，零调用）/ "
                         "distill 凝练模板（跨书全局，单点跑）/ "
                         "all 概括+匿名化+段+分类+弧+verify+报告（并行灌数据用）/ full = all + distill（单点）")
    ap.add_argument("--batch", type=int, default=10, help="段切分每批章数")
    ap.add_argument("--arc-provider", default="deepseek", choices=["deepseek", "modelscope"],
                    help="弧归并用哪家模型：deepseek（默认，付费稳）/"
                         "modelscope（魔搭 Qwen3.8-Flash-Next，吃免费额度 200 次/天；"
                         "每本仅 1~5 批调用，最划算；key 存 app_configs.llm.modelscope_key）")
    ap.add_argument("--threshold", type=float, default=0.80,
                    help="弧聚类相似度阈值（distill 用，越大越保守）")
    ap.add_argument("--min-arcs", type=int, default=1,
                    help="成组最少弧数（distill 用；设 2 = 只要跨书/跨弧的套路）")
    ap.add_argument("--batch-summarize", type=int, default=1,
                    help="每 N 章一次概括调用（默认 1=逐章；建议 3 —— 请求数降 1/N，跨章更连贯）")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发批次数（默认 1=串行；建议 2~3 —— 瓶颈是等生成，并发才能提速）")
    ap.add_argument("--force", action="store_true",
                    help="段切分强制清空重算（默认 False = 断点续跑，跳过已完成批次）；"
                         "也作用于 arc 幂等跳过 / distill 指纹跳过（强制重算）")
    ap.add_argument("--merge-threshold", type=float, default=0.86,
                    help="相邻段合并的相似度阈值（merge-segments 用，0.86 高门槛：只合并确实被切碎的）")
    ap.add_argument("--dry-run", action="store_true",
                    help="merge-segments 只报告将合并哪些段，不写库")
    ap.add_argument("--interval", type=float, default=2.0, help="请求发起最小间隔秒（防限流，禁止调小）")
    ap.add_argument("--out", default=None, help="报告输出路径（默认 outputs/<书名>-导入报告.md）")
    args = ap.parse_args()

    # 校验：只有真正要**读源 txt** 的阶段才需要 --book-dir。
    # - distill（跨书凝练）/ verify（纯读库）/ reanonymize（用现有映射补扫）本来就不需要；
    # - arc / label 也只需要库里的段数据，不需要源目录（2026-09-15 放宽：此前强制
    #   --book-dir 导致「只想重算弧」必须先指个路径，属于无意义的摩擦）。
    _NEED_DIR = ("summarize", "segment", "all", "full")
    if args.stage in _NEED_DIR and not (args.book_dir and args.book_name):
        ap.error(f"--book-dir 与 --book-name 在 {args.stage} 阶段必填（该阶段要读源 txt）")
    if args.stage not in ("distill",) and not args.book_name:
        ap.error(f"--stage {args.stage} 必填 --book-name")

    dbmod.init_db()
    db = dbmod.SessionLocal()
    rate = pi.RateLimiter(args.interval)
    t0 = time.time()

    def _run_anonymize() -> None:
        """专名匿名化（幂等：重复跑会重建映射并再次替换，已替换处为 no-op）。"""
        from app.services import anonymizer
        st = anonymizer.anonymize_book(db, args.book_name)
        print(f"[anonymize] {st['aliases']} 条映射，触及 {st['touched']}/{st['chapters']} 章")
        print(f"   主角 -> {st['protagonist']} | 境界阶梯 -> {st['realms']}")

    def _run_reanonymize() -> None:
        """弧后补扫（2026-09-14）：**用现有映射**再替换一遍全部字段，不重抽（零 LLM 成本）。

        为什么需要：`all` 的顺序是 …→ anonymize → segment → label → **arc**，
        弧是最后一个 LLM 阶段 —— 它用 DeepSeek 重新产出 `arc_summary`，该文本
        **从未经过匿名化**。实测 LLM 会在匿名输入上「认出」原著并写回真名
        （北派「孙家兄弟」/ 蛊真人「花酒行者」，两词在 book_aliases 里其实已注册）。
        `apply_replacement` 幂等，对已清洁字段为 no-op，全程零 LLM 调用。
        """
        from app.services import anonymizer
        mapping = anonymizer.get_aliases(db, args.book_name)
        if not mapping:
            print("[reanonymize] 无映射表（先跑 --stage anonymize）")
            return
        rep = anonymizer.apply_replacement(db, args.book_name, mapping)
        print(f"[reanonymize] 用现有 {len(mapping)} 条映射补扫："
              f"触及 {rep['touched']}/{rep['chapters']} 章（零 LLM）")

    def _distill_books() -> list[str] | None:
        """distill 的书目范围：--book-names（分池，逗号分隔）> --book-name（单本）> None（全库）。"""
        if args.book_names:
            return [b.strip() for b in args.book_names.split(",") if b.strip()]
        return [args.book_name] if args.book_name else None

    def _run_distill() -> None:
        """凝练模板（**跨书全局**：默认 book_names=None；会重建全部 draft，reviewed 不动）。"""
        from app.services import plot_distill
        from app.models.orm import ChapterSummaryORM
        from app.services import anonymizer
        books = _distill_books()
        # 凝练前补扫（2026-09-14 加）：collect_arcs 把 arc_summary/segment_summary **原样**
        # 喂给凝练 LLM（只有 cast desc 过了匿名化）——若弧文本有复燃真名，会被直接写进模板。
        # 用现有映射每本书再洗一遍（零 LLM 成本，幂等）。
        for b in (books if books
                  else [x for (x,) in db.query(ChapterSummaryORM.book_name).distinct()]):
            m = anonymizer.get_aliases(db, b)
            if m:
                anonymizer.apply_replacement(db, b, m)
        st = plot_distill.distill_all(
            db, book_names=books,
            threshold=args.threshold, min_arcs=args.min_arcs, force=args.force)
        if st.get("skipped"):
            print(f"[distill] ⏭ 跳过：{st['reason']}（--force 可强制重跑）")
        else:
            print(f"[distill] {st['groups']} 组 → 入库 {st['created']} 个模板（draft 待人工审核）")
            for t in st["templates"]:
                print(f"   · {t['name']}（{t['arcs']} 弧 / {len(t['books'])} 书 / 相似度 {t['avg_sim']}）")
            for f in st["failed"]:
                print(f"   ✗ {f['group']}: {f['reason']}")
        print(f"[report] {plot_distill.export_template_report(db)}")

    def _run_verify() -> int:
        """专名残留抽检 + 覆盖率体检（**纯 SQL，零 LLM 调用**）。返回**硬判据**命中数。

        为什么需要它：匿名化是"概括之后对文本做替换"，顺序错/被跳过（历史上 `all` 就漏了
        anonymize）会产出带源书专名的概括 → 接一次 distill 就把源书专名焊进模板库，
        而那时钱已经花了。这个检查让「反抄袭门槛：源书专名命中 0」变成**可回归的**。

        ⚠️ **判据分硬/软（2026-09-12 实测修正）**：
        - **硬（判失败）**：`protagonist/role/sect/family/force` —— 这些是**真专名**
          （人名/宗门/家族/势力），会进模板 cast，照抄等于抄袭。
        - **软（只报告）**：`item/place` —— 抽取噪声大：实测把「丹药」「长剑」「玉佩」这类
          **普通名词**也当专名注册了，而下游 LLM 在概括里自然会写出「获得五颗丹药」这种句子，
          并非照抄源书（道诡异仙实测 21 处全属此类）。**它们不进模板 cast，故不作为失败判据。**
        """
        from app.models.orm import BookAliasORM, ChapterSummaryORM
        from app.services.plot_distill import CAST_KINDS
        book = args.book_name
        alias_rows = db.query(BookAliasORM).filter_by(book_name=book).all()
        kind_of = {r.original: r.kind for r in alias_rows if r.original}
        names = sorted({n for n in kind_of if len(n) > 1}, key=len, reverse=True)
        rows = db.query(ChapterSummaryORM).filter_by(book_name=book).all()
        fields = ("summary", "segment_summary", "arc_summary")
        hard: list[tuple[str, int, str]] = []
        soft: list[tuple[str, int, str]] = []
        for n in names:
            bucket = hard if kind_of.get(n) in CAST_KINDS else soft
            for r in rows:
                for f in fields:
                    if n in (getattr(r, f) or ""):
                        bucket.append((n, r.chapter_no, f))
                        break
        seg = sum(1 for r in rows if r.segment_no is not None)
        arc = sum(1 for r in rows if r.arc_no is not None)
        with_desc = sum(1 for r in alias_rows if r.role_desc)
        print(f"[verify] 《{book}》章 {len(rows)} / 有段 {seg} / 有弧 {arc} / "
              f"槽位 {len(alias_rows)}（有说明 {with_desc}）")
        if seg < len(rows) or arc < len(rows):
            print(f"   ⚠️ 覆盖不全：段缺 {len(rows) - seg} 章、弧缺 {len(rows) - arc} 章"
                  f"（重跑 --stage segment / arc 可补）")
        if hard:
            print(f"   ✗ 人物/势力类专名残留 {len(hard)} 处（**硬判据未通过**，禁止拿去凝练）"
                  f"—— 前 10 条：")
            for n, no, f in hard[:10]:
                print(f"      · 「{n}」仍出现在第 {no} 章的 {f}")
            print("      处理：优先 `--stage reanonymize`（用现有映射补扫，零 LLM 成本）；"
                  "仍残留再跑 `--stage anonymize`（重抽映射）后复验")
        else:
            print("   ✓ 人物/势力类专名残留 0（反抄袭硬判据通过）")
        if soft:
            kinds = sorted({kind_of.get(n, "?") for n, _, _ in soft})
            print(f"   ⚠️ 其他类残留 {len(soft)} 处（{'/'.join(kinds)}）—— **不作为失败判据**："
                  f"这类抽取把普通名词也当成了专名，下游自然会写出同样的词。前 5 条：")
            for n, no, f in soft[:5]:
                print(f"      · 「{n}」（{kind_of.get(n, '?')}）出现在第 {no} 章的 {f}")
        return len(hard)

    if args.stage == "verify":
        n = _run_verify()
        print(f"[done] {time.time() - t0:.0f}s")
        db.close()
        return 1 if n else 0

    if args.stage == "anonymize":
        _run_anonymize()
        print(f"[done] {time.time() - t0:.0f}s")
        db.close()
        return 0

    if args.stage == "reanonymize":
        _run_reanonymize()
        print(f"[done] {time.time() - t0:.0f}s")
        db.close()
        return 0

    if args.stage == "distill":
        _run_distill()
        print(f"[done] {time.time() - t0:.0f}s")
        db.close()
        return 0

    # all = 概括 → 匿名化 → 段 → 分类 → 弧 → 报告；full = all + 凝练
    run_all = args.stage in ("all", "full")

    def _prog4(label):
        return lambda no, done, skipped, failed: print(
            f"   {label} 第{no}章 done={done} skip={skipped} fail={failed}", flush=True)

    def _prog2(label):
        return lambda done, total: print(f"   {label} {done}/{total} 章", flush=True)

    if args.stage == "summarize" or run_all:
        if args.batch_summarize > 1 or args.concurrency > 1:
            # 批量 / 并发路径（LLM 在工作线程，DB 写在主线程）
            st = pi.import_chapters_batch(
                db, args.book_dir, args.book_name,
                batch_size=args.batch_summarize, concurrency=args.concurrency,
                start=args.start, end=args.end, rate=rate, progress=_prog4("概括"))
        else:
            # 默认路径：逐章串行（与旧版本行为完全一致）
            st = pi.import_chapters(db, args.book_dir, args.book_name,
                                    start=args.start, end=args.end, rate=rate,
                                    progress=_prog4("概括"))
        print(f"[summarize] {st}")
    if run_all:
        # 必须排在段/弧之前（见模块 docstring 的红线说明）
        _run_anonymize()
    if args.stage == "segment" or run_all:
        st = pi.segment_chapters(db, args.book_name, batch=args.batch,
                                 concurrency=args.concurrency, force=args.force,
                                 rate=rate, progress=_prog2("段切分"))
        print(f"[segment] {st}")
    if args.stage == "merge-segments":
        # 相邻相似段合并（08-B7）：只合并「至少一侧是单章碎片且 embedding 相似 ≥ 阈值」的相邻段。
        # ⚠️ 会清该书弧标记（段变=弧失效）→ 之后需再跑 --stage arc（真实调 LLM）。
        # 刻意**不并入 all**：对已灌完的书跑 all 会意外清弧 + 重烧弧归并，属惊喜成本，故独立成站。
        st = pi.merge_similar_segments(db, args.book_name,
                                       threshold=args.merge_threshold,
                                       dry_run=args.dry_run)
        if st.get("skipped"):
            print(f"[merge-segments] ⏭ 跳过：{st['reason']}")
        elif st.get("dry_run"):
            print(f"[merge-segments] 【预览】将合并 {st['merged_groups']} 组："
                  f"段 {st['segments_before']} → {st['segments_after']}（--dry-run 未写库）")
            for m in st["merges"]:
                print(f"   · 段 {m['members']}（各 {m['chapter_sizes']} 章）")
        elif st.get("merged_groups"):
            print(f"[merge-segments] 合并 {st['merged_groups']} 组："
                  f"段 {st['segments_before']} → {st['segments_after']}")
            for m in st["merges"]:
                print(f"   · 段 {m['members']}（各 {m['chapter_sizes']} 章）")
            print("   ⚠️ 弧标记已清 → 请重跑 --stage arc 重建故事弧")
        else:
            print(f"[merge-segments] 无可合并的相邻相似段（段 {st['segments_after']} 保持不变）")
        db.close()
        print(f"[done] {time.time() - t0:.0f}s")
        return 0
    if args.stage == "label" or run_all:
        st = pi.label_segments(db, args.book_name, rate=rate)
        print(f"[label] {st}")
    if args.stage == "arc" or run_all:
        # 故事弧归并：默认 DeepSeek V4.1 Flash（升档模型），可用 --arc-provider modelscope 切换
        # 幂等（08-B8③）：全部段已有弧 → 跳过（不调 LLM）；增量（部分段无弧）→ 全量重算；--force 强制
        st = pi.merge_arcs(db, args.book_name, rate=rate, provider=args.arc_provider,
                           force=args.force)
        if st.get("skipped"):
            print(f"[arc] ⏭ 跳过：{st['reason']}（--force 可强制重算）")
        else:
            print(f"[arc] provider={args.arc_provider} {st}")
    if run_all:
        # 弧后补扫（2026-09-14）：弧是最后一个 LLM 阶段，其产出 arc_summary 必须再洗一遍
        # —— LLM 会在匿名输入上「认出」原著并写回真名（零 LLM 成本，幂等）
        _run_reanonymize()

    bad = 0
    if run_all:
        # 收尾自检：顺序错了（漏 anonymize）在这里就会暴露，而不是等凝练完才发现
        bad = _run_verify()

    path = pi.export_report(db, args.book_name, args.out)
    print(f"[report] {path}")

    if args.stage == "full":
        if bad:
            print("✗ 专名残留未清零 → **拒绝凝练**（否则源书专名会被焊进模板库）。"
                  "请先重跑 --stage anonymize，再跑 --stage distill。")
            db.close()
            return 1
        print("⚠️ 接下来凝练模板：这是**跨书全局**操作，会重建全部 draft 模板（reviewed 不动）"
              "—— 确认没有其他 AI 在并行灌数据。")
        _run_distill()

    print(f"[done] {time.time() - t0:.0f}s")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
