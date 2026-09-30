# -*- coding: utf-8 -*-
"""硅基流动恢复哨兵（2026-09-19 逆天邪神概括超时事故）。
每 5 分钟探测一次服务端延迟；连续 2 次 <20s 视为恢复 → 自动续跑逆天邪神概括（幂等，已概括章自动 skip）。
哨兵本身零成本（探测请求 max_tokens=8）。"""
import json
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

KEY = None  # 启动时从 DB 读
LOG = Path("E:/AI小说创作/outputs/_nitian_sentinel.log")


def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_key() -> str:
    db = sqlite3.connect("file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro", uri=True)
    v = db.execute("select value from app_configs where key='retrieval.siliconflow_key'").fetchone()[0]
    db.close()
    return v.strip().strip('"')


def probe(timeout: int = 90) -> float | None:
    """最小请求测延迟，秒；失败返回 None。"""
    body = json.dumps({"model": "Qwen/Qwen3-8B",
                       "messages": [{"role": "user", "content": "回复：ok"}],
                       "max_tokens": 8}).encode()
    req = urllib.request.Request("https://api.siliconflow.cn/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    t0 = time.time()
    try:
        urllib.request.urlopen(req, timeout=timeout)
        return time.time() - t0
    except Exception as e:
        log(f"  探测失败 {type(e).__name__}: {str(e)[:80]}")
        return None


def done_count() -> int:
    db = sqlite3.connect("file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro", uri=True)
    n = db.execute("select count(*) from chapter_summaries where book_name='逆天邪神'").fetchone()[0]
    db.close()
    return n


def main() -> int:
    global KEY
    KEY = get_key()
    log("哨兵启动：等待硅基流动恢复（连续 2 次 <20s）→ 自动续跑逆天邪神 1~2000")
    ok_streak = 0
    while True:
        lat = probe()
        if lat is not None and lat < 20:
            ok_streak += 1
            log(f"  探测 {lat:.1f}s ✓（连续 {ok_streak}/2）")
            if ok_streak >= 2:
                log(f"服务恢复 → 启动续跑（当前库内 {done_count()} 章）")
                import os
                with Path("E:/AI小说创作/outputs/_nitian_resume.log").open("w") as rf:
                    subprocess.run(
                        ["E:/AI小说创作/.venv/Scripts/python.exe", "-u",
                         "scripts/import_novel.py",
                         "--book-dir", "E:/AI小说创作/小说/逆天邪神",
                         "--book-name", "逆天邪神",
                         "--stage", "summarize", "--start", "1", "--end", "2000",
                         "--batch-summarize", "3", "--concurrency", "3"],
                        cwd="E:/AI小说创作/backend", stdout=rf, stderr=subprocess.STDOUT,
                        env={**os.environ, "DEV_RELOAD": "0", "PYTHONIOENCODING": "utf-8"})
                log(f"续跑进程结束，库内 {done_count()} 章 → 哨兵退出")
                return 0
        else:
            if lat is not None:
                log(f"  探测 {lat:.1f}s 仍慢")
            ok_streak = 0
        time.sleep(300)


if __name__ == "__main__":
    raise SystemExit(main())
