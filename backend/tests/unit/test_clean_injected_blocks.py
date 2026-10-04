# -*- coding: utf-8 -*-
"""[DEV-DATA01b] 他书注入块清洗 / 整章隔离 / 文件名重命名 / 缺陷登记 的单测。

素材全部用 tmp_path 里的假书目录，**绝不碰 E:\\AI小说创作\\小说\\**。
判据常量的取证来源见 backend/scripts/chapter_noise_patterns.py 的 DATA01b 段。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

import scripts.clean_injected_blocks as cib
from chapter_noise_patterns import match_inject_ad_line  # type: ignore  # noqa: E402

BODY_A = "金锋把地图摊在案上，指尖点着江文文绘出来的那条水路。"
BODY_B = "镇远镖局的船排在船坞里，一盏一盏灯顺过去，看不到头。"
AD_AIYUE_1 = "网页版章节内容慢，请下载爱阅app最新内容"
AD_AIYUE_2 = "网站即将关闭，下载爱阅app免费看最新内容"
AD_AIYUE_3 = "请退出转码页面，请下载爱阅app 最新章节。"
AD_XING_1 = "想要看最新章节内容，请下载星星app，无广告免费最新章节内容。"
AD_XING_2 = "网站已经不更新最新章节内容，已经星星APP更新最新章节内容。"
AD_XING_3 = "下载星星app，最新章节内容无广告免费"
AD_XING_4 = "下载星星app为您提供大神北川的寒门枭士"
AD_TRAD = "丅載愛閱曉詤app"
AD_CHUAN = "小说阅读下尽在穿越小说吧，更新超快，小说更多。 /"
AD_ADDR_1 = "由于各种问题地址更改为请大家收藏新地址避免迷路"
AD_ADDR_2 = "新为你提供最快的寒门枭士更新，免费。"
ZMS_1 = "沈长青走在路上，有遇到相熟的人，彼此都会打个招呼，或是点头。"
ZMS_2 = "因为这里是镇魔司，乃是维护大秦稳定的一个机构，主要的职责就是斩杀妖魔诡怪。"
ZMS_3 = "其中镇魔司一共分为两个职业，一为镇守使，一为除魔使。"
YU_1 = "无尽的昏迷过后，时宇猛地从床上起身。"
YU_2 = "时宇拿起一看，书名瞬间让他沉默。《新手饲养员必备育兽手册》《宠兽产后的护理》。"
BW_1 = "“喂，萧琰吗？”"
BW_2 = "想他萧琰，戎马十载，歼敌百万余众，年仅二十七岁便以无敌之态问鼎至尊之位，封号镇国！"
TD_1 = "“天道图书馆，是我一道意念所化，是根基，也是桎梏，你能靠自己的能力突破桎梏。”"
TD_2 = "看到眼前这位如此不靠谱的老爹，面皮一抽，张悬只好答应：“好吧……”"


def chapter(title: str, *paras: str) -> str:
    return "\n\n".join([title, *paras])


# ───────────────────────── 广告行判定 ─────────────────────────
@pytest.mark.parametrize("line", [AD_AIYUE_1, AD_AIYUE_2, AD_AIYUE_3, AD_TRAD,
                                  AD_XING_1, AD_XING_2, AD_XING_3, AD_XING_4, AD_CHUAN,
                                  AD_ADDR_1, AD_ADDR_2])
def test_ad_line_positive(line):
    assert match_inject_ad_line(line)


@pytest.mark.parametrize("line", [
    BODY_A,
    "他掏出手机，拨通了爱阅社的电话，那边迟迟没人接。",
    "金锋道：“这网站上的星星，画得倒是细致。”",
    "地址更改的消息传到金川，商人们连夜改了路引。",
])
def test_ad_line_negative(line):
    assert match_inject_ad_line(line) is None


def test_ad_line_rejects_long_line_even_with_keyword():
    line = BODY_A + "网页版章节内容慢，请下载爱阅app最新内容" * 2
    assert match_inject_ad_line(line) is None      # 长行不整行删，防砍正文


# ───────────────────────── 注入块判定 ─────────────────────────
def test_find_injection_cuts_at_first_ad_line():
    text = chapter("第2138章 盛世如愿", BODY_A, BODY_B, "全书完",
                   AD_AIYUE_1, ZMS_1, AD_AIYUE_2, ZMS_2, ZMS_3)
    inj = cib.find_injection(text, "寒门枭士")
    assert inj and inj["cut_reason"] == "ad_anchor"
    assert inj["cut_line"] == AD_AIYUE_1           # 切点 = 最早的广告行
    assert inj["cut_frac"] > 0.25                  # 属尾部，走 A 不走 B


def test_find_injection_cuts_at_marker_when_no_ad_line():
    text = chapter("第832章 封锁船坞", BODY_A, BODY_B, BW_1, BW_2)
    inj = cib.find_injection(text, "寒门枭士")
    assert inj and inj["cut_reason"] == "bingwang_xiaoyan"
    assert inj["cut_line"] == BW_1


def test_single_marker_line_is_not_enough():
    """`当时宇文家` 这种子串撞车（实测 c1519）：同块只命中 1 行 → 不判注入。"""
    text = chapter("第1519章 安置难民",
                   "其实当时宇文家和周家的细作发现九公主乘坐飞艇离开船坞，就把消息传了回去。",
                   BODY_A, BODY_B)
    assert cib.find_injection(text, "寒门枭士") is None


def test_find_injection_none_for_clean_chapter():
    assert cib.find_injection(chapter("第1章 起", BODY_A, BODY_B), "寒门枭士") is None


def test_blocks_are_scoped_by_book():
    """斗破没有登记块，同样文字在斗破里不判注入（跨书误伤防线）。"""
    text = chapter("第一千章 x", BODY_A, ZMS_1, ZMS_2, ZMS_3)
    assert cib.find_injection(text, "寒门枭士")
    assert cib.find_injection(text, "斗破苍穹") is None


# ───────────────────────── decide_injection 三档 ─────────────────────────
def write_chapter(book_dir: Path, seq: int, title: str, paras) -> Path:
    p = book_dir / f"{seq:04d}_{title}.txt"
    p.write_text(chapter(title, *paras), encoding="utf-8")
    return p


@pytest.fixture()
def library(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    novel = root / "小说"
    for b in ("寒门枭士", "斗破苍穹", "凡人修仙传"):
        (novel / b).mkdir(parents=True)
    monkeypatch.setattr(cib, "ROOT", root)
    monkeypatch.setattr(cib, "NOVEL_ROOT", novel)
    monkeypatch.setattr(cib, "OUT_DIR", root / "outputs" / "data01b")
    monkeypatch.setattr(cib, "BACKUP_DIR", root / "outputs" / "data01b" / "backup")
    monkeypatch.setattr(cib, "LEDGER", root / "outputs" / "data01b" / "清洗台账_二批.json")
    monkeypatch.setattr(cib, "DECISIONS", root / "outputs" / "data01b" / "决策明细.json")
    monkeypatch.setattr(cib, "REGISTRY", root / "outputs" / "data01b" / "缺陷登记.json")
    return novel


def test_decide_tail_block_is_A(library):
    p = write_chapter(library / "寒门枭士", 1929, "第1929章 处罚决定",
                      [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    d = cib.decide_injection("寒门枭士", p)
    assert d["action"] == "A" and d["rule"] == "injected_tail_block"


def test_decide_ad_only_is_A_without_cut(library):
    p = write_chapter(library / "寒门枭士", 1068, "第1068章 黑侠",
                      [BODY_A, BODY_B, AD_AIYUE_1, AD_AIYUE_3])
    d = cib.decide_injection("寒门枭士", p)
    assert d["action"] == "A" and d["rule"] == "ad_line_only" and d["cut_idx"] is None


def test_ad_only_with_long_body_tail_still_not_cut(library):
    """只有广告行时尾巴再长也不切段——那是正文，砍了就是丢正文。"""
    tail = [f"金锋把第{i}批水泥送进仓库，验收的吏员一笔一笔记在册子上，谁也不敢马虎。" for i in range(6)]
    p = write_chapter(library / "寒门枭士", 1070, "第1070章 长尾",
                      [BODY_A, AD_AIYUE_1] + tail)
    d = cib.decide_injection("寒门枭士", p)
    assert d["rule"] == "ad_line_only" and d["cut_idx"] is None
    new, _ = cib.clean_injection(p.read_text(encoding="utf-8"), d["cut_idx"])
    assert all(t in new for t in tail)


def test_block_evidence_cuts_even_with_short_tail(library):
    """块证据（≥2 行异质专名）够硬，尾巴不到 150 字也照切——这正是核账要归零的那类残留。"""
    p = write_chapter(library / "寒门枭士", 1929, "第1929章 处罚决定",
                      [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    d = cib.decide_injection("寒门枭士", p)
    assert d["rule"] == "injected_tail_block" and d["cut_idx"] is not None


def test_decide_whole_chapter_foreign_is_B(library):
    """整章是他书正文（实测 c1372：切点在第 2 行、前面只剩 35 汉字）→ 隔离不切尾。"""
    p = write_chapter(library / "寒门枭士", 1372, "第1372章 夫妻夜谈（五）",
                      [TD_1, TD_2, "神界崩塌之前，张悬终于突破了帝君的桎梏，洛若曦在一旁看着。",
                       "不死帝君小黄鸡站在云端，看着自己女儿的归宿，欣慰地点了点头。",
                       ZMS_2, BODY_A, BODY_B])
    d = cib.decide_injection("寒门枭士", p)
    assert d["action"] == "B" and d["rule"] == "whole_chapter_foreign"
    assert d["body_cjk_before"] < 200


def test_short_tail_block_below_threshold_is_left(library):
    """注入块尾巴不足 150 汉字 → 本单不动（避免碰巧两行就砍）。"""
    p = write_chapter(library / "寒门枭士", 1500, "第1500章 短句", [BODY_A, BODY_B, ZMS_1])
    d = cib.decide_injection("寒门枭士", p)
    assert d["action"] == "none"


# ───────────────────────── 清理算法 ─────────────────────────
def test_clean_injection_keeps_body_and_drops_tail():
    text = chapter("第1929章 处罚决定", BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2)
    inj = cib.find_injection(text, "寒门枭士")
    new, hits = cib.clean_injection(text, inj["cut_idx"])
    assert BODY_A in new and BODY_B in new and "第1929章" in new
    assert ZMS_1 not in new and AD_AIYUE_1 not in new
    assert "injected_tail_block" in hits


def test_clean_injection_ad_only_removes_just_those_lines():
    text = chapter("第1068章 黑侠", BODY_A, AD_AIYUE_1, BODY_B, AD_AIYUE_3)
    new, hits = cib.clean_injection(text, None)
    assert new.split() and BODY_A in new and BODY_B in new
    assert AD_AIYUE_1 not in new and AD_AIYUE_3 not in new
    assert set(hits) == {"aiyue_ad"}


def test_clean_injection_is_idempotent():
    text = chapter("第1929章 处罚决定", BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2)
    inj = cib.find_injection(text, "寒门枭士")
    once, _ = cib.clean_injection(text, inj["cut_idx"])
    twice, hits = cib.clean_injection(once, None)
    assert twice == once and hits == []


def test_crlf_file_keeps_crlf_after_tail_cut():
    text = BODY_A + "\r\n\r\n" + BODY_B + "\r\n\r\n" + AD_AIYUE_1 + "\r\n\r\n" + ZMS_1
    inj = cib.find_injection(text, "寒门枭士")
    new, _ = cib.clean_injection(text, inj["cut_idx"])
    assert ZMS_1 not in new and AD_AIYUE_1 not in new
    assert not re.search(r"(?<!\r)\n", new)      # 行尾仍是 CRLF，没混进裸 LF
    assert BODY_A in new and BODY_B in new


def test_no_trailing_newline_is_preserved():
    text = BODY_A + "\n\n" + BODY_B            # 实测 90% 章节文件结尾无换行
    new, hits = cib.clean_injection(text, None)
    assert new == text and hits == []


def test_untouched_region_blank_lines_not_collapsed():
    text = BODY_A + "\n\n\n" + BODY_B + "\n\n" + AD_AIYUE_1 + "\n\n" + ZMS_1 + "\n" + ZMS_2
    inj = cib.find_injection(text, "寒门枭士")
    new, _ = cib.clean_injection(text, inj["cut_idx"])
    assert BODY_A + "\n\n\n" + BODY_B in new      # 三连续空行在正文区，一个都不该动


# ───────────────────────── 文件名控制字符（R）─────────────────────────
def test_rename_drops_control_char_and_keeps_seq_prefix(library):
    weird = "0881_第八百五十九章 \x7f 到来.txt"
    p = library / "斗破苍穹" / weird
    p.write_text(chapter("第八百五十九章 到来", BODY_A, BODY_B), encoding="utf-8")
    d = cib.decide_rename("斗破苍穹", p)
    assert d and d["action"] == "R"
    assert d["new_file"].startswith("0881_") and "\x7f" not in d["new_file"]
    assert "  " not in d["new_file"]           # 空白折叠，不留两个空格


def test_rename_none_for_clean_name(library):
    p = write_chapter(library / "斗破苍穹", 100, "第一千章 干净", [BODY_A])
    assert cib.decide_rename("斗破苍穹", p) is None


# ───────────────────────── 同人续写（B）─────────────────────────
def test_fan_sequel_detected_by_numbering_restart(library):
    p = write_chapter(library / "斗破苍穹", 1659, "第二章 新世界",
                      ["注明：本小说根据土豆的五帝破空开始写的，思路也会沿着土豆的思路走。",
                       "玄幻界里边有古老的古阵，斗帝死亡一半来源于此地。", AD_CHUAN])
    d = cib.decide_nonbody("斗破苍穹", p, p.read_text(encoding="utf-8"))
    assert d and d["action"] == "B" and d["rule"] == "fan_sequel_nonbody"


def test_normal_offset_chapter_is_not_fan_sequel(library):
    """斗破正文章号偏移实测 -22~-27，不能误判成续写。"""
    p = write_chapter(library / "斗破苍穹", 841, "第八百一十九章 拍卖干尸",
                      ["干尸一出现，紫研便觉得不太舒服。", BODY_B])
    assert cib.decide_nonbody("斗破苍穹", p, p.read_text(encoding="utf-8")) is None


def test_fan_sequel_rule_scoped_to_doupo(library):
    p = write_chapter(library / "凡人修仙传", 1659, "第二章 新世界",
                      ["注明：本小说根据土豆的五帝破空开始写的。", BODY_B])
    assert cib.decide_nonbody("凡人修仙传", p, p.read_text(encoding="utf-8")) is None


def test_fan_sequel_low_number_not_touched(library):
    """前缀 <1600 一律不判：续写在书尾，正文开头本就该有小数字章号。"""
    p = write_chapter(library / "斗破苍穹", 3, "第三章 起", [BODY_A, BODY_B])
    assert cib.decide_nonbody("斗破苍穹", p, p.read_text(encoding="utf-8")) is None


# ───────────────────────── 截断分档 ─────────────────────────
@pytest.mark.parametrize("last,expect", [
    ("江文文介绍道：“过了这个堡垒，就正式进", "truncated"),   # 引号未合（实测 c1616）
    ("铁锤自", "truncated"),                                   # 残块（实测 c1617）
    ("结果被打击得体无完肤，", "truncated"),                    # 逗号收尾
    ("金锋点点头，心里同情铁锤三秒钟", "no_mark"),               # 只是没打句号
    ("那一战，伏尸百万！", "clean"),
    ("全书完", "clean"),
    ("今天卡文，明早补上，大家晚安", "author_talk"),
])
def test_classify_truncation_tiers(last, expect):
    text = chapter("第1章 x", BODY_A) + "\n\n" + last
    assert cib.classify_truncation(text)[0] == expect


# ───────────────────────── 偏移与重复登记 ─────────────────────────
def test_offset_distribution_counts_inner_vs_prefix(library):
    d = library / "斗破苍穹"
    write_chapter(d, 841, "第八百一十九章 拍卖干尸", ["第八百一十九章 拍卖干尸", BODY_A])
    write_chapter(d, 842, "第八百二十章 出关", ["第八百二十章 出关", BODY_A])
    texts = cib.load_texts(d)
    dist = cib.offset_distribution("斗破苍穹", texts)
    assert dist["-22"] == 2                                  # 实测系统性偏移 -22


def test_duplicate_report_flags_same_chapter_pair(library):
    d = library / "寒门枭士"
    long_para = "安保队员杀入猪笼寨的时候采取的是镇远镖局经典的三角阵型身强体壮力气大的队员穿着盔甲顶在前面"
    write_chapter(d, 1956, "第1956章 开仓运粮", [BODY_A, long_para, "“各位大哥，成败就在今天了！”"])
    write_chapter(d, 1957, "第1957章 积极应对", [BODY_A, long_para, "“各位大哥，成败就在今天了！”"])
    rep = cib.duplicate_report(cib.load_texts(d))
    assert rep["相邻章整章同文"] and rep["相邻章整章同文"][0]["prev"] == 1956


def test_duplicate_report_ignores_short_lines(library):
    d = library / "寒门枭士"
    write_chapter(d, 100, "第100章 甲", [BODY_A, "他走了。"])
    write_chapter(d, 101, "第101章 乙", [BODY_B, "他走了。"])
    assert cib.duplicate_report(cib.load_texts(d))["相邻章整章同文"] == []


# ───────────────────────── apply / 台账 / 核账 ─────────────────────────
def _args(books="寒门枭士,斗破苍穹,凡人修仙传"):
    import argparse
    return argparse.Namespace(books=books, force=True, decisions=str(cib.DECISIONS))


def test_apply_cut_and_quarantine_and_rename_with_ledger(library):
    hm = library / "寒门枭士"
    write_chapter(hm, 1929, "第1929章 处罚决定", [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    p_b = write_chapter(hm, 1372, "第1372章 夫妻夜谈（五）",
                        [TD_1, TD_2, "神界崩塌之前，张悬终于突破了帝君的桎梏，洛若曦在一旁看着。",
                         "不死帝君小黄鸡站在云端，欣慰地点了点头。", BODY_A, BODY_B])
    dp = library / "斗破苍穹"
    p_r = dp / "0881_第八百五十九章 \x7f 到来.txt"
    p_r.write_text(chapter("第八百五十九章 到来", BODY_A, BODY_B), encoding="utf-8")

    plan, _reg, _ov = cib.build_plan(cib.SCOPE_BOOKS)
    cib.apply_plan(plan)
    led = cib.load_ledger()
    assert {e["action"] for e in led} == {"A", "B", "R"}

    a = next(e for e in led if e["action"] == "A")
    after = (hm / "1929_第1929章 处罚决定.txt").read_text(encoding="utf-8")
    assert ZMS_1 not in after and AD_AIYUE_1 not in after and BODY_A in after
    assert (Path(cib.ROOT) / a["backup"]).exists()          # 备份先行
    assert (Path(cib.ROOT) / a["backup"]).read_text(encoding="utf-8").count(ZMS_1) == 1

    b = next(e for e in led if e["action"] == "B")
    assert not (hm / p_b.name).exists()
    assert (Path(cib.ROOT) / b["new_rel"]).exists()          # 隔离不删除

    r = next(e for e in led if e["action"] == "R")
    assert not (dp / p_r.name).exists()
    assert (dp / r["new_file"]).exists() and "\x7f" not in r["new_file"]


def test_apply_is_idempotent(library):
    hm = library / "寒门枭士"
    write_chapter(hm, 1929, "第1929章 处罚决定", [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    plan, _r, _o = cib.build_plan(cib.SCOPE_BOOKS)
    cib.apply_plan(plan)
    size1 = (hm / "1929_第1929章 处罚决定.txt").stat().st_size
    plan2, _r2, _o2 = cib.build_plan(cib.SCOPE_BOOKS)
    cib.apply_plan(plan2)
    assert size1 == (hm / "1929_第1929章 处罚决定.txt").stat().st_size
    assert plan2 == []
    assert len(cib.load_ledger()) == 1                       # 台账不重复记


def test_rescan_books_zero_after_apply(library):
    hm = library / "寒门枭士"
    write_chapter(hm, 1929, "第1929章 处罚决定", [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    assert cib.rescan_books(cib.SCOPE_BOOKS)["left"], "处置前应有命中"
    plan, _r, _o = cib.build_plan(cib.SCOPE_BOOKS)
    cib.apply_plan(plan)
    assert cib.rescan_books(cib.SCOPE_BOOKS)["left"] == Counter()


def test_rollback_restores_all_three_actions(library):
    hm = library / "寒门枭士"
    p_a = write_chapter(hm, 1929, "第1929章 处罚决定",
                        [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    original = p_a.read_text(encoding="utf-8")
    p_b = write_chapter(hm, 1372, "第1372章 夫妻夜谈（五）",
                        [TD_1, TD_2, "神界崩塌之前，张悬终于突破了帝君的桎梏，洛若曦在一旁看着。",
                         "不死帝君小黄鸡站在云端，欣慰地点了点头。", BODY_A, BODY_B])
    dp = library / "斗破苍穹"
    p_r = dp / "0881_第八百五十九章 \x7f 到来.txt"
    p_r.write_text(chapter("第八百五十九章 到来", BODY_A, BODY_B), encoding="utf-8")

    plan, _r, _o = cib.build_plan(cib.SCOPE_BOOKS)
    cib.apply_plan(plan)
    assert p_a.read_text(encoding="utf-8") != original
    assert not p_b.exists() and not p_r.exists()

    import argparse
    assert cib.cmd_rollback(argparse.Namespace(force=True)) == 0
    assert p_a.read_text(encoding="utf-8") == original      # A 类原文逐字还原
    assert p_b.exists() and p_b.read_text(encoding="utf-8")  # B 类从隔离区回到原位
    assert p_r.exists() and "\x7f" in p_r.name              # R 类文件名还原
    assert not (cib.NOVEL_ROOT / "_quarantine" / "寒门枭士" / p_b.name).exists()


def test_dry_run_writes_decisions_and_registry(library, monkeypatch):
    hm = library / "寒门枭士"
    write_chapter(hm, 1929, "第1929章 处罚决定", [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    assert cib.cmd_dry_run(_args()) == 0
    plan = json.loads(cib.DECISIONS.read_text(encoding="utf-8"))
    reg = json.loads(cib.REGISTRY.read_text(encoding="utf-8"))
    assert [Path(d["rel"]).name for d in plan] == ["1929_第1929章 处罚决定.txt"]
    assert reg["寒门枭士"]["文件数"] == 1
    assert (cib.OUT_DIR / "dry-run报告.md").exists()


def test_verify_passes_after_apply(library, monkeypatch, capsys):
    hm = library / "寒门枭士"
    write_chapter(hm, 1929, "第1929章 处罚决定", [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    plan, _r, _o = cib.build_plan(cib.SCOPE_BOOKS)
    cib.apply_plan(plan)
    assert cib.cmd_verify(_args()) == 0
    out = capsys.readouterr().out
    assert "核账通过" in out


def test_verify_fails_when_residue_left(library, capsys):
    hm = library / "寒门枭士"
    write_chapter(hm, 2000, "第2000章 残留", [BODY_A, BODY_B, AD_AIYUE_1, ZMS_1, ZMS_2, ZMS_3])
    (hm / "2000_第2000章 残留.txt").write_text(
        chapter("第2000章 残留", BODY_A, BODY_B, ZMS_1, ZMS_2, ZMS_3, ZMS_1 + ZMS_2 + ZMS_3),
        encoding="utf-8")
    assert cib.cmd_verify(_args()) == 1
    assert "仍剩" in capsys.readouterr().out
