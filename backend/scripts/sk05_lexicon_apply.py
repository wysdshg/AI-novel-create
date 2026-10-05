# -*- coding: utf-8 -*-
"""[DEV-SK05] 词表落地：atomic_events 表白名单写入（13 INSERT + 5 UPDATE）。

零 LLM 调用；全部写入内容由本文件顶部的三张常量表驱动，可逐条审阅。

    .venv\\Scripts\\python.exe backend/scripts/sk05_lexicon_apply.py --dry-run
    .venv\\Scripts\\python.exe backend/scripts/sk05_lexicon_apply.py --apply
    .venv\\Scripts\\python.exe backend/scripts/sk05_lexicon_apply.py --verify
    .venv\\Scripts\\python.exe backend/scripts/sk05_lexicon_apply.py --restore-check
    可选 --db <路径>：默认生产库；先在副本上试跑再打生产库。

白名单（唯一允许的库写范围，任务单 [DEV-SK05] 第一节）：
  INSERT 13 条新类型：B09 G08 C16 C17 C18 C19 C20 H09 D09 A10 A11 F07 H10
  UPDATE  3 条 candidate→active：B07 B08 C14（D08 维持 candidate 不动）
  UPDATE  A04：定义中立化 + 四拍同步改（SK04 §2.2）
  UPDATE  F03：定义放宽 + 起/合两拍同步改（SK04 §2.4）
越界即拒绝：脚本内 ASSERT 只碰上述 ID，且不动其它表。

口径依据：outputs/sk04/词表双轨提案.md（用户 2026-10-05 拍板 B1/B2/B3/B5/B6/B7）。
"""
from __future__ import annotations

import argparse
import io
import json
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD_DB = Path(r"C:\Users\w3013\.ai_novel\data\novel_agent.db")
BACKUP = ROOT / "outputs" / "sk05" / "atomic_events_备份.json"
ROLLBACK = ROOT / "outputs" / "sk05" / "sk05_回滚.sql"
LOGDIR = ROOT / "outputs" / "sk05"
TABLE = "atomic_events"

# 期望终态（verify 用）：64 + 13 = 77 行；B07/B08/C14 转 active 后 candidate 只剩 D08
EXPECT_TOTAL, EXPECT_ACTIVE, EXPECT_CANDIDATE = 77, 76, 1
EXPECT_CANDIDATE_IDS = ["D08"]

