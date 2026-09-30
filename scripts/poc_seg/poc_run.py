# -*- coding: utf-8 -*-
"""POC：章节生成 A/B 对比——A=单模型整章 vs B=骨架分段多智能体。

链路全部走 MyAPI 网关（127.0.0.1:9377/v1，llm.gateway_key）。
用法：
    python poc_run.py --no 6            # 用 article_plans 第 6 行 beat 当本章大纲
产物：
    outputs/poc_seg/line{no}_plan.json  规划派工单（含代码补捞实体记录）
    outputs/poc_seg/line{no}_A.txt      A 路整章文本
    outputs/poc_seg/line{no}_B.txt      B 路整章文本（分段拼装+修订后）
    outputs/poc_seg/line{no}_report.json 探针与用量数据
    outputs/poc_seg/line{no}_report.md   人读报告
    outputs/poc_seg/line{no}_blind.html  A/B 盲评页（甲/乙随机）
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import urllib.request

# ---------------------------------------------------------------- 常量
ROOT = Path(r"E:\AI小说创作")
OUT = ROOT / "outputs" / "poc_seg"
OUT.mkdir(parents=True, exist_ok=True)

GW_BASE = "http://127.0.0.1:9377/v1"
DB_MAIN = "file:C:/Users/w3013/.ai_novel/data/novel_agent.db?mode=ro"
DB_VEC = r"C:\Users\w3013\.ai_novel\para_ref.db"
BOOK_ROOT = Path(r"E:\AI小说创作\小说")
BOOK_DIR = {
    "没钱修什么仙": "没钱修什么仙？ - 熊狼狗",
    "凡人修仙传": "凡人修仙传",
    "修真四万年": "修真四万年 - 卧牛真人",
}
PROJECT_ID = "b0a5851a-ee91-4e2b-98e2-9344e8897508"   # 原神是怎样练成的（陈峰线）
PLAN_ID = "54646c8dd81147f6a85c0e568dd2ee5e"           # 第一篇 10 行计划

M_MAIN = "qwen3.8-flash-next"   # Flash-Next（规划/打斗/情节/审核）
M_SMALL = "qwen3-8b"            # Qwen3-8B（对话/心理/旁白）
EMBED_MODEL = "BAAI/bge-m3"

STAGE_TYPES = ["对话", "打斗", "情节", "心理", "旁白"]
SMALL_TYPES = {"对话", "心理", "旁白"}          # 其余走 Flash-Next
TOP_K_FS = 3
REL_GAP = 0.04

_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- 网关
def gw_key() -> str:
    db = sqlite3.connect(DB_MAIN, uri=True)
    (v,) = db.execute("SELECT value FROM app_configs WHERE key='llm.gateway_key'").fetchone()
    db.close()
    return json.loads(v) if v.strip().startswith(('"', "'", "[")) else v.strip()


KEY = gw_key()


def chat(model: str, messages: list[dict], max_tokens: int = 4096,
         temperature: float = 0.75, usage_acc: list | None = None) -> str:
    """流式累积（E10 教训：非流式长请求会被网关 300s 空闲超时掐掉，流式有字节流保活）。"""
    body = json.dumps({
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature, "stream": True, "stream_options": {"include_usage": True},
    }).encode("utf-8")
    req = urllib.request.Request(
        GW_BASE + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    parts: list[str] = []
    usage = {}
    for attempt in range(3):
        t0 = time.time()
        try:
            parts, usage = [], {}
            r = _op.open(req, timeout=900)
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                ch = (chunk.get("choices") or [{}])[0].get("delta") or {}
                if ch.get("content"):
                    parts.append(ch["content"])
            if parts:
                break
            raise RuntimeError("流式响应无 content")
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                print(f"    [retry {attempt+1}] {model} {type(e).__name__}，10s 后重试",
                      flush=True)
                time.sleep(10 * (attempt + 1))
                continue
            raise
    dt = time.time() - t0
    print(f"    [chat] {model} {dt:.0f}s out={len(''.join(parts))}字", flush=True)
    if usage_acc is not None:
        usage_acc.append({"model": model, "s": round(dt, 1),
                          "in": usage.get("prompt_tokens", 0),
                          "out": usage.get("completion_tokens", 0)})
    return "".join(parts).strip()


def embed(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        GW_BASE + "/embeddings", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    r = _op.open(req, timeout=120)
    data = json.loads(r.read().decode("utf-8"))
    items = sorted(data["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in items]


# ---------------------------------------------------------------- 主库素材
def load_plan_line(no: int) -> dict:
    db = sqlite3.connect(DB_MAIN, uri=True)
    (raw,) = db.execute("SELECT plan FROM article_plans WHERE id=?", (PLAN_ID,)).fetchone()
    db.close()
    plan = json.loads(raw)
    for line in plan["lines"]:
        if line["no"] == no:
            return line
    raise SystemExit(f"计划行 {no} 不存在")


def load_chars(names: set[str]) -> list[dict]:
    db = sqlite3.connect(DB_MAIN, uri=True)
    rows = db.execute(
        "SELECT name, role_type, personality, background, talent, current_level, brief "
        "FROM characters WHERE project_id=?", (PROJECT_ID,)).fetchall()
    db.close()
    out = []
    for name, rt, pers, bg, tal, lvl, brief in rows:
        if any(name_hit(name, n) or n == name for n in names):
            out.append({
                "name": name, "role_type": rt,
                "personality": (pers or "")[:200], "background": (bg or "")[:200],
                "talent": (tal or "")[:80], "level": (lvl or "")[:40],
                "brief": (brief or "")[:120],
            })
    return out


def load_world_names() -> dict[str, list[tuple[str, str, str]]]:
    """返回 {items: [(name, category, desc)], skills: [...], chars: [names], other: [names]}"""
    db = sqlite3.connect(DB_MAIN, uri=True)
    items = [(r[0], r[1] or "", (r[2] or r[3] or "")) for r in db.execute(
        "SELECT name, category, full_desc, brief FROM global_items")]
    skills = [(r[0], r[1] or "", (r[2] or r[3] or "")) for r in db.execute(
        "SELECT name, category, full_desc, brief FROM global_skills")]
    fac = [r[0] for r in db.execute(
        "SELECT name FROM factions WHERE project_id=?", (PROJECT_ID,))]
    loc = [r[0] for r in db.execute(
        "SELECT name FROM locations WHERE project_id=?", (PROJECT_ID,))]
    chn = [r[0] for r in db.execute(
        "SELECT name FROM characters WHERE project_id=?", (PROJECT_ID,))]
    db.close()
    return {"items": items, "skills": skills,
            "names": set(fac) | set(loc) | set(chn)}


def load_skills_block() -> str:
    db = sqlite3.connect(DB_MAIN, uri=True)
    rows = db.execute(
        "SELECT name, prompt_body FROM custom_skills "
        "WHERE trigger='chapter' AND enabled=1 ORDER BY category, priority DESC").fetchall()
    db.close()
    return "\n\n".join(f"【{n}】\n{b}" for n, b in rows)


def entity_cards(text: str, world: dict, extra_names: set[str] = frozenset()) -> list[str]:
    """子串匹配：文本里出现过的库名 → 卡片。"""
    cards = []
    seen = set()
    for bucket in ("items", "skills"):
        for name, cat, desc in world[bucket]:
            if len(name) >= 2 and name in text and name not in seen:
                seen.add(name)
                cards.append(f"· {name}（{cat}）：{desc[:150]}")
    for name in world["names"] | extra_names:
        if name not in seen and name_hit(name, text):
            seen.add(name)
            cards.append(f"· {name}（已登记实体，设定以角色卡/前文为准）")
    return cards


# ---------------------------------------------------------------- few-shot 库
class FSIndex:
    def __init__(self):
        con = sqlite3.connect(DB_VEC)
        rows = con.execute(
            "SELECT book,src_file,start_para,end_para,vec FROM block_vec").fetchall()
        con.close()
        self.mat = np.asarray([struct.unpack(f"{len(r[4])//4}f", r[4]) for r in rows],
                              dtype=np.float32)
        self.meta = [(r[0], r[1], r[2], r[3]) for r in rows]
        self._text_cache: dict[tuple, str] = {}

    def block_text(self, i: int) -> str:
        key = self.meta[i]
        if key not in self._text_cache:
            book, fn, sp, ep = key
            d = BOOK_ROOT / BOOK_DIR.get(book, book) / fn
            paras = d.read_text(encoding="utf-8", errors="ignore").splitlines()
            paras = [p.strip() for p in paras if p.strip()]
            self._text_cache[key] = "\n".join(paras[sp:ep + 1])[:600]
        return self._text_cache[key]

    @staticmethod
    def bucket_of(text: str) -> str:
        if "「" in text or "”" in text or "“" in text:
            return "对话"
        if any(k in text for k in "刀剑拳掌劈斩血爪轰撞杀暴起闪身"):
            return "打斗"
        if any(k in text for k in ("心中", "心里", "暗道", "想道", "心念", "心头发")):
            return "心理"
        return "旁白"

    def search(self, qvec: list[float], stage_type: str, top_k: int = TOP_K_FS) -> list[str]:
        q = np.asarray(qvec, dtype=np.float32)
        scores = self.mat @ q
        idx = np.argsort(-scores)[:80]
        out: list[str] = []
        want = stage_type if stage_type in ("对话", "打斗", "心理") else None
        for i in idx:
            if len(out) >= top_k:
                break
            txt = self.block_text(int(i))
            b = self.bucket_of(txt)
            if want and b != want:
                continue
            if not want and b == "旁白" and stage_type == "情节":
                pass  # 情节段全桶可搜
            out.append(f"（{self.meta[int(i)][0]}·{b}）{txt}")
        if not out:  # 桶内没货 → 全库兜底
            for i in idx[:top_k]:
                out.append(f"（{self.meta[int(i)][0]}）{self.block_text(int(i))}")
        return out


# ---------------------------------------------------------------- 探针
_END_RE = re.compile(r"(终于|随着|仿佛在诉说|预示着|落下了帷幕|故事才刚刚|这一刻[，他]|他知道，)")
_DUP_RE = re.compile(r"(布条|石头|灵石|薄册|凉意|暖意|掌心)")


def probe(text: str, target: int, stages: list[dict] | None = None) -> dict:
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chars = len(text.replace("\n", "").replace(" ", ""))
    dialog = sum(len(p) for p in paras if ("「" in p or "”" in p or "“" in p))
    tail = "\n".join(paras[-3:])
    dups = {w: text.count(w) for w in _DUP_RE.findall(text)}
    out = {
        "chars": chars, "target": target, "hit": round(chars / target, 2),
        "paras": len(paras),
        "avg_para": round(chars / max(len(paras), 1)),
        "dialog_ratio": round(dialog / max(chars, 1), 2),
        "ending_summary_hits": len(_END_RE.findall(tail)),
        "top_word_freq": sorted(dups.items(), key=lambda x: -x[1])[:3],
    }
    if stages:
        hit = 0
        rows = []
        for st in stages:
            sc = len(st.get("text", "").replace("\n", ""))
            lo, hi = st["word_budget"]
            ok = lo * 0.7 <= sc <= hi * 1.4
            hit += ok
            rows.append({"seq": st["seq"], "type": st["type"],
                         "budget": [lo, hi], "actual": sc, "ok": ok})
        out["stage_budget"] = rows
        out["budget_hit_rate"] = round(hit / max(len(rows), 1), 2)
    return out


def extract_json(s: str) -> dict:
    """鲁棒解析：raw_decode 循环抽出所有顶层对象，多对象时合并/择优。"""
    s = re.sub(r"^```(json)?|```$", "", s.strip(), flags=re.M).strip()
    dec = json.JSONDecoder()
    objs: list = []
    idx = 0
    while True:
        i = s.find("{", idx)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(s, i)
            objs.append(obj)
            idx = end
        except json.JSONDecodeError:
            idx = i + 1
    if not objs:
        raise ValueError("无 JSON：" + s[:200])
    for o in objs:
        if isinstance(o, dict) and "stages" in o:
            return o
    merged = [o for o in objs if isinstance(o, dict) and "seq" in o and "type" in o]
    if merged:
        merged.sort(key=lambda x: x.get("seq", 0))
        return {"stages": merged}
    return objs[0]


def name_hit(db_name: str, text: str) -> bool:
    """库内长名（如 旧书摊主郑老翁） vs 文本短名（郑老翁）：名字或 3 字尾缀命中。"""
    if len(db_name) < 2:
        return False
    if db_name in text:
        return True
    return len(db_name) >= 5 and "（" not in db_name and db_name[-3:] in text


# ---------------------------------------------------------------- A 路
def run_a(line: dict, ctx_block: str, usage: list) -> str:
    sys_p = "你是网文作者。遵守以下写作纪律：\n" + ctx_block["skills"]
    usr = f"""写一章小说正文。

