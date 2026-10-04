# -*- coding: utf-8 -*-
"""[DEV-DATA01] 章节噪声清洗：模式判定 + 三趟算法 + 两类动作 + 台账核账 的零 LLM 单测。

素材全部用 tmp_path 里的假书目录，**绝不碰 E:\\AI小说创作\\小说\\**。
模式常量的取证来源见 backend/scripts/chapter_noise_patterns.py 文件头。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import scripts.clean_novel_chapters as cnc
from chapter_noise_patterns import (  # type: ignore  # noqa: E402
    EMPTY_BODY_MIN_CJK, SMALL_NOISE_MAX_BYTES, classify_pure_line, cjk_count,
)

# ───────────────────────── 素材片段（逐条抄自全库实测形态） ─────────────────────────
BODY = "他把药炉搁在案上，低头看了看掌心那道旧疤，很久没有这样安静过。"
BODY2 = "帐外风雪正紧，巡夜的脚步一圈一圈绕着辕门走，谁也没有先开口。"


@pytest.fixture()
def library(tmp_path, monkeypatch):
    """把模块级路径全部指向 tmp_path，构造 2 本书的假目录。"""
    root = tmp_path / "proj"
    novel = root / "小说"
    (novel / "测试一本").mkdir(parents=True)
    (novel / "测试二本").mkdir(parents=True)
    monkeypatch.setattr(cnc, "ROOT", root)
    monkeypatch.setattr(cnc, "NOVEL_ROOT", novel)
    monkeypatch.setattr(cnc, "OUT_DIR", root / "outputs" / "data_clean")
    monkeypatch.setattr(cnc, "BACKUP_DIR", root / "outputs" / "data_clean" / "backup")
    monkeypatch.setattr(cnc, "LEDGER", root / "outputs" / "data_clean" / "清洗台账.json")
    monkeypatch.setattr(cnc, "DECISIONS", root / "outputs" / "data_clean" / "扫描明细.json")
    monkeypatch.setattr(cnc, "QUARANTINE_EXTRA_NAMES", {})
    monkeypatch.setattr(cnc, "KEEP_NAMES", {})
    return novel


def w(book_dir: Path, name: str, text: str) -> Path:
    p = book_dir / name
    p.write_bytes(text.encode("utf-8"))
    return p


# ══════════════════════ 一、整行噪声模式：正例 + 反例 ══════════════════════
PURE_POSITIVE = [
    ("done_marker", "本章已完成！"),
    ("done_marker", "本章已完成</p>"),
    ("chapter_end_bracket", "（本章完）"),
    ("page_nag", "本章未完，请翻下一页继续阅读........."),
    ("page_nag", "小主，这个章节后面还有哦^.^，请点击下一页继续阅读，后面更精彩！"),
    ("site_thanks_brace", "{宜搜小说ysxsw感谢各位书友的支持，您的支持就是我们最大的动力}"),
    ("qidian_tail_lone", "(未完待续。如果您喜欢这部作品，欢迎您来起点投推荐票、月票，您的支持，就是我最大的动力。)"),
    ("update_count_mark", "【第二更！】"),
    ("update_count_mark", "（第一更！）"),
    ("chapter_done_plea", "三章完毕！番茄谢谢所有兄弟了！"),
    ("nav_bar", "上一页←皇家金牌县令章节目录→下一章"),
    ("page_counter", "第(3/3)页"),
    ("list_word", "列表"),
    ("site_prompt", "天才一秒记住本站地址：."),
    ("site_prompt", "手机版网址：m."),
    ("site_prompt", "由于xx问题不能显示：请关注微信公众号：大文学网，继续"),
    ("site_prompt", "我的qt房间开通了，qt房间号[1655]"),
    ("url_residue", "https://baiycap."),
    ("pinyin_domain", "woshixiongshizaitaiwenjianle"),
    ("domain_fragment_line", "info"),
    ("escaped_html_residue", "&lt;ahref=http://&gt;起点target=_nk>欢迎广大书友光临，最新、最快、最火的连载作品尽在起点原创！"),
    ("html_only", "**************</p>"),
    ("author_note_head_lone", "作者有话说："),
    ("ps_lone", "ps:"),
    ("plea_lone", "偷偷求票"),
    ("plea_lone", "ps：求月票。"),
    ("plea_collect", "(书友若觉得好看，请别忘收藏本书)"),
    ("author_edit_note", "书友160910125310853提醒，修改几个话语"),
]

PURE_NEGATIVE = [
    "四更。",                                    # 叙事时间词，不是更数标记
    "他握紧了拳头，眼中闪过一丝寒芒。",
    "biu！",                                     # 拟声词（全库 464 块）
    "“是！”",                                   # 高频叙事短句
    "……",                                       # 纯标点分节
    "《武朝》的春天来得晚，边军却先知道了。",
    "第二百三十一章 灵髓爆发",                     # 章标题行
    "榜单前三的名字被人用朱笔圈了出来。",
    "谢谢诸君的提醒！）(……)",                     # 含括号但非模板，交 TIER2 报告
]


@pytest.mark.parametrize("name,line", PURE_POSITIVE)
def test_pure_line_positive(name, line):
    assert classify_pure_line(line) == name, f"{line!r} 应命中 {name}"


@pytest.mark.parametrize("line", PURE_NEGATIVE)
def test_pure_line_negative(line):
    assert classify_pure_line(line) is None, f"{line!r} 是正文，不该命中任何整行噪声模式"


def test_star_prefix_retry_keeps_body_line():
    """行首有星号但剥掉后不是噪声模板 → 整行保留（正文里的 `**之内，皇帝之土。`）。"""
    assert classify_pure_line("**之内，皇帝之土。") is None
    assert classify_pure_line("******(未完待续。欢迎您来起点投推荐票、月票)") == "qidian_tail_lone"


# ══════════════════════ 二、行内噪声：只删片段不删行 ══════════════════════
def test_inline_domain_keeps_sentence():
    line = "张若尘在剑道上的造诣，即便是与最顶尖的剑道奇才相比，也丝毫不落下风。www.他修炼出来的剑道规则，比掌道规则更多。"
    st, name, new = cnc.classify_line(line)
    assert st == "strip" and name == "domain_inline"
    assert "www." not in new and "丝毫不落下风。他修炼出来的剑道规则" in new


def test_inline_mobile_prompt_keeps_sentence():
    line = "一直以来，张若尘的神经都绷得很紧，很少完全放松警惕痛痛快快的睡一觉。手机用户请浏览m.aiquxs.阅读，更优质的阅读体验。"
    st, _name, new = cnc.classify_line(line)
    assert st == "strip"
    assert "手机用户请浏览" not in new and "痛痛快快的睡一觉。" in new


def test_inline_nag_with_div_becomes_pure_drop():
    """赘婿：nag 行挂着 </div>，整行匹配不上；抠掉标签后必须回头再判一次整行噪声。"""
    line = "小主，这个章节后面还有哦^.^，请点击下一页继续阅读，后面更精彩！ </div>"
    st, name, _ = cnc.classify_line(line)
    assert st == "drop" and "page_nag" in name


def test_inline_qidian_truncated_tail():
    line = "Ps：第一章到！(未完待续。如果您喜欢这部作品，欢迎您来起点（qidian.com）投推荐票、月票，您的支持，就是我最大的动力。)"
    st, name, new = cnc.classify_line(line)
    assert st == "strip" and "未完待续" not in new and new.startswith("Ps：第一章到！")


def test_inline_no_hit_on_plain_body():
    st, name, new = cnc.classify_line(BODY)
    assert (st, name, new) == ("body", "", BODY)


# ══════════════════════ 三、三趟算法的安全边界 ══════════════════════
def chapter(*lines, eol="\r\n"):
    return eol.join(lines) + ("" if lines[-1] == "" else eol)


def test_star_separator_in_body_is_preserved():
    """吞噬星空/赘婿把 `******` 当正文分节符：文件后半部但没有求票话术 → 不许删。"""
    text = chapter("第一章 试炼", "", BODY * 3, "", "******", "", BODY * 3, "", BODY * 2, "")
    new, hits, _res = cnc.clean_text(text)
    assert "star_tail_plea" not in {h["pattern"] for h in hits}
    assert "******" in new


def test_star_line_followed_by_plea_is_dropped():
    body = BODY * 8
    text = chapter("第二章 风雪", "", body, "", body, "", "************",
                   "", "新书阶段，点击、收藏、投票一个都不能少，请大家支持作者。", "")
    new, hits, _res = cnc.clean_text(text)
    assert "star_tail_plea" in {h["pattern"] for h in hits}
    assert "************" not in new
    assert "点击、收藏、投票" not in new          # 星号块之后的作者话整段收掉
    assert "第二章 风雪" in new and body in new   # 正文一个字不少


def test_star_in_first_half_never_dropped():
    body = BODY * 6
    text = chapter("第三章", "", "************", "", "求月票！", "", body, "", body, "")
    new, hits, _res = cnc.clean_text(text)
    assert "star_tail_plea" not in {h["pattern"] for h in hits}
    assert "************" in new


def test_tail_block_stops_at_dialogue():
    """雪中悍刀行实测坑：正文对白里出现「打赏」，尾部回溯必须停，不许砍台词。"""
    body = BODY * 6
    text = chapter("第208章", "", body, "", body,
                   "", "徐凤年喃喃道：“老前辈，本世子没法子打赏啊。”", "", "本章已完成！", "")
    new, hits, _res = cnc.clean_text(text)
    assert "没法子打赏啊" in new
    assert "本章已完成！" not in new


def test_tail_block_requires_strong_marker():
    """没有强标记收尾时，就算末行含求票词也不回溯删（保守）。"""
    body = BODY * 6
    text = chapter("第9章", "", body, "", body, "", "他低声说，月票已经用尽了。", "")
    new, hits, _res = cnc.clean_text(text)
    assert "月票已经用尽了" in new
    assert "tail_author_block" not in {h["pattern"] for h in hits}


def test_tail_block_caps_size_and_lines():
    """尾部作者话块有总量/行数上限：再长也不会一路吃掉正文。"""
    body = BODY * 6
    tail = [f"第{k}次拜托：有月票的帮忙投一张，感谢支持。" for k in range(30)]
    text = chapter("第10章", "", body, "", body, "", *tail, "", "本章已完成！", "")
    new, hits, _res = cnc.clean_text(text)
    dropped = [h for h in hits if h["pattern"] == "tail_author_block"]
    assert dropped, "尾部求票块应该被收掉"
    assert len(dropped) <= 12                                   # TAIL_AUTHOR_MAX_LINES
    assert sum(cjk_count(h["text"]) for h in dropped) <= 300     # TAIL_AUTHOR_MAX_TOTAL_CJK
    assert "第17次拜托" in new and "第18次拜托" not in new        # 超上限的部分保留（保守）
    assert "本章已完成！" not in new and body in new


# ══════════════════════ 四、字节保真：换行与缩进 ══════════════════════
def test_crlf_and_no_trailing_newline_preserved():
    text = "第一章\r\n\r\n" + BODY + "\r\n\r\n本章已完成！"
    new, hits, _res = cnc.clean_text(text)
    assert hits and "\r\n" in new
    assert not new.endswith("\n") and not new.endswith("\r")   # 原本结尾无换行 → 保持无换行
    assert "\r\r" not in new and "\n" not in new.replace("\r\n", "")


def test_lf_file_stays_lf():
    text = "第一章\n\n" + BODY + "\n\n本章已完成！\n"
    new, _hits, _res = cnc.clean_text(text)
    assert "\r" not in new and new.endswith("\n")


def test_fullwidth_indent_survives_strip():
    line = "　　" + BODY + "（本章完）"
    st, _name, new = cnc.classify_line(line)
    assert st == "strip" and new.startswith("　　")


def test_double_blank_not_collapsed_in_untouched_region():
    """连续空行是 2406 个文件的既有形态，不是噪声：没删东西就不该压缩。"""
    text = "第一章\r\n\r\n\r\n" + BODY + "\r\n\r\n\r\n" + BODY2
    new, hits, _res = cnc.clean_text(text)
    assert not hits
    assert new == text                                   # 一个字节都不动


# ══════════════════════ 五、幂等 ══════════════════════
def test_clean_text_idempotent():
    body = BODY * 8
    text = chapter("第1章", "", body, "", "本章未完，请翻下一页继续阅读.........",
                   "", body, "", "************", "", "新书阶段，点击、收藏、投票一个都不能少。",
                   "", "本章已完成！", "")
    once, hits1, _r1 = cnc.clean_text(text)
    twice, hits2, _r2 = cnc.clean_text(once)
    assert hits1 and not hits2, "第二趟必须零命中"
    assert once == twice


# ══════════════════════ 六、decide() 判据优先级 ══════════════════════
def text_with_body(*extra_noise, times=8):
    return chapter("第1章 开端", "", *[BODY] * times, "", *extra_noise, "")


def test_decide_a_when_noise_but_body_ok(library):
    b = library / "测试一本"
    p = w(b, "0001_第一章 开端.txt", text_with_body("本章已完成！"))
    d = cnc.decide("测试一本", p)
    assert d["action"] == "A" and d["rule"] == "noise_hits"


def test_decide_none_when_clean(library):
    b = library / "测试一本"
    p = w(b, "0002_第二章 平静.txt", chapter("第二章 平静", "", BODY * 8, ""))
    assert cnc.decide("测试一本", p)["action"] == "none"


@pytest.mark.parametrize("fname", ["0987_完本感言.txt", "0003_番外一：劫后.txt", "0100_后记.txt"])
def test_decide_b_by_title(library, fname):
    b = library / "测试一本"
    p = w(b, fname, text_with_body("本章已完成！"))     # 正文很足也要隔离：标题主体即非正文
    d = cnc.decide("测试一本", p)
    assert d["action"] == "B" and d["rule"] == "title_nonbody"


def test_decide_b_by_empty_body(library):
    b = library / "测试一本"
    p = w(b, "0123_第一千二百三十五章 大结局.txt", chapter("第一千二百三十五章", "", "本章已完成！", ""))
    d = cnc.decide("测试一本", p)
    assert d["action"] == "B" and d["rule"] == "empty_body"
    assert d["residual_cjk"] < EMPTY_BODY_MIN_CJK


def test_decide_small_notice_title(library):
    b = library / "测试一本"
    noise = text_with_body("求月票。", times=1)          # 160 汉字 + 标题带「通知」+ 小文件
    p = w(b, "0386_第三百八十六章 春节更新通知.txt", noise)
    assert p.stat().st_size < SMALL_NOISE_MAX_BYTES
    assert cnc.decide("测试一本", p)["rule"] == "title_notice_small"


def test_decide_big_chapter_with_notice_title_stays_a(library):
    """《第580章金阳老祖兼婚礼请假通知》实测 10737B 是正文章 → 只能 A，不能整章隔离。"""
    b = library / "测试一本"
    p = w(b, "0580_第580章金阳老祖兼婚礼请假通知.txt", text_with_body("本章已完成！", times=40))
    assert p.stat().st_size >= SMALL_NOISE_MAX_BYTES
    assert cnc.decide("测试一本", p)["action"] == "A"


def test_bracket_aside_in_title_does_not_quarantine(library):
    """`第一千零四十九章 运气好（内附更新说明）`实测 10557B：章首一段 ps + 整段正文 → 只能 A。"""
    b = library / "测试一本"
    p = w(b, "1053_第一千零四十九章 运气好（内附更新说明）.txt",
          chapter("第一千零四十九章 运气好（内附更新说明）", "", "ps：昨天在医院折腾许久，住院治疗。",
                  "", *[BODY] * 40, "", "(未完待续。如果您喜欢这部作品，欢迎您来起点投推荐票、月票。)", ""))
    assert cnc.decide("测试一本", p)["action"] == "A"


def test_bracketed_plea_tag_does_not_quarantine(library):
    """`第一百三十二章 真假小药丸【求票】`是正文残段：票务后缀在括号里，不算「整章是通知」。"""
    b = library / "测试一本"
    p = w(b, "0136_第一百三十二章 真假小药丸【求票】.txt", text_with_body("求月票。", times=2))
    assert p.stat().st_size < SMALL_NOISE_MAX_BYTES
    assert cnc.decide("测试一本", p)["action"] == "A"


def test_bracketed_waifan_is_still_quarantined(library):
    """括号里写「番外」仍隔离（gate ① 拍板番外一律隔离：玄鉴仙族 `冬景（下）（番外 建议勿订）`）。"""
    b = library / "测试一本"
    p = w(b, "0535_第535章 冬景 （下）（番外 建议勿订）.txt", text_with_body("本章已完成！", times=30))
    d = cnc.decide("测试一本", p)
    assert d["action"] == "B" and d["rule"] == "title_nonbody"


def test_keep_names_overrides_everything(library, monkeypatch):
    b = library / "测试一本"
    p = w(b, "0751_五竹自述.txt", chapter("五竹自述", "", "本章已完成！", ""))
    monkeypatch.setattr(cnc, "KEEP_NAMES", {"测试一本": {"0751_五竹自述.txt"}})
    assert cnc.decide("测试一本", p)["action"] == "none"


def test_manual_quarantine_names(library, monkeypatch):
    b = library / "测试二本"
    p = w(b, "0064_写在回明完结之前.txt", text_with_body("求推荐票！", times=6))
    monkeypatch.setattr(cnc, "QUARANTINE_EXTRA_NAMES", {"测试二本": {"0064_写在回明完结之前.txt"}})
    assert cnc.decide("测试二本", p)["action"] == "B"


def test_skip_dirs_and_files_not_traversed(library):
    (library / "_残章").mkdir()
    w(library / "_残章", "0001_残.txt", "本章已完成！")
    w(library / "测试一本", "书籍信息.txt", "本章已完成！")
    assert [p.name for p in cnc.iter_books()] == ["测试一本", "测试二本"]
    assert cnc.chapter_files(library / "测试一本") == []


# ══════════════════════ 七、apply + 台账 + 核账（全在 tmp_path） ══════════════════════
def apply_args(**kw):
    ns = argparse.Namespace(decisions=str(cnc.DECISIONS), force=True)
    ns.decisions_path = Path(ns.decisions)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_apply_a_backups_rewrites_and_ledgers(library):
    b = library / "测试一本"
    src = w(b, "0001_第一章 开端.txt", text_with_body("本章已完成！"))
    before = src.read_bytes()
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    cnc.DECISIONS.write_text(json.dumps([{"rel": str(src.relative_to(cnc.ROOT)), "action": "A"}]),
                             encoding="utf-8")
    assert cnc.cmd_apply(apply_args()) == 0
    after = src.read_bytes()
    assert after != before and "本章已完成".encode() not in after
    assert BODY.encode() in after                              # 正文一个字节没少
    bak = cnc.BACKUP_DIR / "测试一本" / "0001_第一章 开端.txt"
    assert bak.exists() and bak.read_bytes() == before         # 备份 = 原文逐字节一致
    ledger = json.loads(cnc.LEDGER.read_text(encoding="utf-8"))
    assert len(ledger) == 1 and ledger[0]["action"] == "A"
    assert ledger[0]["backup"].endswith("0001_第一章 开端.txt")
    assert "ts_utc" in ledger[0] and ledger[0]["ts_utc"].endswith("+00:00")   # A16：UTC 时间戳


def test_apply_b_moves_to_quarantine_never_deletes(library):
    b = library / "测试一本"
    src = w(b, "0988_番外一：劫后.txt", "番外一：劫后\r\n\r\n本章已完成！")
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    cnc.DECISIONS.write_text(json.dumps([{"rel": str(src.relative_to(cnc.ROOT)), "action": "B"}]),
                             encoding="utf-8")
    assert cnc.cmd_apply(apply_args()) == 0
    assert not src.exists()
    dst = cnc.NOVEL_ROOT / "_quarantine" / "测试一本" / "0988_番外一：劫后.txt"
    assert dst.read_bytes() == "番外一：劫后\r\n\r\n本章已完成！".encode()   # 不删除、不改写
    assert json.loads(cnc.LEDGER.read_text(encoding="utf-8"))[0]["new_rel"].startswith(
        str(Path("小说") / "_quarantine"))


def test_apply_is_idempotent_on_rerun(library):
    b = library / "测试一本"
    src = w(b, "0001_第一章 开端.txt", text_with_body("本章已完成！"))
    rel = str(src.relative_to(cnc.ROOT))
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    cnc.DECISIONS.write_text(json.dumps([{"rel": rel, "action": "A"}]), encoding="utf-8")
    assert cnc.cmd_apply(apply_args()) == 0
    first = src.read_bytes()
    assert cnc.cmd_apply(apply_args()) == 0                    # 第二次：已干净 → 无事可做
    assert src.read_bytes() == first
    assert len(json.loads(cnc.LEDGER.read_text(encoding="utf-8"))) == 1   # 台账不重复记


def test_apply_refuses_without_force(library):
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    cnc.DECISIONS.write_text("[]", encoding="utf-8")
    assert cnc.cmd_apply(apply_args(force=False)) == 2


def test_apply_refuses_without_approved_list(library):
    assert cnc.cmd_apply(apply_args()) == 2                    # 没有批准清单 → 拒绝动手


def test_apply_drops_items_missing_from_approved_plan(library):
    """批准清单只含 A 那一个文件；B 文件是口径漂移 → 不进计划，也不得被改动。"""
    b = library / "测试一本"
    a = w(b, "0001_第一章 开端.txt", text_with_body("本章已完成！"))
    q = w(b, "0987_完本感言.txt", text_with_body("求月票。", times=6))
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    cnc.DECISIONS.write_text(json.dumps([{"rel": str(a.relative_to(cnc.ROOT)), "action": "A"}]),
                             encoding="utf-8")
    plan, mism = cnc.plan_from_disk({(str(a.relative_to(cnc.ROOT)), "A")})
    assert [d["file"] for d in plan] == [a.name]
    assert any("不在批准清单" in m for m in mism)
    assert q.exists() and not (cnc.NOVEL_ROOT / "_quarantine").exists()


def test_verify_passes_after_apply_and_flags_residue(library):
    b = library / "测试一本"
    src = w(b, "0001_第一章 开端.txt", text_with_body("本章已完成！"))
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    cnc.DECISIONS.write_text(json.dumps([{"rel": str(src.relative_to(cnc.ROOT)), "action": "A"}]),
                             encoding="utf-8")
    assert cnc.cmd_apply(apply_args()) == 0
    assert cnc.cmd_verify(apply_args()) == 0                   # 核账：按文件系统现状通过
    src.write_bytes(text_with_body("本章已完成！").encode("utf-8"))   # 人为回退成脏文件
    assert cnc.cmd_verify(apply_args()) == 1                   # 核账必须报不通过


def test_scan_writes_report_and_details(library, capsys):
    b = library / "测试一本"
    w(b, "0001_第一章 开端.txt", text_with_body("本章已完成！"))
    w(b, "0988_番外一：劫后.txt", "番外一：劫后\r\n\r\n本章已完成！")
    rc = cnc.cmd_scan(apply_args(report=str(cnc.OUT_DIR / "扫描报告.md")))
    assert rc == 0
    assert (cnc.OUT_DIR / "扫描报告.md").exists() and cnc.DECISIONS.exists()
    details = json.loads(cnc.DECISIONS.read_text(encoding="utf-8"))
    assert {d["file"] for d in details} == {"0001_第一章 开端.txt", "0988_番外一：劫后.txt"}
    assert all("lost_cjk" in d and "rule" in d for d in details)
    text = (cnc.OUT_DIR / "扫描报告.md").read_text(encoding="utf-8")
    assert "## 五、B/C 类" in text and "## 七、TIER2" in text


def test_body_loss_is_bounded_by_noise_size(library):
    """被删汉字只能来自噪声行：正文字数一个字都不能少。"""
    b = library / "测试一本"
    src = w(b, "0001_第一章 开端.txt",
            text_with_body("本章已完成！", "本章未完，请翻下一页继续阅读.........", times=6))
    before_body = cjk_count(src.read_bytes().decode("utf-8"))
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    cnc.DECISIONS.write_text(json.dumps([{"rel": str(src.relative_to(cnc.ROOT)), "action": "A"}]),
                             encoding="utf-8")
    assert cnc.cmd_apply(apply_args()) == 0
    after = src.read_bytes().decode("utf-8")
    assert after.count(BODY) == 6                              # 6 段正文逐段还在
    assert cjk_count(after) >= before_body - 40                # 只掉了噪声那十几字
    assert "本章已完成" not in after and "请翻下一页" not in after


# ══════════════════════ 十、C 类：书内重复章节（用户 2026-10-03 追加） ══════════════════════
def _dup_body(extra: str = "") -> str:
    """280 汉字的正文，够上 DUP_MIN_CHARS=200 的查重门槛。"""
    return "第一章 开端\r\n\r\n" + "\r\n\r\n".join([BODY] * 10) + ("\r\n\r\n" + extra if extra else "")


def test_duplicate_chapter_marked_C(library):
    b = library / "测试一本"
    w(b, "0100_第一章 开端.txt", _dup_body())
    w(b, "0101_第一章 开端.txt", _dup_body())
    per = [cnc.decide("测试一本", b / n) for n in ("0101_第一章 开端.txt", "0100_第一章 开端.txt")]
    assert cnc.mark_duplicates(per) == 1          # 传参顺序故意颠倒，验证按序号而非传入顺序保留
    assert per[1]["action"] == "none"             # 0100 序号小，保留
    assert per[0]["action"] == "C"                # 0101 隔离
    assert per[0]["dup_of"] == "0100_第一章 开端.txt"
    assert per[0]["rule"] == "duplicate"


def test_duplicate_detection_ignores_noise_difference(library):
    """同一章的两个抓取版本，一个带尾巴噪声一个不带，仍要认出来是重复。"""
    b = library / "测试一本"
    w(b, "0100_第一章 开端.txt", _dup_body())
    w(b, "0101_第一章 开端.txt", _dup_body("本章已完成！"))
    per = [cnc.decide("测试一本", b / n) for n in ("0100_第一章 开端.txt", "0101_第一章 开端.txt")]
    assert cnc.mark_duplicates(per) == 1


def test_short_body_is_not_fingerprinted(library):
    """正文不足 200 汉字不给查重指纹：空壳章/通知章清噪后天然同文，互判会误伤。"""
    b = library / "测试一本"
    for n in ("0100_甲.txt", "0101_乙.txt"):
        w(b, n, "今日无更\r\n\r\n" + BODY)
    per = [cnc.decide("测试一本", b / n) for n in ("0100_甲.txt", "0101_乙.txt")]
    assert all(not d["dup_key"] for d in per)
    assert cnc.mark_duplicates(per) == 0


def test_duplicates_are_book_scoped(library):
    """不同书的同内容文件不算书内重复（转载/同名章节各书自有）。"""
    for name in ("测试一本", "测试二本"):
        w(library / name, "0100_第一章 开端.txt", _dup_body())
    for name in ("测试一本", "测试二本"):
        per = [cnc.decide(name, library / name / "0100_第一章 开端.txt")]
        assert cnc.mark_duplicates(per) == 0


def test_apply_moves_duplicate_and_ledgers_C(library):
    b = library / "测试一本"
    keep = w(b, "0100_第一章 开端.txt", _dup_body())
    dup = w(b, "0101_第一章 开端.txt", _dup_body("本章已完成！"))
    cnc.OUT_DIR.mkdir(parents=True, exist_ok=True)
    per = [cnc.decide("测试一本", keep), cnc.decide("测试一本", dup)]
    cnc.mark_duplicates(per)
    cnc.DECISIONS.write_text(json.dumps(per, ensure_ascii=False), encoding="utf-8")
    assert cnc.cmd_apply(apply_args()) == 0
    assert keep.exists() and not dup.exists()
    assert (cnc.NOVEL_ROOT / "_quarantine" / "测试一本" / dup.name).exists()
    ce = [e for e in cnc.load_ledger() if e["action"] == "C"]
    assert len(ce) == 1 and ce[0]["dup_of"] == keep.name