# ---------------------------------------------------------------------------
# 一、13 条转正新类型（定义照 SK04 提案 §2.1；C16 域 H→C、A10 含军制军械、
#     C19 去掉英文残词 competing 三处为拍板项）。四拍为本单按库内 64 类体例补齐
#     （提案只给一句话定义，库表四拍列非空且下游判类要读）。
# ---------------------------------------------------------------------------
NEW_TYPES: list[dict] = [
    dict(id="B09", name="经营实业与商路", category_id="B",
         definition="为获利或供给开办产业、打通商路、定价营销并扩大产销规模",
         beat_start="为获利或补供给缺口，定下办厂开店跑商路的打算，手头缺人手本钱门路",
         beat_mid="置办物料、招人定酬、定价推销，产销逐步跑通并打通外销渠道",
         beat_turn="同行挤压、税关卡拿或内部分利不均，规模扩张濒临断链",
         beat_end="产业与商路立住，现金流与货源成为后续行事的底气",
         scope_tags=["寒门"]),
    dict(id="G08", name="工程兴工与勘测", category_id="G",
         definition="为工农业/军事目的勘测地形并组织大规模土木工程（修渠、筑坝、开矿、修路）并推进落成",
         beat_start="因农工或军事所需，先勘地形矿脉水势，定下可施工的选址",
         beat_mid="调集人夫物料，分段开工，工序与器具边干边改",
         beat_turn="塌方涌水、工期延误或物料断供，工程眼看停工",
         beat_end="渠成坝合路通矿出，一处地形被永久改变，产出或防务上台阶",
         scope_tags=["寒门"]),
    dict(id="C16", name="赈灾放粮与济贫", category_id="C",
         definition="灾荒时发放粮饷、施粥济民，并组织流民安置与以工代赈",
         beat_start="灾荒骤至，饥民流民聚拢而来，存粮与秩序同时吃紧",
         beat_mid="设厂放粮、施粥登记，把壮丁编入以工代赈的活计",
         beat_turn="粮源被截、疫病继发或有人趁乱哄抢囤积",
         beat_end="灾民安置下来，流民变作人手，人心与劳力一并归附",
         scope_tags=["寒门"]),
    dict(id="C17", name="民生治安与基层治理", category_id="C",
         definition="基层官吏治理、查办贪墨、维持治安（保甲/户籍/捕役等）",
         beat_start="接手掌治一地，户籍混乱、治安败坏或胥吏上下其手",
         beat_mid="清册编甲、任用捕役、查办经手钱粮的小吏，立规矩办事",
         beat_turn="豪强勾结胥吏反扑，旧案翻出牵连同僚",
         beat_end="地方秩序整肃，政令下得去，治理班底初步成形",
         scope_tags=["寒门"]),
    dict(id="C18", name="变法施政与律法", category_id="C",
         definition="在朝堂议定并颁行新施政方略（变法、税政、律法、土地与垦荒政策）",
         beat_start="旧制弊病积重，须定出一套新政（税制、律法、田政）方能解困",
         beat_mid="拟条陈、定细则，取得上位者背书后颁行试点",
         beat_turn="既得利益者阳奉阴违、地方执行走样，新政推不动",
         beat_end="新法新制落地生效，赋税田亩的秩序被重定",
         scope_tags=["寒门"]),
    dict(id="C19", name="朝堂党争与奏章对质", category_id="C",
         definition="在朝堂/御前就国政方略对质辩论并议定路线",
         beat_start="一桩国政协奏提交御前，朝中两派立场相左",
         beat_mid="轮番上章、当庭对质，各自摆利害、揭对方的短",
         beat_turn="有人抛出旧案或私德问题，辩论升级成派系倾轧",
         beat_end="御前定下路线，得势者奉命推行，失势者记恨埋下后患",
         scope_tags=["寒门"]),
    dict(id="C20", name="教化办学与育才", category_id="C",
         definition="兴办学校/书院、编教材聘教员育才，或推行科举选官与官员考绩",
         beat_start="缺可用之才，须兴学育才或另开选官考绩之门",
         beat_mid="筹校舍、编教材、聘教员，立课规与考绩之法",
         beat_turn="旧学阀抵制、生源不足或经费被克扣",
         beat_end="学成者入仕任职，一条选才渠道握在己手",
         scope_tags=["寒门"]),
    dict(id="H09", name="舆情宣传与民心", category_id="H",
         definition="以报刊传单、演讲戏剧等舆论手段宣传主张、塑造民心或瓦解敌方意志",
         beat_start="要成一件事却缺人心基础，决定先动手上的舆论",
         beat_mid="刊印散布、登台宣讲、借戏造势，说法一路传开",
         beat_turn="对手封锁、伪造反宣或当众揭其虚，风向面临反转",
         beat_end="民心倒向己方，敌方意志松动，主张成为公论",
         scope_tags=["寒门"]),
    dict(id="D09", name="谍报刺探与反谍", category_id="D",
         definition="布置或侦破敌方情报网、内线暗桩与反间监视",
         beat_start="情报不通被动挨打，决意布桩或侦查对方的暗线",
         beat_mid="安插眼线、接递密信、比对假讯，摸清对方耳目所在",
         beat_turn="内线被识破或传出假料，反间计当场见血",
         beat_end="己方暗桩站稳，或敌方情报网被连根拔起，行动重回暗处",
         scope_tags=["寒门"]),
    dict(id="A10", name="军事筹谋与战前部署", category_id="A",
         definition="战前议定作战方略、兵力部署与后勤筹办，含军制变更与军械配发（不含临阵交手本身）",
         beat_start="战事将起，敌我态势与己方编制未定，须先定方略",
         beat_mid="议主攻次、整编部队配发军械、清点粮草，排出部署次序",
         beat_turn="情报有变、粮道告急或将帅争策，部署临期推翻重来",
         beat_end="军令下发、各部就位，只等开仗",
         scope_tags=["寒门"]),
    dict(id="A11", name="会战攻城与战役歼灭", category_id="A",
         definition="以战役为单位的大规模攻防与歼灭（超出 A 域小单元类型）",
         beat_start="大军压至城下或野战正面，两军列阵进入会战",
         beat_mid="攻城野战连日推进，攻防要点反复易手",
         beat_turn="援军将至、粮尽或某一翼崩溃，战役面临翻盘",
         beat_end="城破野灭，敌主力被歼或溃退，一战定下一地归属",
         scope_tags=["寒门"]),
    dict(id="F07", name="工艺试制与技术研发", category_id="F",
         definition="工艺/器械的技术研发与试制成功（区别于 F03 铸本命法宝、F02 炼丹）",
         beat_start="现有器物不敷使用，定下试制新器新法的目标",
         beat_mid="反复配料试作、拆解改良，记录失败参数逐步逼近",
         beat_turn="关键工序卡死、材料不济或被人窃密抢先",
         beat_end="试制成功并定型量产，一项技术优势落到己方手里",
         scope_tags=["寒门"]),
    dict(id="H10", name="仪典祭祀与丧葬", category_id="H",
         definition="祭祀典礼与丧葬安立仪式的举行",
         beat_start="有人亡故或需祭告天地祖先，议定典礼规格",
         beat_mid="备祭器、立灵位、聚众执礼，仪程逐项走完",
         beat_turn="礼制争议、身份阻挠或讣变叠至，典礼几近中断",
         beat_end="丧祭落定，名分与追念当众确认，生者关系随之重排",
         scope_tags=["寒门"]),
]