【本章大纲】{line['beat']}：{line['summary']}
【出场角色】
{ctx_block['chars']}
【相关实体】
{ctx_block['cards'] or '（无）'}
【世界观要点】
{ctx_block['world']}

【要求】
1. 目标 {line['target_words']} 字左右（±10%）。
2. 结尾落在钩子上：{line['hook']}
3. 直接输出正文，不写标题、不写解释。

正文："""
    return chat(M_MAIN, [{"role": "system", "content": sys_p},
                         {"role": "user", "content": usr}],
                max_tokens=8192, temperature=0.75, usage_acc=usage)


# ---------------------------------------------------------------- B 路
PLANNER_PROMPT = """你是章节规划师。把本章拆成 4~7 个阶段，输出 JSON。

【阶段字段】
seq：序号；type：对话/打斗/情节/心理/旁白 之一；desc：一句话说明；
word_budget：[最小字数, 最大字数]（按戏剧需要定，干净利落可以 150，缠斗可以 600~700，不要平均分配）；
skeleton：该段骨架草稿——剧情节拍+关键台词原话+动作要点，长度约为目标字数的 1/3，
禁止润色描写（对话段要写出关键台词原话）；entities：该段会用到的已有实体名列表；
new_entities：需要新出现的实体（没有则空数组）。

