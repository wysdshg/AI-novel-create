# -*- coding: utf-8 -*-
"""段落写法打磨服务（2026-09-21 正式版）。

作者拍板的方案（2026-09-21）：
· **一次请求**：勾选多少段（可断续、可全选）就打包多少段 + 各自 top-5 参考，**只发一次魔搭开思考请求**
  ——Qwen3.8-Flash-Next 100w 上下文，4 万 token 输入是零头；魔搭按调用次数计费，1 次调用 = 真成本。
· **向量库用块库**（280 字 + 段落边界，2026-09-21 拍板放弃单段/块并存）：
  查询侧按同样规则切块 → 块数 ≈ 原段数/7 → 参考数量可控。
· 🔴 合规三不变：库内只存向量+定位（原文回源读）；输出对参考查重；本地使用不发布。

输出解析：AI 按「【段落 N】」逐段输出 → 后端按编号映射回原位置；
缺失/编号错误的段**保留原文**并在返回里标注（绝不静默覆盖）。
"""
import json
import logging
import re
import sqlite3
import struct
import time
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 向量库路径：正式版在数据目录（与主库同盘），实验期可指向 outputs
DEFAULT_BLOCK_DB = r"C:\Users\w3013\.ai_novel\para_ref.db"
BOOK_ROOT = Path(r"E:\AI小说创作\小说")
BOOK_DIR = {
    "没钱修什么仙": "没钱修什么仙？ - 熊狼狗",
    "凡人修仙传": "凡人修仙传",
    "修真四万年": "修真四万年 - 卧牛真人",
}
TOP_K = 2         # 2026-09-25 从 3 降到 2：作者要求**参考总量 ≤ 3500 字（≈一章字数）**
REF_BUDGET = 3500  # 🔴 全章参考硬预算（超出即裁条）——作者实测：参考越多，模型改动越小
REL_GAP = 0.04    # 相对阈值：低于 top1 - 0.04 的参考直接丢弃（至少留 1 条）
TARGET = 250      # 查询侧切块：与建库同规则
HARD_MAX = 380
MIN_PARA = 12
MS_TIMEOUT = 900
MS_MAX_TOKENS = 6000

