"""Phase 7.1 情节模板库：CRUD / beat 级切块 / 三层检索。

覆盖重点：
- **结构拍平与切块**：beat 是检索的粒度单位，拍平顺序与变体文本必须正确；
- **向量路径**：monkeypatch 假向量 + Brute store，验证 beat 级索引 → 检索 → RRF 聚合回模板；
- **multi-query**：显式多查询（"学院大比"+"秘境寻宝"）两路都能召回；
- **fallback**：向量不可用时关键词兜底（不硬报错）；标签过滤在两条路径下都必须生效。
"""
import pytest
from pydantic import ValidationError

from app.routers.plot_templates import TemplateSearch, TemplateUpsert
from app.services import plot_template_crud as tpl
from app.services import vector_index

DIM = 64


def _vec(kind: int) -> list[float]:
    v = [0.0] * DIM
    v[kind % DIM] = 1.0
    return v


def _mk_structure():
    return {
        "phases": [
            {"phase": "开局", "beats": [
                {"beat": "规则公布", "variants": [
                    {"src": "A书", "how": "赌约立局，先输一场"},
                    {"src": "B书", "how": "配角内战卷入"},
                ]},
            ]},
            {"phase": "高潮", "beats": [
                {"beat": "决战", "variants": [{"src": "C书", "how": "以巧破力"}]},
            ]},
        ]
    }


@pytest.fixture()
def tpl_a(test_db):
    return tpl.create(test_db, {
        "name": "学院大比", "scale": "arc",
        "genre_tags": ["玄幻", "校园"],
        "logline": "封闭赛场内的阶梯竞争",
        "structure": _mk_structure(),
        "source_stats": {"books": 3},
    })


def tpl_b_factory(test_db):
    return tpl.create(test_db, {
        "name": "秘境寻宝", "scale": "arc",
        "genre_tags": ["玄幻"],
        "logline": "受限空间内的探索与争夺",
        "structure": {"phases": [{"phase": "开局", "beats": [
            {"beat": "进入遗迹", "variants": [{"src": "D书", "how": "探宝团内讧"}]},
        ]}]},
    })


def _enable_vector(monkeypatch, kind_by_text: dict):
    """强制向量通道开启：可控假向量（按文本探测返回不同方向的向量）+ Brute store。"""
    from app.services.vector_store import BruteVectorStore
    monkeypatch.setattr(vector_index, "enabled", lambda db: True)
    monkeypatch.setattr(vector_index, "get_store", lambda db: BruteVectorStore())

    def fake_embed(texts, db=None):
        out = []
        for t in texts:
            kind = 99   # 默认方向（谁都不像）
            for keyword, probe_kind in kind_by_text.items():
                if keyword in t:
                    kind = probe_kind
                    break
            out.append(_vec(kind))
        return out

    monkeypatch.setattr(vector_index.embedding_client, "embed_texts", fake_embed)