【本章大纲】{beat}：{summary}
【出场角色】
{chars}
【相关实体】
{cards}
【世界观要点】
{world}
【结尾钩子】{hook}

只输出 JSON：
{{"stages": [{{"seq":1,"type":"情节","desc":"…","word_budget":[300,500],"skeleton":"…","entities":[],"new_entities":[]}}]}}"""

TYPE_PROMPT = {
    "对话": "本段以对话为主：台词要有潜台词与攻防，禁「气氛地说」类中性对白标签；叙述句只做动作切分。",
    "打斗": "本段以动作/对抗为主：动作有因果与空间感，禁堆形容词，一招一果。",
    "情节": "本段负责推进剧情：信息通过事件与动作给出，不做总结陈词。",
    "心理": "本段以内心活动为主：念头具体、有对象，禁空泛感慨。",
    "旁白": "本段以场景与过渡为主：白描，短句，给下一段留接口。",
}


def run_b(line: dict, ctx_block: str, usage: list, fs: FSIndex) -> tuple[str, dict]:
    trace = {"stages": []}
    # 1) 规划
    plan_raw = chat(M_MAIN, [
        {"role": "system", "content": "只输出 JSON，不输出任何其他文字。"},
        {"role": "user", "content": PLANNER_PROMPT.format(
            beat=line["beat"], summary=line["summary"], hook=line["hook"],
            chars=ctx_block["chars"], cards=ctx_block["cards"], world=ctx_block["world"])},
    ], max_tokens=3000, temperature=0.4, usage_acc=usage)
    try:
        plan = extract_json(plan_raw)
    except Exception:
        (OUT / "plan_raw_fail.json").write_text(plan_raw, encoding="utf-8")
        raise
    stages = plan["stages"]
    # 2) 确定性实体补捞
    for st in stages:
        have = set(st.get("entities") or [])
        found = entity_cards(st["desc"] + st.get("skeleton", ""), ctx_block["world_raw"])
        st["_cards_auto"] = [c for c in found if c.split("（")[0][2:] not in have]
    # 3) few-shot 检索（批量 embedding）
    skels = [st.get("skeleton") or st["desc"] for st in stages]
    vecs = embed(skels)
    for st, v in zip(stages, vecs):
        st["_fewshots"] = fs.search(v, st["type"])
    # 4) 子 agent 并发改写
    def _one(st: dict) -> None:
        model = M_SMALL if st["type"] in SMALL_TYPES else M_MAIN
        lo, hi = st["word_budget"]
        neighbors = "\n".join(
            f"· [{s['type']}] {s.get('skeleton') or s['desc']}"
            for s in stages if abs(s["seq"] - st["seq"]) == 1)
        cards = "\n".join(entity_cards(
            st["desc"] + st.get("skeleton", ""), ctx_block["world_raw"])) or "（无）"
        sys_p = ("你是网文作者，负责把一段骨架扩写成品。遵守写作纪律：\n"
                 + ctx_block["skills"] + "\n\n本段写法要求：" + TYPE_PROMPT[st["type"]])
        usr = f"""【全章骨架（前后相邻段）】
{neighbors}

