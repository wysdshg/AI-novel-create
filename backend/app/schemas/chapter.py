"""章节生成与剧情商讨（需求 2、3、6）。"""
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime


class GenerateRequest(BaseModel):
    model_config = {"protected_namespaces": ()}  # 允许 model_id 字段，消除 Pydantic 命名空间冲突警告

    chapter_no: int = Field(..., ge=1)
    prompt_hint: Optional[str] = None
    from_discussion: bool = True
    trigger_foreshadow_ids: List[str] = []
    word_range: dict = Field(default_factory=lambda: {"min": 3000, "max": 5000})
    temperature: float = Field(0.4, ge=0, le=1)
    enable_thinking: Optional[bool] = None
    article_id: Optional[str] = None          # 4 级结构：生成章节时必属某篇（可空兼容旧调用）
    model_id: Optional[str] = None            # 前端指定模型 ID；不传则 fallback 到默认模型
    title: Optional[str] = None               # 用户填写的章节标题；为空则后端按序号兜底
    chapter_id: Optional[str] = None          # 重新生成目标章节 ID（非空=覆盖该章，不新建）
    thread_chapter_id: Optional[str] = None   # 当前所在对话线程（章）；用于打包商讨 + 走向建议归位
    thread_conversation_id: Optional[str] = None  # 当前所在对话线程（会话）；同上
    ingest_level: Optional[str] = None        # 写后摄取档位：full(默认) / lite(跳过概览聚合) / none(全跳)
                                              # 仅给测试脚本多轮验证省调用用；不传=读全局配置，行为不变


class ChapterBase(BaseModel):
    chapter_no: int
    title: Optional[str] = None
    content: str = ""
    note: Optional[str] = None                # 商讨归档备注
    word_count: int = 0
    article_id: Optional[str] = None          # 隶属篇（article）；4 级结构下一章必属于一篇


class ChapterCreate(ChapterBase):
    pass


class ChapterUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    note: Optional[str] = None
    word_count: Optional[int] = None


class Chapter(ChapterBase):
    id: str
    created_at: datetime
    updated_at: datetime


# ---------- 剧情商讨消息（需求 6） ----------
class DiscussionMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class DiscussionMessageCreate(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str
    meta: Optional[dict] = None
    conversation_id: Optional[str] = None


class DiscussionChatRequest(BaseModel):
    """剧情商讨/全局对话：前端传完整对话历史 + 可选模型 ID，后端用指定或默认模型流式回复。"""
    model_config = {"protected_namespaces": ()}

    messages: List[dict] = Field(default_factory=list)  # [{role:'user'|'assistant', content}]
    enable_thinking: Optional[bool] = None
    temperature: Optional[float] = None
    conversation_id: Optional[str] = None
    model_id: Optional[str] = None  # 前端指定模型 ID；不传则 fallback 到默认模型


class PolishRequest(BaseModel):
    """段落写法打磨（2026-09-21）：一次请求打包全部选中段 + 各自 top-5 参考。

    para_ids: 1-based 段号列表；None/空 = 全选。
    provider: 优化用哪个模型（ms=魔搭 Qwen3.8-Flash-Next / ds=DeepSeek）。
    thinking: 魔搭开思考（🔴 更慢约 8 倍，但措辞更稳；开思考时后端自动放大 max_tokens/timeout）。
    index_db: 块向量库路径；不传用默认数据目录（C:\\Users\\w3013\\.ai_novel\\para_ref.db）。
    """
    model_config = {"protected_namespaces": ()}

    para_ids: Optional[List[int]] = None
    provider: str = "ms"
    thinking: bool = True
    index_db: Optional[str] = None
    top_k: int = 5