# ---------------------------------------------------------------------------
# 二、candidate → active（拍板 B7；D08 明确不转正）
# ---------------------------------------------------------------------------
STATUS_UPDATES = ["B07", "B08", "C14"]

# ---------------------------------------------------------------------------
# 三、存量行字段改写（拍板 B5/B6）
#     A04：定义去掉「主角落入算计」的单向性，四拍同步改（SK04 §2.2 新四拍原文）。
#     F03：定义放宽到兵器/器械/工具（含民用器物），起/合两拍同步改（SK04 §2.4）。
# ---------------------------------------------------------------------------
FIELD_UPDATES = {
    "A04": {
        "definition": "一方布局埋伏或掷暗箭伏击目标（设伏方与被伏方视角均可）",
        "beat_start": "一方选定目标与地形，布下埋伏或暗箭",
        "beat_mid": "伏击发动，己方受创或计策得手，第三方势力随之卷入",
        "beat_turn": "识破机关用底牌反打，或伏击方临阵加码",
        "beat_end": "主谋被诛、被擒或全身而退，埋下后续仇怨",
    },
    "F03": {
        "definition": "打造、重铸或改装兵器/器械/工具（含民用器物）",
        "beat_start": "需某件兵器、器械或工具，或受托为他人打造",
        "beat_end": "得到成品器物，用途与战力/生产效率提升",
    },
    # 自纠：首轮把 A11 定义抄成「…小单元类型的尺度」，多出提案原文没有的「的尺度」三字
    # （对账脚本 outputs/sk05/_tools/t04_definition_diff.py 查出）。
    # INSERT 项按设计不覆盖已存在行，故这类"存量行字段修正"显式走 UPDATE 通道。
    "A11": {
        "definition": "以战役为单位的大规模攻防与歼灭（超出 A 域小单元类型）",
    },
}

NEW_IDS = {t["id"] for t in NEW_TYPES}
UPD_IDS = set(STATUS_UPDATES) | set(FIELD_UPDATES)
WHITELIST = NEW_IDS | UPD_IDS

COLUMNS = ["id", "name", "category_id", "definition", "beat_start", "beat_mid",
           "beat_turn", "beat_end", "is_core_capable", "domain", "scope_tags",
           "status", "created_at"]


class _Tee:
    """GBK 控制台会糊中文：同步落一份 UTF-8 日志。"""

    def __init__(self, path: Path) -> None:
        self._f = io.open(path, "w", encoding="utf-8")
        self._con = sys.__stdout__

    def write(self, s: str) -> int:
        self._f.write(s)
        self._con.write(s.encode("gbk", "replace").decode("gbk"))
        return len(s)

    def flush(self) -> None:
        self._f.flush()
        self._con.flush()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_table(con: sqlite3.Connection) -> dict[str, dict]:
    con.row_factory = sqlite3.Row
    return {r["id"]: dict(r) for r in con.execute(f"SELECT * FROM {TABLE}")}