PROMPT_TMPL = """你在给一章小说的**选中段落**做写法打磨（不是改剧情）。全章各段如下，标注「◆要打磨」的才改。

【全章段落】
{chapter_block}

【参考——同类场景，看人家是怎么写的（只服务于各自挂靠的段落）】
{refs}

【要求】
1. **可以改什么、不能改什么**（2026-09-25 作者拍板：不再"剧情一字不许动"）：
   可以改：写法、节奏、累赘段落、描写方式，以及**明显不合理的地方**
   （人物做了不符合处境的事、无关描述占了篇幅、旁人对正常东西过度关注……）。
   不能改：这一章**发生了什么**（谁做了什么、结局如何）、已经确立的人物关系。
   🔴 改不合理处时**只改到合理为止**，不要顺手扩写，也不要加新情节。
2. **写法放开改**：句子怎么断、用什么词、节奏怎么走、对话怎么落，都可以重写。
   参考是**教你怎么写这种场景**的——学句法与节奏；🔴 不要复用参考里的任何措辞、比喻、人名、地名、事件。
3. 不要只做同义替换（把"语气"换成"声调"不算改）。原段好的地方可留，但整段要**更像真人写的**。
4. **超过 80 字的段落要敢于删整句**：纯烘托气氛的环境句、跟剧情无关的细节、跟上一句重复的，整句删掉。
   ⚠️ 是「整句删」，不是「每句写短一点」——每句都缩会读起来干巴巴，更像机器写的。
5. 删句后要把衔接顺一遍：删完突兀就补一个短句接上。但只做衔接，不要顺手扩写。
6. 🔴 **不许丢信息**：原段的每个动作、每句台词的内容都必须在（可以换说法、可以改掉不合理处）。
7. 未标「◆要打磨」的段落**不要输出**。
8. 🔴 **输出格式**：只输出选中段落，每段以「【段落 N】」开头（N=原编号），按编号升序，段落之间空一行。
   不要任何说明、前言、markdown 标记。

【碰到不合理的地方，按下面的样子改（只学判断标准，不学词句内容）】
一、**写完了又追加一句解释/升级——追加的那句删掉**
  ✗ 不烫，就是温。像冬天刚捂热的被窝，又像活物呼吸时的体温。
  ✓ 不烫，就是温。
  （作者原话：「写到胸腔剧烈起伏就够了，后面的就 AI 味明显了。」
    **写完一个正确的感觉就停手**——追加的那句几乎必然是比喻，删掉反而更利落。）
二、**跟剧情无关的描述占了篇幅**
  ✗ 檐下的风铃响了两声。院里那口井的绳子磨得发亮。他走进屋，把门带上。
  ✓ 他走进屋，把门带上。
  （没参与剧情的景物，删掉。）
三、**人物做了不符合处境的事**
  ✗ 班头搜出石头，捏了捏，还给了他，什么也没问。
  ✓ 班头搜出石头，翻来覆去看了几遍：“山里捡的？”——见他点头，才把石头塞回去，药却扣下了。
  （搜出问题物件却不追问，不合常理；**要么追问，要么根本不搜**。）
四、**旁人对正常的东西过度反应**
  ✗ 班头眯着眼打量他，目光在他泥泞不堪的衣衫上停留了一瞬，随即移开。
  ✓ 班头眯着眼看了他一下：“这么大雨，跑什么丧？”
  （雨天赶路一身泥再正常不过，没人会盯着看；要写的是他**当下关心的事**。）
五、**身份差面前不会变通——面对官差/上位者/债主，正常人先软下来**
  ✗ “回春堂的伙计，给巡检司送止血散。”他微微躬身，语气恭敬却不卑微。
  ✓ “官爷，小人是回春堂的伙计，掌柜的吩咐小的给巡检司送些止血散。”他陪着笑，腰又弯低了些。
  （作者原话：「普通人面对官爷，哪怕他再怎么不凡，也会表面上讨好的。」
   「语气恭敬却不卑微」是**解说词**不是写法；真人的讨好是**开口先报身份和来意、陪笑、腰弯低些**。
   🔴 主角的不凡写在**事后怎么想怎么做**，不写在**当面怎么顶**。
   只有三种情况才硬：对方是死敌且已撕破脸 / 要他做违背底线的事 / 已经没有退路。）
六、**知识泄漏——把作者/读者知道的塞给了角色**（作者 2026-09-25 点破）
  ✗ 还没人告诉他这是什么，他却心头一悸，仿佛有什么东西在这一刻产生了共鸣。
  ✗ 那块最大的石头，在无人触碰的情况下，轻轻地跳动了一下。
  ✓ 他掂了掂，觉得这石头比寻常的沉，颜色也怪，就揣进怀里——想着拿回去垫桌脚正好。
  （🔴 判据：**他凭什么这么紧张？** 把理由找出来；找不到，就是作者在替他紧张。
   主角在**知道它是什么之前**，感觉只能是**单纯的好奇**——珍视必须发生在「知道」之后。
   🔴 物品自己跳动/发热/发光/共鸣 = 违反世界观，不是悬念；钩子要来自**人**，不能靠物品异常。
   想表现「这东西不寻常」，用**重量**（坠手）、**外观**（云纹/颜色不对）、**硬度**（砸不烂），
   或**别人的反应**（旁人认得成色、铺子肯收、有人在找）——不要用温度。）
七、**角色说出了他没听过的名词**（术语边界）
  ✗ 如果这真是灵石，掌柜的为什么还扣我工钱？
  ✓ 如果这真是宝贝，掌柜的为什么还扣我工钱？
  （药铺杂役知道「仙师」「仙人」，没听过「灵石」「灵气」「引气入体」——
   这些词只能从别人嘴里说出来，不能出现在他的心里和嘴上。）

直接输出。"""

_SEG_RE = re.compile(r"【段落\s*(\d+)】\s*\n?(.*?)(?=【段落\s*\d+】|$)", re.S)