【本段骨架（seq {st['seq']}，{st['type']}）】
{st.get('skeleton') or st['desc']}

【本章角色卡】
{ctx_block['chars']}

【本段相关实体】
{cards}

【参考——真书同类场景原句，只学写法与节奏，禁止抄内容、人名、设定】
{chr(10).join(st['_fewshots'])}

【要求】
1. 只输出本段正文（{lo}~{hi} 字），不写标题、不写解释。
2. 严格按骨架节拍顺序写：骨架里的每个节拍都要出现，关键台词内容必须保留（措辞可润色）。
3. 骨架之外不得新增情节、人物、物件、招式、回忆；专有名词只准用骨架、本段实体卡和角色卡里出现过的。
4. 对话一律用中文全角引号“”，禁用直引号。
5. 结尾自然停在骨架的最后一个节拍，不抢后文，不加总结感慨。"""
        st["text"] = chat(model, [{"role": "system", "content": sys_p},
                                  {"role": "user", "content": usr}],
                          max_tokens=3000, temperature=0.75, usage_acc=usage)
        st["_model"] = model

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_one, st) for st in stages]
        for f in as_completed(futs):
            f.result()
    stages.sort(key=lambda s: s["seq"])
    draft = "\n\n".join(st["text"] for st in stages)
    # 5) 审核轮（只出修改指令；骨架一并提供对照）
    skel_block = "\n".join(
        f"[{st['seq']}]（{st['type']}）{st.get('skeleton') or st['desc']}" for st in stages)
    review = chat(M_MAIN, [
        {"role": "system", "content": "只输出 JSON。"},
        {"role": "user", "content": f"""审核一章小说的分段草稿。逐项核对，宁多报不漏报。

