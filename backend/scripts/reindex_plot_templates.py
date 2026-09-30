"""一次性全量重建全部情节模板的向量索引（2026-09-15 角色原型库上线回填）。

背景：char_archetype 第三路索引上线时，存量模板从未索引过原型块；
且 cast 索引功能上线前入库的老模板连 plot_cast 块也是缺的
（实测：793 个 cast 槽位只有 189 个向量块）。本脚本对全部模板逐个跑
`index_template`（三路全清全建，幂等，失败自动重试）。

以后正常增量**不依赖本脚本**：create/update 会自动 index_template。
只在「改了切块文本形态 / 换 embedding 模型」时重跑。

用法（在 backend/ 下执行）：

    ..\\.venv\\Scripts\\python.exe scripts/reindex_plot_templates.py --dry-run
    ..\\.venv\\Scripts\\python.exe scripts/reindex_plot_templates.py
"""
import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台中文输出
except Exception:  # noqa: BLE001
    pass

import app.core.database as dbmod                           # noqa: E402
from app.models.orm import PlotTemplateORM, VectorChunkORM  # noqa: E402
from app.services import plot_template_crud as tpl          # noqa: E402

TPL_TYPES = (tpl.SOURCE_TYPE, tpl.SOURCE_TYPE_CAST, tpl.SOURCE_TYPE_ARCHETYPE)


def _counts(db) -> dict:
    rows = (db.query(VectorChunkORM.source_type,
                     VectorChunkORM.source_id.label("sid"))
            .filter(VectorChunkORM.source_type.in_(TPL_TYPES)).all())
    valid_ids = {r[0] for r in db.query(PlotTemplateORM.id).all()}
    out = {t: 0 for t in TPL_TYPES}
    orphans = 0
    for st, sid in rows:
        if sid not in valid_ids:
            orphans += 1
        else:
            out[st] = out.get(st, 0) + 1
    return out, orphans


def main() -> int:
    ap = argparse.ArgumentParser(description="全量重建情节模板向量（三路）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计预期块数，不写库不调 embedding API")
    ap.add_argument("--out", default=None,
                    help="结果同时写入该文件（管道/控制台捕获中文不可靠时的保底）")
    ap.add_argument("--status", default=None,
                    help="只处理该状态的模板（如 active；默认全部，含已归档）")
    ap.add_argument("--limit", type=int, default=None,
                    help="只处理前 N 个模板（先小批量验证 embedding 可用再放量）")
    args = ap.parse_args()

    if args.out:
        class _Tee:
            def __init__(self, *streams):
                self.streams = streams

            def write(self, s):
                for f in self.streams:
                    f.write(s)

            def flush(self):
                for f in self.streams:
                    f.flush()

        _fh = open(args.out, "w", encoding="utf-8")
        sys.stdout = _Tee(sys.stdout, _fh)   # noqa: F841 - main 结束随进程关闭

    dbmod.init_db()
    # 🔴 本机环境硬需求（2026-09-15 四次实验定案）：池化多连接在本机文件虚拟化
    # 下**每个连接是独立视图** —— 写入对其他连接/进程不可见，commit/rollback
    # 归还连接后换新连接会让写入"蒸发"。StaticPool 强制全局唯一连接：
    # 写读自洽 + 结尾在同一条连接上 checkpoint 才能把数据真正刷进主库文件
    # （probe 实测：同连接 checkpoint(PASSIVE) 后跨进程可见）。
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    eng = create_engine(
        dbmod.DEFAULT_DB_URL,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db = sessionmaker(bind=eng)()
    _q = db.query(PlotTemplateORM)
    if args.status:
        _q = _q.filter(PlotTemplateORM.status == args.status)
    rows = _q.order_by(PlotTemplateORM.created_at).all()
    if args.limit:
        rows = rows[:args.limit]
    print(f"模板总数: {len(rows)}" + (f"（--limit 截取）" if args.limit else ""))

    expect_tpl = expect_cast = expect_arch = 0
    for t in rows:
        expect_tpl += len(tpl.template_chunks(t))
        expect_cast += len(tpl.cast_chunks(t))
        expect_arch += len(tpl.archetype_chunks(t))
    print(f"预期块数: 模板={expect_tpl} cast={expect_cast} 原型={expect_arch} "
          f"合计={expect_tpl + expect_cast + expect_arch}")
    if args.dry_run:
        base, orphans = _counts(db)
        print(f"[dry-run] 当前基线: {base} 孤儿={orphans}")
        return 0

    base, orphans0 = _counts(db)
    print(f"回填前基线: {base} 孤儿={orphans0}")

    mismatch: list[tuple[str, str, int, int]] = []
    for i, t in enumerate(rows, 1):
        expect = (len(tpl.template_chunks(t)) + len(tpl.cast_chunks(t))
                  + len(tpl.archetype_chunks(t)))
        ok = False
        for attempt in (1, 2, 3):   # embed 抖动/限流重试；index_template 幂等（先清后建）
            tpl.index_template(db, t)
            db.expire_all()          # index_template 内部 commit，防读到过期对象
            got = db.query(VectorChunkORM).filter_by(source_id=t.id).count()
            if got == expect:
                ok = True
                break
            print(f"  ⚠️ {t.name}: 第{attempt}次建库 got={got} != expect={expect}，重试…")
            time.sleep(2.0 * attempt)
        if not ok:
            mismatch.append((t.id, t.name, expect, got))
        if i % 10 == 0 or i == len(rows):
            print(f"进度 {i}/{len(rows)}（失败 {len(mismatch)}）")

    # 🔴 强制 checkpoint（2026-09-15 实测本机硬需求）：写连接 commit 的 WAL 帧
    # 对**跨进程读者不可见**，只有把 WAL 刷进主库文件后其他进程才读得到。
    # ⚠️ 必须在**写数据的那条唯一连接**上跑（StaticPool 保证）；先 commit 结束
    # 挂起事务（TRUNCATE 需要无活动读者，PASSIVE 尽力而为不 busy）。
    # 实测形态参照 probe：同连接 commit → checkpoint → 跨进程立即可见。
    try:
        db.commit()
        ck = db.execute(text("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
        print(f"checkpoint(PASSIVE): {ck}")
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint 失败（可稍后手动执行 PRAGMA wal_checkpoint(TRUNCATE)）: "
              f"{type(e).__name__}: {e}")
    # 清理诊断期误插的测试行（如有）
    try:
        db.rollback()
        n = db.execute(text(
            "delete from vector_chunks where id='PROBE_20260915_1236'")).rowcount
        db.commit()
        if n:
            print(f"清理 PROBE 测试行: {n}")
    except Exception:  # noqa: BLE001
        db.rollback()

    final, orphans1 = _counts(db)
    print("\n===== 回填结果 =====")
    print(f"回填前: {base}")
    print(f"回填后: {final}")
    print(f"预期  : 模板={expect_tpl} cast={expect_cast} 原型={expect_arch}")
    print(f"孤儿  : {orphans0} → {orphans1}")
    if mismatch:
        print(f"\n❌ {len(mismatch)} 个模板三路块数与预期不符（可重跑本脚本修复，幂等）:")
        for tid, name, e, g in mismatch:
            print(f"  {tid[:8]} {name}: expect={e} got={g}")
        return 1
    print("✅ 全部模板三路块数与预期一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