# ---------------------------------------------------------------- 向量库
def _blob_to_vec(b: bytes) -> list[float]:
    return list(struct.unpack(f"{len(b)//4}f", b))


class BlockIndex:
    """块向量库（numpy 矩阵检索）。加载一次常驻。"""

    def __init__(self, db_path: str | Path):
        con = sqlite3.connect(str(db_path))
        self.rows = con.execute(
            "SELECT id,book,src_file,start_para,end_para,vec FROM block_vec").fetchall()
        con.close()
        if not self.rows:
            raise RuntimeError(f"块向量库为空: {db_path}")
        self.mat = np.asarray(
            [_blob_to_vec(r[5]) for r in self.rows], dtype=np.float32)   # (N,1024) 已 L2 归一
        self.meta = [(r[1], r[2], r[3], r[4]) for r in self.rows]        # (book, src_file, sp, ep)
        logger.info("[para_polish] 块索引加载: %s 块 / %.1f MB",
                    len(self.rows), len(self.mat) * 1024 * 4 / 1024 / 1024)

    def search(self, query_vec: list[float], top_k: int = TOP_K) -> list[dict]:
        q = np.asarray(query_vec, dtype=np.float32)
        scores = self.mat @ q                                            # (N,) 余弦（已归一）
        idx = np.argsort(-scores)[:top_k]
        out = []
        for i in idx:
            book, fn, sp, ep = self.meta[int(i)]
            out.append({"score": round(float(scores[int(i)]), 4),
                        "book": book, "src_file": fn,
                        "start_para": sp, "end_para": ep})
        return out

    def search_filtered(self, query_vec: list[float], top_k: int = TOP_K,
                        rel_gap: float = REL_GAP) -> list[dict]:
        """检索 + **相对阈值过滤**（2026-09-21 加）。

        为什么需要：实测块级检索的 top-5 分数分布极窄（0.667~0.721，极差仅 0.054）——
        第 2~5 名基本是等价陪跑，留着只增加上下文噪声与"被模仿"的风险。
        规则：只保留 `score >= top1 - rel_gap` 的条目（默认 0.04），至少保留 1 条。

        修好回源 bug 后每条参考 ~385 字，5 条 = 1900 字/段、全章近 1.5 万字（原文的 5 倍）
        ⇒ 数量必须收，质量才有意义（few-shot 边际收益在 4~8 条后趋平，过多即负收益）。
        """
        q = np.asarray(query_vec, dtype=np.float32)
        scores = self.mat @ q
        idx = np.argsort(-scores)[:top_k]
        if len(idx) == 0:
            return []
        top1 = float(scores[int(idx[0])])
        out = []
        for i in idx:
            s = float(scores[int(i)])
            # 🔴 rel_gap <= 0 表示**关闭过滤**（2026-09-25 修：此前 0 会被当成"严格阈值"只留 top1）
            if rel_gap and rel_gap > 0 and out and s < top1 - rel_gap:
                break                      # 已按分数降序，后续都更低
            book, fn, sp, ep = self.meta[int(i)]
            out.append({"score": round(s, 4), "book": book, "src_file": fn,
                        "start_para": sp, "end_para": ep})
        return out


_INDEX_CACHE: dict[str, BlockIndex] = {}


def get_index(db_path: str | None) -> BlockIndex:
    path = str(db_path or DEFAULT_BLOCK_DB)
    if path not in _INDEX_CACHE:
        _INDEX_CACHE[path] = BlockIndex(path)
    return _INDEX_CACHE[path]