【本章大纲】{line['beat']}：{line['summary']}
【结尾钩子】{line['hook']}
【各段骨架】
{skel_block}
【分段草稿（段前标注 seq）】
{draft}

必须逐项检查：
a. 大纲要素逐条核对：大纲里每个事件/人物是否都写到了？漏了哪条？
b. 幻觉扫描：草稿里出现、但大纲和骨架里都没有的专有名词（物件/人名/招式/地名/事件），逐个列出并给 seq。
c. 人设矛盾、体系错误（如品级/术语错误）。
d. 段间衔接断裂。
e. ASCII 直引号段落（应换中文引号）。
每条 {{"seq": 阶段号, "problem": "…", "instruction": "怎么改"}}。没问题才允许 {{"issues": []}}。
只输出 JSON。"""},
    ], max_tokens=2000, temperature=0.2, usage_acc=usage)
    rev = extract_json(review)
    trace["issues"] = rev.get("issues", [])
    # 6) 按指令修订（单轮，只改被点名的段）
    if trace["issues"]:
        flagged = {i["seq"] for i in trace["issues"]}
        inst_map = {i["seq"]: i["instruction"] for i in trace["issues"]}
        for st in stages:
            if st["seq"] not in flagged:
                continue
            model = st["_model"]
            prev_t = next((s["text"] for s in stages if s["seq"] == st["seq"] - 1), "")
            next_t = next((s["text"] for s in stages if s["seq"] == st["seq"] + 1), "")
            lo, hi = st["word_budget"]
            st["text"] = chat(model, [
                {"role": "user", "content": f"""按审稿意见修订这一段（其余不动）。