def check_backend_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 8000)) == 0


def who_listens_8000() -> str:
    """守卫拦下时必须说清「是谁在听」——只报端口号会让人和用户各执一词（E16）。"""
    import subprocess

    def sh(cmd):
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True)
        for enc in ("gbk", "utf-8"):
            try:
                return p.stdout.decode(enc)
            except UnicodeDecodeError:
                continue
        return p.stdout.decode("utf-8", "replace")

    pids = sorted({ln.split()[-1] for ln in sh("netstat -ano").splitlines()
                   if ":8000" in ln and "LISTENING" in ln})
    if not pids:
        return "端口在听但 netstat 查不到 LISTEN（可能是 IPv6 或瞬时状态）"
    out = []
    for pid in pids:
        info = sh(["powershell", "-NoProfile", "-Command",
                   f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
                   "Select-Object -ExpandProperty CommandLine",
                   ]).strip().splitlines()
        cmd = info[0] if info and info[0] else "(命令行取不到)"
        out.append(f"PID {pid}: {cmd}")
    return "; ".join(out)


def guard_prod_write(db_arg: str, force: bool) -> bool:
    """守卫只盯生产库：副本试跑不受后端进程影响（照 SK01 铁律，写生产库须先停 8000）。"""
    if force:
        return True
    same = Path(db_arg).resolve() == PROD_DB.resolve()
    if same and check_backend_running():
        print(f"[apply] 拒绝：目标是生产库且 127.0.0.1:8000 有进程在听 → {who_listens_8000()}\n"
              "        写生产库须先停后端（StaticPool 单连接，写后它读不到新行）；"
              "确认该进程确属后端且可停，用 --force 或先结束它")
        return False
    if same:
        print("[apply] 目标为生产库，后端未监听 8000 → 放行")
    else:
        print(f"[apply] 目标非生产库（{db_arg}）→ 跳过端口守卫")
    return True


def plan_rows() -> list[dict]:
    """把两张常量表展开成 (op, id, payload) 计划。"""
    plan = []
    for t in NEW_TYPES:
        row = {
            "id": t["id"], "name": t["name"], "category_id": t["category_id"],
            "definition": t["definition"], "beat_start": t["beat_start"],
            "beat_mid": t["beat_mid"], "beat_turn": t["beat_turn"],
            "beat_end": t["beat_end"], "is_core_capable": 1, "domain": "general",
            "scope_tags": json.dumps(t["scope_tags"], ensure_ascii=False),
            "status": "active",
        }
        plan.append({"op": "INSERT", "id": t["id"], "row": row})
    for i in STATUS_UPDATES:
        plan.append({"op": "UPDATE", "id": i, "row": {"status": "active"}})
    for i, fields in FIELD_UPDATES.items():
        plan.append({"op": "UPDATE", "id": i, "row": dict(fields)})
    return plan


def selfcheck_plan() -> list[str]:
    """落库前的静态自检：ID 格式、域内续号、非空、白名单不重叠。"""
    errs = []
    overlap = NEW_IDS & UPD_IDS
    if overlap:
        # 允许且只允许一种情形：某新类首轮抄写有误、改在 FIELD_UPDATES 里自纠。
        # 两通道必须给同一个 definition，否则新库跑出来和老库修补后不一致。
        for _id in sorted(overlap):
            new_def = next(t["definition"] for t in NEW_TYPES if t["id"] == _id)
            upd_def = FIELD_UPDATES[_id].get("definition")
            if upd_def is not None and upd_def != new_def:
                errs.append(f"{_id}: NEW_TYPES 与 FIELD_UPDATES 给了两个不同的 definition，"
                            f"全新库与修补库会长成两种值")
            else:
                print(f"[selfcheck] INFO {_id} 走「新建 + 自纠 UPDATE」双通道，两处定义一致")
    if len(NEW_IDS) != len(NEW_TYPES):
        errs.append("新类型 ID 有重复")
    seen: dict[str, list[int]] = {}
    for t in NEW_TYPES:
        _id = t["id"]
        if not (len(_id) >= 2 and _id[0] in "ABCDEFGH" and _id[1:].isdigit()):
            errs.append(f"{_id}: ID 形状不合规则")
            continue
        if _id[0] != t["category_id"]:
            errs.append(f"{_id}: category_id={t['category_id']} 与域首字母不一致")
        if _id in seen.setdefault(_id[0], []):
            errs.append(f"{_id}: 新类型内部重复")
        seen[_id[0]].append(_id)
        for col in ("name", "definition", "beat_start", "beat_mid", "beat_turn", "beat_end"):
            if not (t[col] or "").strip():
                errs.append(f"{_id}: {col} 为空")
        if "competing" in t["definition"]:
            errs.append(f"{_id}: 定义仍含英文残词 competing")
    return errs