# ---------------------------------------------------------------------------
# CRUD / 结构拍平
# ---------------------------------------------------------------------------
class TestCrud:
    def test_beats_flatten_order(self, test_db, tpl_a):
        beats = tpl.structure_beats(tpl_a)
        assert [b["beat"] for b in beats] == ["规则公布", "决战"]
        assert [b["phase"] for b in beats] == ["开局", "高潮"]
        assert beats[0]["variants"][0] == {"src": "A书", "how": "赌约立局，先输一场"}

    def test_beat_chunks_format(self, test_db, tpl_a):
        chunks = tpl.beat_chunks(tpl_a)
        assert len(chunks) == 2
        assert chunks[0].startswith("学院大比｜开局｜规则公布")
        assert "（A书）赌约立局" in chunks[0]
        assert "（C书）以巧破力" in chunks[1]

    def test_get_and_list_filters(self, test_db, tpl_a):
        assert tpl.get(test_db, tpl_a.id).name == "学院大比"
        assert len(tpl.list_templates(test_db, scale="arc")) == 1
        assert tpl.list_templates(test_db, scale="segment") == []
        assert len(tpl.list_templates(test_db, status="draft")) == 1

    def test_update_reindexes(self, test_db, tpl_a, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(tpl, "index_template", lambda db, t: calls.append(t.id) or 0)
        got = tpl.update(test_db, tpl_a.id, {"name": "学院大比·改", "status": "reviewed"})
        assert got.name == "学院大比·改" and got.status == "reviewed"
        assert calls == [tpl_a.id]   # 结构变更 → 必须重建向量

    def test_delete(self, test_db, tpl_a):
        assert tpl.delete(test_db, tpl_a.id) is True
        assert tpl.get(test_db, tpl_a.id) is None
        assert tpl.delete(test_db, tpl_a.id) is False


# ---------------------------------------------------------------------------
# 检索 · fallback（向量不可用）
# ---------------------------------------------------------------------------
class TestSearchFallback:
    def test_keyword_hit(self, test_db, tpl_a):
        r = tpl.search(test_db, query="学院")
        assert r["mode"] == "fallback_tags"
        assert r["items"] and r["items"][0]["name"] == "学院大比"
        assert any(b["beat"] == "规则公布" for b in r["items"][0]["matched_beats"])

    def test_keyword_miss(self, test_db, tpl_a):
        r = tpl.search(test_db, query="修仙炼丹")
        assert r["items"] == []

    def test_tag_filter_blocks(self, test_db, tpl_a):
        r = tpl.search(test_db, query="学院", tags=["都市"])
        assert r["items"] == []      # 标签不匹配 → 过滤掉

    def test_empty_query(self, test_db, tpl_a):
        r = tpl.search(test_db, query="")
        assert r["items"] == []


# ---------------------------------------------------------------------------
# 检索 · 向量路径
# ---------------------------------------------------------------------------
class TestSearchVector:
    def test_index_then_hit(self, test_db, tpl_a, monkeypatch):
        tpl_b = tpl_b_factory(test_db)
        _enable_vector(monkeypatch, {"学院": 0, "秘境": 1})
        tpl.index_template(test_db, tpl_a)   # create 阶段无 key 索引静默失败 → 此处补索引
        tpl.index_template(test_db, tpl_b)
        r = tpl.search(test_db, query="学院大比")
        assert r["mode"] == "vector"
        assert r["items"], "应至少召回一个模板"
        assert r["items"][0]["name"] == "学院大比"
        assert tpl_b.id not in [i["id"] for i in r["items"]]   # 方向不同，不应被排前

    def test_multi_query_rrf(self, test_db, tpl_a, monkeypatch):
        """显式多查询：两个方向各查一路，两个模板都应召回（混合口述场景）。"""
        tpl_b = tpl_b_factory(test_db)
        _enable_vector(monkeypatch, {"学院": 0, "秘境": 1})
        tpl.index_template(test_db, tpl_a)
        tpl.index_template(test_db, tpl_b)
        r = tpl.search(test_db, query="", queries=["学院大比", "秘境寻宝"])
        names = [i["name"] for i in r["items"]]
        assert "学院大比" in names and "秘境寻宝" in names

    def test_matched_beats_carry_variants(self, test_db, tpl_a, monkeypatch):
        _enable_vector(monkeypatch, {"学院": 0, "秘境": 1})
        tpl.index_template(test_db, tpl_a)
        r = tpl.search(test_db, query="学院大比")
        beats = r["items"][0]["matched_beats"]
        assert beats and any(
            v["src"] == "A书" for b in beats for v in b["variants"]
        )

    def test_tag_filter_with_vector(self, test_db, tpl_a, monkeypatch):
        tpl_b = tpl_b_factory(test_db)
        _enable_vector(monkeypatch, {"学院": 0, "秘境": 1})
        tpl.index_template(test_db, tpl_a)
        tpl.index_template(test_db, tpl_b)
        r = tpl.search(test_db, query="学院大比", tags=["都市"])
        assert r["items"] == []      # 向量召回后标签闸门仍然生效

    def test_scale_filter_with_vector(self, test_db, tpl_a, monkeypatch):
        _enable_vector(monkeypatch, {"学院": 0, "秘境": 1})
        r = tpl.search(test_db, query="学院大比", scale="segment")
        assert r["items"] == []      # arc 模板不冒充 segment


class TestScaleWhitelist:
    """DEV-F9c（2026-10-03）：路由层 scale 白名单必须含 character。

    角色模板与骨架同表同端点，模板库页详情抽屉保存是**整条 PUT**（body 带 scale=character），
    白名单漏了它 → 改个状态也 422；检索端点的 scale 过滤同理（粒度下拉可选角色模板）。
    """

    @pytest.mark.parametrize("scale", ["arc", "segment", "character"])
    def test_three_scales_accepted(self, scale):
        assert TemplateUpsert(name="蛮横抢宝型首领", scale=scale).scale == scale
        assert TemplateSearch(query="夺宝", scale=scale).scale == scale

    def test_default_is_arc(self):
        assert TemplateUpsert(name="秘境夺宝").scale == "arc"

    def test_bogus_scale_rejected(self):
        with pytest.raises(ValidationError):
            TemplateUpsert(name="x", scale="novel")