# ---------------------------------------------------------------- 切块（查询侧，与建库同规则）
def chapter_paras(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if len(s) < MIN_PARA and out:
            out[-1] += s
            continue
        if len(s) < MIN_PARA:
            continue
        out.append(s)
    return out


def split_blocks(paras: list[str]) -> list[list[str]]:
    blocks, cur, cur_len = [], [], 0
    for p in paras:
        plen = len(p)
        if plen > HARD_MAX:
            if cur:
                blocks.append(cur); cur, cur_len = [], 0
            blocks.append([p]); continue
        if cur_len == 0:
            cur, cur_len = [p], plen; continue
        if cur_len >= TARGET:
            if cur_len + plen > HARD_MAX:
                blocks.append(cur); cur, cur_len = [p], plen
            else:
                cur.append(p); cur_len += plen
        else:
            cur.append(p); cur_len += plen
    if cur:
        blocks.append(cur)
    return blocks


def read_block_text(book: str, src_file: str, start_para: int, end_para: int,
                    max_chars: int = 220) -> str:
    """回源读块原文（库里只存向量+定位）。

    🔴 2026-09-21 修 bug：块库的 `start_para/end_para` 存的是**块序号 bi**（建库时
    `buf_meta.append((book, fn, bi, bi, ...))`，15032 块全部 start==end），
    **不是段落范围**。直接 `paras[bi:bi+1]` 只会读到「第 bi 段」这一个孤段（实测仅 35 字），
    块级检索拿到的完整语境（~350 字）被丢掉——参考退化成"来自不同章节的孤立碎片"，
    比没有参考更容易误导模型。
    现按与建库**同一套规则**重切该章的块，取第 bi 个块 → 还原正确语境。
    """
    d = BOOK_ROOT / BOOK_DIR.get(book, book) / src_file
    try:
        txt = d.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    blocks = split_blocks(chapter_paras(txt))
    bi = start_para
    if 0 <= bi < len(blocks):
        return "\n".join(blocks[bi])[:max_chars]
    # 兜底：序号越界时按段落范围读（兼容将来存真段落范围的库）
    paras = chapter_paras(txt)
    if end_para >= len(paras):
        return ""
    return "\n".join(paras[start_para:end_para + 1])[:max_chars]


# ---------------------------------------------------------------- 主流程
def polish_paragraphs(db: Session, *, chapter_text: str, para_ids: list[int] | None,
                      index_db: str | None, api_key: str,
                      call_llm=None, top_k: int = TOP_K,
                      no_ref: bool = False, rel_gap: float | None = None) -> dict:
    """一次请求打磨选中段落。

    para_ids: 1-based 段号列表（None/空 = 全选）；
    call_llm: 注入的 LLM 调用函数 prompt->str（由路由层按 provider 提供）；
    no_ref: **实验用**——跳过检索，不挂任何参考（验证"参考是否真有帮助"）；
    rel_gap: None=用默认 REL_GAP；0=关闭相对阈值过滤（实验用）。
    返回 {items:[{idx,original,polished,ov_self,ov_ref_max,refs}], missing:[...], stats:{...}}
    """
    from app.services import embedding_client as ec

    t0 = time.time()
    paras = chapter_paras(chapter_text)
    if not paras:
        return {"items": [], "missing": [], "stats": {"error": "章节为空"}}
    selected = sorted(set(para_ids)) if para_ids else list(range(1, len(paras) + 1))
    selected = [i for i in selected if 1 <= i <= len(paras)]
    if not selected:
        return {"items": [], "missing": [], "stats": {"error": "没有可用的段落编号"}}

    index = get_index(index_db)

    # ① 查询侧切块：把选中段按 280 字规则分块（连续选中段自然合并；孤段独立）
    sel_set = set(selected)
    q_paras = [(i, paras[i - 1]) for i in selected]
    q_blocks = split_blocks([p for _i, p in q_paras])
    # 块 → 覆盖的段号（按 selected 顺序均分映射）
    block_of_para: dict[int, int] = {}
    cursor = 0
    for bi, blk in enumerate(q_blocks):
        for _ in blk:
            if cursor < len(q_paras):
                block_of_para[q_paras[cursor][0]] = bi
                cursor += 1

    # ② 每块一次 embedding 检索 → 参考"挂"在块上
    # 🔴 2026-09-25 实验开关：`no_ref` 跳过检索（验证"参考到底帮不帮"）；`rel_gap=0` 关闭相对阈值过滤
    block_refs: dict[int, list[dict]] = {}
    if not no_ref:
        qvecs = ec.embed_texts(["\n".join(b) for b in q_blocks], api_key=api_key)
        for bi, qv in enumerate(qvecs):
            hits = index.search_filtered(qv, top_k, REL_GAP if rel_gap is None else rel_gap)
            refs = []
            for h in hits:
                t = read_block_text(h["book"], h["src_file"], h["start_para"], h["end_para"])
                if t:
                    refs.append({"score": h["score"],
                                 "src": f"{h['book']}|{h['src_file']}#{h['start_para']}-{h['end_para']}",
                                 "text": t})
            block_refs[bi] = refs
        # 🔴 硬预算：参考总量超过 REF_BUDGET 就逐块裁条（2026-09-25 作者要求 ≤3500 字）
        total = sum(len(r["text"]) for v in block_refs.values() for r in v)
        while total > REF_BUDGET:
            # 优先砍掉参考最多的那块（它稀释得最厉害）
            victim = max(block_refs, key=lambda k: sum(len(r["text"]) for r in block_refs[k]))
            v = block_refs[victim]
            if len(v) <= 1:
                break
            total -= len(v[-1]["text"])
            block_refs[victim] = v[:-1]
        logger.info("[para_polish] 参考预算: %d 字 / 上限 %d（%d 块）",
                    total, REF_BUDGET, len(block_refs))

    # ③ 组 prompt：全章编号列表（未选中标"不改动"）+ 选中段挂参考
    chapter_lines = []
    for i, p in enumerate(paras, 1):
        mark = "◆要打磨" if i in sel_set else "不改动"
        chapter_lines.append(f"【段落 {i}】【{mark}】\n{p}")
    ref_lines = []
    for i in selected:
        refs = block_refs.get(block_of_para.get(i, -1), [])
        if refs:
            body = "\n".join(f"  参考{j}（{r['src']}）：{r['text']}"
                             for j, r in enumerate(refs, 1))
            ref_lines.append(f"【段落 {i}】的参考：\n{body}")
    prompt = PROMPT_TMPL.format(
        chapter_block="\n\n".join(chapter_lines),
        refs="\n\n".join(ref_lines) or "（无参考，按要求自行打磨）")

    logger.info("[para_polish] 段落 %s/%s 选中，prompt %s 字符，1 次请求",
                len(selected), len(paras), len(prompt))
    raw = call_llm(prompt)

    # ④ 解析回填
    parsed: dict[int, str] = {}
    for m in _SEG_RE.finditer(raw):
        idx = int(m.group(1))
        txt = m.group(2).strip()
        if idx in sel_set and txt:
            parsed[idx] = txt

    def ngram_ov(a: str, b: str, k: int = 8) -> float:
        a2 = re.sub(r"\s+", "", a); b2 = re.sub(r"\s+", "", b)
        if len(a2) < k:
            return 0.0
        pool = {b2[i:i + k] for i in range(len(b2) - k + 1)}
        grams = [a2[i:i + k] for i in range(len(a2) - k + 1)]
        return round(sum(1 for g in grams if g in pool) / len(grams), 4)

    items, missing = [], []
    for i in selected:
        orig = paras[i - 1]
        new = parsed.get(i)
        if not new:
            missing.append(i)
            continue
        ov_ref = 0.0
        for r in block_refs.get(block_of_para.get(i, -1), []):
            ov_ref = max(ov_ref, ngram_ov(new, r["text"]))
        items.append({"idx": i, "original": orig, "polished": new,
                      "ov_self": ngram_ov(new, orig), "ov_ref_max": ov_ref,
                      "refs": block_refs.get(block_of_para.get(i, -1), [])})

    stats = {"paras": len(paras), "selected": len(selected),
             "polished": len(items), "missing": len(missing),
             "blocks": len(q_blocks), "refs_total": sum(len(v) for v in block_refs.values()),
             "secs": round(time.time() - t0, 1), "llm_calls": 1}
    logger.info("[para_polish] 完成: %s", stats)
    return {"items": items, "missing": missing, "stats": stats}