def assert_domain_seq(current: dict[str, dict]) -> list[str]:
    """新 ID 必须是「域内现有最大号 +1」起的连续号（任务单 §一.1）。

    基线最大号要排除本单自己的新 ID，否则二次 apply（幂等复跑）会因为
    「域内最大号已被自己抬高」而误判续号不合规。
    """
    errs = []
    max_now: dict[str, int] = {}
    for _id in current:
        if _id in NEW_IDS:
            continue
        if _id[0] in "ABCDEFGH" and _id[1:].isdigit():
            d = _id[0]
            max_now[d] = max(max_now.get(d, 0), int(_id[1:]))
    want: dict[str, list[int]] = {}
    for t in NEW_TYPES:
        want.setdefault(t["id"][0], []).append(int(t["id"][1:]))
    for d, nums in want.items():
        base = max_now.get(d, 0)
        exp = sorted(nums)
        got = list(range(base + 1, base + 1 + len(exp)))
        if exp != got:
            errs.append(f"域 {d}: 现有最大 {base}，期望续号 {got}，实际 {exp}")
    return errs


def backup_table(con: sqlite3.Connection, path: Path) -> None:
    """备份语义：`path` 必须是**首次改动前**的状态，因此已存在就不覆盖（SK01 口径）。
    本单后续若有增量改写，另存一份带时间戳的写前快照供审计。"""
    rows = [dict(r) for r in con.execute(f"SELECT * FROM {TABLE}")]
    payload = {
        "table": TABLE,
        "columns": COLUMNS,
        "backup_ts": now_utc(),
        "source_db": str(con.execute("PRAGMA database_list").fetchone()[2]),
        "row_count": len(rows),
        "rows": rows,
    }
    if path.exists():
        print(f"[backup] 原始备份已存在，保留不覆盖: {path.name}（{len(rows)} 行是当前态，"
              f"不能替换掉改前基线）")
        snap = path.with_name(path.stem + "_写前快照.json")
        snap.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[backup] 本次写前快照 → {snap.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[backup] 全表 {len(rows)} 行 × {len(COLUMNS)} 列 → {path}")


def dry_run(con: sqlite3.Connection) -> int:
    print(f"== dry-run == db={con.execute('PRAGMA database_list').fetchone()[2]}")
    errs = selfcheck_plan()
    current = read_table(con)
    errs += assert_domain_seq(current)
    print(f"静态自检: {'PASS' if not errs else 'FAIL'}")
    for e in errs:
        print("   !", e)
    if errs:
        return 1
    print(f"现状: 总 {len(current)} 行 / active {sum(1 for v in current.values() if v['status']=='active')} "
          f"/ candidate {sum(1 for v in current.values() if v['status']=='candidate')}")
    will_insert, will_update, already = [], [], []
    for p in plan_rows():
        cur = current.get(p["id"])
        if p["op"] == "INSERT":
            if cur is None:
                will_insert.append(p)
            elif all(str(cur.get(k)) == str(v) for k, v in p["row"].items() if k != "created_at"):
                already.append(p)
            else:
                will_update.append({"op": "INSERT-overwrite", "id": p["id"], "row": p["row"], "cur": cur})
        else:
            diff = {k: (cur.get(k), v) for k, v in p["row"].items()
                    if cur is not None and str(cur.get(k)) != str(v)}
            if not diff:
                already.append(p)
            else:
                will_update.append({"op": "UPDATE", "id": p["id"], "row": p["row"], "diff": diff})
    print(f"\n将 INSERT {len(will_insert)} 条:")
    for p in will_insert:
        r = p["row"]
        print(f"   {r['id']} {r['name']} [{r['category_id']}] status={r['status']} "
              f"scope_tags={r['scope_tags']}")
        print(f"      定义: {r['definition']}")
        print(f"      四拍: {r['beat_start']} ｜ {r['beat_mid']} ｜ {r['beat_turn']} ｜ {r['beat_end']}")
    print(f"\n将 UPDATE {len(will_update)} 条:")
    for p in will_update:
        print(f"   {p['id']} ({p['op']})")
        for k, v in (p.get("diff") or p["row"]).items():
            if isinstance(v, tuple):
                print(f"      {k}: {v[0]!r} → {v[1]!r}")
            else:
                print(f"      {k}: {v!r}")
    uniq = sorted({p["id"] for p in already})
    print(f"\n已达标将跳过 {len(uniq)} 个 ID（计划项 {len(already)} 条，"
          f"A11 这类「新建+自纠」双通道会重复计）: {uniq}")
    print(f"\n终态预期: 总 {EXPECT_TOTAL} / active {EXPECT_ACTIVE} / candidate {EXPECT_CANDIDATE} {EXPECT_CANDIDATE_IDS}")
    print(f"越界自检: 计划涉及 ID {sorted(set([p['id'] for p in will_insert+will_update+already]))}")
    out_of_range = set(current) - WHITELIST - set(NEW_IDS)
    touched = {p["id"] for p in will_insert + will_update} - NEW_IDS
    bad = touched - WHITELIST
    print(f"白名单外被触碰: {sorted(bad) if bad else '无'}")
    return 0


def diff_of(op: str, cur: dict | None, row: dict) -> dict:
    """该计划项相对现状的实际差异（空 = 已达标，无需写）。"""
    if op == "INSERT":
        if cur is None:
            return dict(row)
        return {k: v for k, v in row.items()
                if k != "created_at" and str(cur.get(k)) != str(v)}
    if cur is None:
        return {"__missing_row__": True}
    return {k: v for k, v in row.items() if str(cur.get(k)) != str(v)}


def apply_db(con: sqlite3.Connection, force: bool = False) -> int:
    errs = selfcheck_plan()
    current = read_table(con)
    errs += assert_domain_seq(current)
    if errs:
        for e in errs:
            print("   !", e)
        return 1
    if not guard_prod_write(str(con.execute("PRAGMA database_list").fetchone()[2]), force):
        return 1

    before = dict(count=len(current),
                  active=sum(1 for v in current.values() if v["status"] == "active"),
                  candidate=sum(1 for v in current.values() if v["status"] == "candidate"))
    print(f"[apply] 写前现状 {before} db={con.execute('PRAGMA database_list').fetchone()[2]}")

    pending = [(p, diff_of(p["op"], current.get(p["id"]), p["row"])) for p in plan_rows()]
    todo = [(p, d) for p, d in pending if d]
    if not todo:
        print("[apply] 目标状态已全部达成 → 幂等跳过（不写库、不覆盖备份）")
        return 0
    # INSERT 目标已存在 = 该行此前已建。差异字段若全部由同 ID 的 UPDATE 通道负责
    # （抄写自纠），则合法；否则说明库被外部改过，拒绝静默覆盖。
    for p, d in todo:
        if p["op"] != "INSERT" or current.get(p["id"]) is None:
            continue
        cover = set(FIELD_UPDATES.get(p["id"], {}))
        if not set(d) <= cover:
            print(f"[apply] 拒绝：{p['id']} 已在库且差异 {sorted(set(d) - cover)} "
                  f"不在任何改写通道内，请先人工对账")
            return 1
        print(f"[apply] {p['id']} 已存在，差异 {sorted(d)} 由 UPDATE 通道自纠")
    print(f"[apply] 待写 {len(todo)} 项：{[p['id'] for p, _ in todo]}")
    backup_table(con, BACKUP)

    n_ins = n_upd = 0
    ts = datetime.now(timezone.utc).isoformat(sep=" ").split("+")[0]
    try:
        with con:
            for p, diff in todo:
                exists = current.get(p["id"]) is not None
                if p["op"] == "INSERT" and not exists:
                    assert p["id"] in NEW_IDS, f"白名单违规: {p['id']}"
                    cols = list(p["row"].keys()) + ["created_at"]
                    vals = list(p["row"].values()) + [ts]
                    con.execute(
                        f"INSERT INTO {TABLE} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        vals)
                    n_ins += 1
                    print(f"   INSERT {p['id']} {p['row']['name']}")
                    continue
                # 纯 UPDATE，或 INSERT 项的自纠差异（行已存在，只补差异列）
                assert p["id"] in UPD_IDS, f"白名单违规: {p['id']}"
                fields = {k: v for k, v in p["row"].items()
                          if k in diff and k != "created_at"}
                if not fields:
                    print(f"   SKIP {p['id']}（无可写字段）")
                    continue
                sets = ", ".join(f"{k}=?" for k in fields)
                con.execute(f"UPDATE {TABLE} SET {sets} WHERE id=?",
                            list(fields.values()) + [p["id"]])
                n_upd += 1
                print(f"   {'UPDATE(自纠)' if p['op'] == 'INSERT' else 'UPDATE'} "
                      f"{p['id']} 字段 {list(fields)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[apply] 事务回滚: {exc}")
        return 1

    after = read_table(con)
    print(f"[apply] INSERT {n_ins} / UPDATE {n_upd} / 已达标跳过 {len(pending) - len(todo)}")
    print(f"[apply] 写后: 总 {len(after)} "
          f"active {sum(1 for v in after.values() if v['status']=='active')} "
          f"candidate {sum(1 for v in after.values() if v['status']=='candidate')}")
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return 0


def verify(con: sqlite3.Connection) -> int:
    rows = read_table(con)
    res = []
    def chk(name, ok, detail=""):
        res.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

    chk("总行数 = 77", len(rows) == EXPECT_TOTAL, f"实测 {len(rows)}")
    act = sorted(k for k, v in rows.items() if v["status"] == "active")
    cand = sorted(k for k, v in rows.items() if v["status"] == "candidate")
    chk(f"active = {EXPECT_ACTIVE}", len(act) == EXPECT_ACTIVE, f"实测 {len(act)}")
    chk(f"candidate = {EXPECT_CANDIDATE} 且仅 D08", cand == EXPECT_CANDIDATE_IDS, f"实测 {cand}")
    chk("13 新类全部在库且 active", all(i in act for i in sorted(NEW_IDS)),
        f"缺 {sorted(NEW_IDS - set(act))}")
    missing = []
    for t in NEW_TYPES:
        r = rows.get(t["id"])
        if not r:
            missing.append(t["id"])
            continue
        for col in ("name", "definition", "beat_start", "beat_mid", "beat_turn", "beat_end"):
            if str(r.get(col) or "") != t[col]:
                missing.append(f"{t['id']}.{col}")
        try:
            st = json.loads(r.get("scope_tags") or "null")
        except json.JSONDecodeError:
            st = None
        if st != t["scope_tags"]:
            missing.append(f"{t['id']}.scope_tags={r.get('scope_tags')!r}")
    chk("新类定义/四拍/scope_tags 与计划逐字一致", not missing, f"差异 {missing}")
    for _id, fields in FIELD_UPDATES.items():
        r = rows.get(_id, {})
        bad = [k for k, v in fields.items() if str(r.get(k) or "") != v]
        chk(f"{_id} 改写落库", not bad, f"未落实 {bad}；现定义={r.get('definition')!r}")
    chk("A04 定义已去单向性", "主角落入算计" not in (rows.get("A04", {}).get("definition") or ""))
    chk("全表无英文残词 competing",
        not any("competing" in (v.get("definition") or "") for v in rows.values()))
    # 只应动本表：其它表基线
    others = {}
    for t in ("plot_templates", "event_skeletons", "atomic_variants"):
        try:
            others[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            others[t] = "N/A"
    chk("plot_templates/event_skeletons/atomic_variants 未被本单改动（仅记录基线）", True,
        json.dumps(others))
    print(f"\nverify: {sum(1 for _, ok, _ in res if ok)}/{len(res)} PASS")
    return 0 if all(ok for _, ok, _ in res) else 1


def restore_check(con: sqlite3.Connection) -> int:
    if not BACKUP.exists():
        print(f"[restore-check] 备份缺失: {BACKUP}")
        return 1
    data = json.loads(BACKUP.read_text(encoding="utf-8"))
    rows = data["rows"]
    cur = read_table(con)
    missing_cols = [c for c in COLUMNS if c not in rows[0]]
    print(f"备份: {len(rows)} 行 / ts={data['backup_ts']} / 列缺失={missing_cols}")
    in_db = [r["id"] for r in rows if r["id"] in cur]
    print(f"备份行仍在库: {len(in_db)}/{len(rows)}（原 64 行应全在；被本单改写的 5 行值已变，需按备份 UPDATE 还原）")
    old_by_id = {r["id"]: r for r in rows}
    diffs = []
    for _id in sorted(UPD_IDS):
        now_row, old = cur.get(_id), old_by_id.get(_id)
        if not now_row or not old:
            continue
        for c in COLUMNS:
            if str(now_row[c]) != str(old[c]):
                diffs.append((_id, c, old[c], now_row[c]))
    print(f"\n存量改写行的可还原差异 {len(diffs)} 处:")
    for _id, c, o, n in diffs:
        print(f"   {_id}.{c}: 现={n!r} ← 备份={o!r}")
    new_in_db = sorted(NEW_IDS & set(cur))
    print(f"\n新类 {len(new_in_db)} 条在库，回滚 = DELETE FROM {TABLE} WHERE id IN (...)")
    sql = [f"-- [DEV-SK05] atomic_events 回滚 SQL（备份 {data['backup_ts']}）"]
    sql.append(f"DELETE FROM {TABLE} WHERE id IN ({','.join(repr(i) for i in new_in_db)});")
    for _id in sorted(UPD_IDS):
        old = old_by_id.get(_id)
        if not old:
            continue
        def lit(col):
            v = old[col]
            if v is None:
                return "NULL"          # scope_tags 存量是 SQL NULL，不是字符串 "null"
            txt = json.dumps(v, ensure_ascii=False) if col == "scope_tags" else str(v)
            return "'" + txt.replace("'", "''") + "'"   # 单引号字面量，严格模式也成立
        sets = ", ".join(f"{c}={lit(c)}" for c in COLUMNS if c not in ("id", "created_at"))
        sql.append(f"UPDATE {TABLE} SET {sets} WHERE id={_id!r};")
    (ROLLBACK).write_text("\n".join(sql), encoding="utf-8")
    print(f"回滚 SQL → {ROLLBACK}")
    ok = len(rows) == 64 and not missing_cols and len(in_db) == 64
    print(f"restore-check: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--restore-check", action="store_true")
    ap.add_argument("--db", default=str(PROD_DB))
    ap.add_argument("--force", action="store_true",
                    help="跳过 8000 端口守卫（仅在确认后端已停用时使用）")
    ap.add_argument("--tag", default="", help="日志文件名后缀（副本试跑用 _local）")
    a = ap.parse_args()

    LOGDIR.mkdir(parents=True, exist_ok=True)
    global BACKUP, ROLLBACK
    if a.tag:
        BACKUP = LOGDIR / f"atomic_events_备份{a.tag}.json"
        ROLLBACK = LOGDIR / f"sk05_回滚{a.tag}.sql"
    mode = "dry_run" if a.dry_run else ("apply" if a.apply else
           ("verify" if a.verify else "restore_check"))
    log_path = LOGDIR / f"sk05_lexicon_{mode}{a.tag}.txt"
    sys.stdout = _Tee(log_path)
    print(f"[DEV-SK05] mode={mode} db={a.db}")
    con = sqlite3.connect(a.db)
    try:
        rc = {"dry_run": dry_run, "apply": lambda c: apply_db(c, a.force),
              "verify": verify, "restore_check": restore_check}[mode](con)
    finally:
        con.close()
    print(f"[DEV-SK05] {mode} rc={rc}")
    sys.stdout.flush()
    sys.exit(rc)


if __name__ == "__main__":
    main()