【大纲】{line['beat']}：{line['summary']}
【本段原文】
{st['text']}

【前段结尾】
{prev_t[-300:]}

【后段开头】
{next_t[:300]}

【审稿意见】{inst_map[st['seq']]}

只输出修订后的本段正文（{lo}~{hi} 字）。"""},
            ], max_tokens=3000, temperature=0.7, usage_acc=usage)
        draft = "\n\n".join(st["text"] for st in stages)
    trace["stages"] = [{k: st.get(k) for k in
                        ("seq", "type", "desc", "word_budget", "skeleton",
                         "_model", "_fewshots", "text", "_cards_auto")}
                       for st in stages]
    return draft, trace


# ---------------------------------------------------------------- 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no", type=int, default=6)
    args = ap.parse_args()
    line = load_plan_line(args.no)
    world = load_world_names()
    involved = set(line.get("new_chars") or []) | set(line.get("recall_chars") or [])
    involved |= {n for n in world["names"] if name_hit(n, line["summary"])}
    chars = load_chars(involved)
    chars_block = "\n".join(
        f"· {c['name']}（{c['role_type']}，{c['level']}）：{c['personality']} "
        f"背景：{c['background']}" for c in chars) or "（无）"
    cards = "\n".join(entity_cards(line["summary"], world)) or "（无）"
    ctx = {
        "chars": chars_block, "cards": cards, "skills": load_skills_block(),
        "world": "· 灵石是修真界硬通货（下品/中品/上品/极品），吸完灵力后成废石；"
                 "灵石不会自己发热、跳动、发光——物品异常不是悬念。\n"
                 "· 药铺杂役陈峰没听过「灵气」「引气入体」等术语，这些词只能从别人嘴里出现。\n"
                 "· 修真界以物易物为主，下品灵石≈2 两白银。",
        "world_raw": world,
    }
    print(f"[1] 计划行 no={args.no} {line['beat']} 角色数={len(chars)}")

    t0 = time.time()
    usage_a: list = []
    text_a = run_a(line, ctx, usage_a)
    t_a = time.time() - t0
    print(f"[2] A 路完成 {len(text_a)} 字 / {t_a:.0f}s / {len(usage_a)} 次调用")
    (OUT / f"line{args.no}_A.txt").write_text(text_a, encoding="utf-8")

    t0 = time.time()
    usage_b: list = []
    fs = FSIndex()
    text_b, trace = run_b(line, ctx, usage_b, fs)
    t_b = time.time() - t0
    print(f"[3] B 路完成 {len(text_b)} 字 / {t_b:.0f}s / {len(usage_b)} 次调用"
          f" / 阶段 {len(trace['stages'])} / 审核问题 {len(trace['issues'])}")

    rep = {
        "line": line,
        "A": {"text": text_a, "probe": probe(text_a, line["target_words"]),
              "usage": usage_a, "wall_s": round(t_a)},
        "B": {"text": text_b, "probe": probe(text_b, line["target_words"],
                                             trace["stages"]),
              "usage": usage_b, "wall_s": round(t_b),
              "issues": trace["issues"]},
    }
    (OUT / f"line{args.no}_plan.json").write_text(
        json.dumps(trace["stages"], ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / f"line{args.no}_A.txt").write_text(text_a, encoding="utf-8")
    (OUT / f"line{args.no}_B.txt").write_text(text_b, encoding="utf-8")
    (OUT / f"line{args.no}_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    pa, pb = rep["A"]["probe"], rep["B"]["probe"]
    md = [f"# POC 报告：line {args.no}「{line['beat']}」", "",
          "| 指标 | A 单模型 | B 分段 |", "|---|---|---|",
          f"| 字数/目标 | {pa['chars']}/{pa['target']}（{pa['hit']}） | {pb['chars']}/{pb['target']}（{pb['hit']}） |",
          f"| 段落数 / 均段长 | {pa['paras']} / {pa['avg_para']} | {pb['paras']} / {pb['avg_para']} |",
          f"| 对话占比 | {pa['dialog_ratio']} | {pb['dialog_ratio']} |",
          f"| 结尾总结式命中 | {pa['ending_summary_hits']} | {pb['ending_summary_hits']} |",
          f"| 高频词 | {pa['top_word_freq']} | {pb['top_word_freq']} |",
          f"| 调用次数 / tokens | {len(usage_a)} / "
          f"{sum(u['in'] for u in usage_a)}+{sum(u['out'] for u in usage_a)} | "
          f"{len(usage_b)} / {sum(u['in'] for u in usage_b)}+{sum(u['out'] for u in usage_b)} |",
          f"| 墙钟 | {rep['A']['wall_s']}s | {rep['B']['wall_s']}s |"]
    if pb.get("stage_budget"):
        md += ["", "## B 路阶段字数", "",
               "| seq | 类型 | 预算 | 实际 | 达标 | 模型 |", "|---|---|---|---|---|---|"]
        for r in pb["stage_budget"]:
            st = next(s for s in trace["stages"] if s["seq"] == r["seq"])
            md.append(f"| {r['seq']} | {r['type']} | {r['budget']} | {r['actual']} "
                      f"| {'✅' if r['ok'] else '❌'} | {st.get('_model')} |")
    if trace["issues"]:
        md += ["", "## 审核轮问题", ""] + [f"- [{i['seq']}] {i['problem']} → {i['instruction']}"
                                          for i in trace["issues"]]
    (OUT / f"line{args.no}_report.md").write_text("\n".join(md), encoding="utf-8")

    # 盲评页（甲乙随机，reveal 折叠）
    import random
    rnd = random.Random(args.no)
    order = ["A", "B"] if rnd.random() < 0.5 else ["B", "A"]
    labels = {"A": ("甲" if order[0] == "A" else "乙"), "B": ("乙" if order[0] == "A" else "甲")}
    def esc(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;")
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>POC 盲评 line{args.no} {line['beat']}</title><style>
body{{font-family:system-ui,'Microsoft YaHei';max-width:860px;margin:24px auto;padding:0 16px;line-height:1.75}}
h2{{border-left:4px solid #534AB7;padding-left:10px}}
.txt{{white-space:pre-wrap;background:#fafafa;border:1px solid #e3e3e3;border-radius:8px;padding:18px}}
button{{padding:6px 16px;margin:6px 4px}}
</style></head><body>
<h1>盲评：同一大纲两种生成（{line['beat']}）</h1>
<p>大纲：{line['summary']}</p>
<h2>{labels['A']}（{pa['chars']} 字）</h2><div class="txt">{esc(text_a)}</div>
<h2>{labels['B']}（{pb['chars']} 字）</h2><div class="txt">{esc(text_b)}</div>
<details><summary>点开揭晓（自评完再看）</summary>
<p>{labels['A']} = A 路（单模型整章）；{labels['B']} = B 路（骨架分段多智能体）。
探针见 line{args.no}_report.md</p></details>
</body></html>"""
    (OUT / f"line{args.no}_blind.html").write_text(html, encoding="utf-8")
    print(f"[4] 产物已写入 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
