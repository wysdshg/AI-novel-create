"""ORM 数据模型（对应 API接口规范.md 各实体）。

声明基类 Base 由 core.database 统一提供，确保建表元数据唯一。
分作品隔离（§1）：所有业务实体含 project_id 字段。
"""
from datetime import datetime
from sqlalchemy import (
    String, Integer, Text, Boolean, DateTime, Float, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProjectORM(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    genre: Mapped[str | None] = mapped_column(String(40), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    db_backend: Mapped[str] = mapped_column(String(20), default="sqlite")
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    # 本小说选中的全局设定库 ID 列表（引用 SettingORM.id）
    # 空列表 = 不注入任何设定；null/缺失 = 全量注入（向后兼容旧数据）
    setting_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CharacterORM(Base):
    __tablename__ = "characters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(80))
    role_type: Mapped[str | None] = mapped_column(String(20), nullable=True)   # 主角/配角/反派
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)      # 男/女/其他
    personality: Mapped[str | None] = mapped_column(Text, nullable=True)       # 性格
    background: Mapped[str | None] = mapped_column(Text, nullable=True)        # 背景
    talent: Mapped[str | None] = mapped_column(Text, nullable=True)            # 天赋
    current_level: Mapped[str | None] = mapped_column(String(40), nullable=True)  # 当前等级
    skills: Mapped[list] = mapped_column(JSON, default=list)                   # 技能
    relationship_network: Mapped[list] = mapped_column(JSON, default=list)     # 关系网
    # ---- 关系网可视化布局坐标（SVG 画布坐标系，可拖拽持久化）----
    network_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    network_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)             # 简介
    # ---- 角色状态（Phase 7.3 ③ 角色状态门，2026-09-13）----
    # 🔴 **派生 vs 抽取要分工**（7.3.5 定稿，这里先把字段备齐）：
    #   `last_seen_chapter` / `appearance_count` 是**纯派生** —— 扫 `chapter_memories.characters`
    #     统计即可，可重算可验证（由 `casting_crud.refresh_character_appearances` 刷）。
    #     **绝不能交给 LLM**：统计问题是确定性任务，模型只会引入漂移。
    #   `status` 等必须**抽取 + 人工可改**："最后出场"≠"死了"（可能是闭关/失踪/退居幕后），
    #     纯统计推不出来，但模型也会猜错 → 所以给作者留改的口子。
    # 四态语义（7.3.5）：alive 在世 / dormant 蛰伏 / departed 离场 / dead 已死。
    # `dead` 默认不进选角候选池 —— 没有这一条，casting 会系统性产出"第 60 章用第 3 章选的
    # 已在 40 章前死掉的角色"这类连续性错误。
    status: Mapped[str] = mapped_column(String(20), default="alive", index=True)
    last_seen_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    appearance_count: Mapped[int] = mapped_column(Integer, default=0)
    status_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)   # 哪一章哪句话（可回溯）
    # explicit=有具体离场事件 / silent=只是后续没再写 / unknown
    # 为什么必须区分：silent 退场的人回归几乎不需要理由（他就是没被写），
    # explicit 失踪/死亡的人回归**必须交代** —— 没有这个区分，"编回归理由"无从下手。
    disappear_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    disappear_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SkillORM(Base):
    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(80))
    level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    limitation: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    side_effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    unlock_condition: Mapped[str | None] = mapped_column(Text, nullable=True)


class RelationORM(Base):
    __tablename__ = "relations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    subject_id: Mapped[str] = mapped_column(String(36))
    object_id: Mapped[str] = mapped_column(String(36))
    relation_type: Mapped[str] = mapped_column(String(20))
    strength: Mapped[int] = mapped_column(Integer, default=50)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class FactionORM(Base):
    __tablename__ = "factions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)      # 相关描述
    leader_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    members: Mapped[list] = mapped_column(JSON, default=list)                # 核心成员（名称或角色ID）
    territory: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 势力范围
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocationORM(Base):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(80))
    location_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 城市/秘境/洞府/门派/其他
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)         # 所属区域
    description: Mapped[str | None] = mapped_column(Text, nullable=True)          # 描述
    notable_features: Mapped[list] = mapped_column(JSON, default=list)            # 特色/地标
    related_ids: Mapped[list] = mapped_column(JSON, default=list)                 # 关联角色/势力
    # ---- 空间几何建模（统一世界坐标系 x/y + 位面 plane + 高度 height）----
    plane: Mapped[str | None] = mapped_column(String(40), nullable=True)          # 位面/地图标签：凡间/仙界/地狱/秘境/自定义
    center_x: Mapped[float | None] = mapped_column(Float, nullable=True)          # 世界坐标系横坐标
    center_y: Mapped[float | None] = mapped_column(Float, nullable=True)          # 世界坐标系纵坐标
    shape: Mapped[str | None] = mapped_column(String(20), nullable=True)          # point/circle/rect/sector/polygon
    radius: Mapped[float | None] = mapped_column(Float, nullable=True)            # 圆/扇形半径，或矩形半宽
    radius_y: Mapped[float | None] = mapped_column(Float, nullable=True)          # 矩形半高
    angle: Mapped[float | None] = mapped_column(Float, nullable=True)             # 扇形中心朝向（度）
    angle_span: Mapped[float | None] = mapped_column(Float, nullable=True)        # 扇形张角（度）
    height: Mapped[float | None] = mapped_column(Float, nullable=True)            # 高度/海拔（第三维）
    polygon: Mapped[list | None] = mapped_column(JSON, nullable=True)             # 不规则多边形顶点 [[x,y],...]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class ForeshadowORM(Base):
    __tablename__ = "foreshadows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    description: Mapped[str] = mapped_column(Text)
    buried_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scene: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    activated_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    related_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")


class ChapterORM(Base):
    __tablename__ = "chapters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    article_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # 隶属篇（article）；迁移期可空
    chapter_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VolumeORM(Base):
    """卷：小说 / 卷 / 篇 / 章 4 级结构中的第 2 级。
    每卷归属于一个 project；其下挂多「篇(article)」。
    sort_order 用于侧栏树的展示顺序（升序）。
    """
    __tablename__ = "volumes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(120))                  # 例如「第一卷 山野游侠」
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # AI 生成的卷概览
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArticleORM(Base):
    """篇：4 级结构中的第 3 级，隶属卷；其下挂多「章(chapter)」。
    summary 是 AI 生成的篇概览；一个篇共享一个「篇章参考文档」（第 3 批落地）。
    """
    __tablename__ = "articles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    volume_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)  # 冗余，便于整本查询
    name: Mapped[str] = mapped_column(String(120))                  # 例如「第一篇 踏入仙途」
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # AI 生成的篇概览
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DiscussionMessageORM(Base):
    __tablename__ = "discussion_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    chapter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # 非空=章级商讨线程
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # 非空=会话级独立线程
    role: Mapped[str] = mapped_column(String(10))            # user / assistant
    content: Mapped[str] = mapped_column(Text)
    # ---- 商讨缓存持久化增强字段 ----
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)        # 思考过程（开启思考时）
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)           # {enable_thinking, model} 等
    archived_chapter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # 非空=已归档到该章节
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DiscussionLoadLogORM(Base):
    """P0 观测表：记录每轮商讨对话「实际加载了哪些设定/参考文件」。

    用途（设定库加载机制演进的数据地基）：
      - 频率统计：哪些设定被高频 LOAD_SETTING → 后续做常驻/预载的候选；
      - 关联挖掘：同轮被一起加载的设定对 → 后续做 related_ids 关联边的依据；
      - 阈值校准：question + 是否加载，作为 BM25 自动注入的 golden set。
    仅观测、不参与任何业务逻辑；落库失败静默降级，绝不阻塞对话主流程。
    """
    __tablename__ = "discussion_load_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)   # __global__ = 全局对话
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    question: Mapped[str] = mapped_column(Text, default="")           # 最近一条 user 消息（截断）
    # 模型 Pass1 主动请求的 id（去重后）
    load_ref_ids: Mapped[list] = mapped_column(JSON, default=list)       # LOAD_REFS:<ids>
    load_setting_ids: Mapped[list] = mapped_column(JSON, default=list)   # LOAD_SETTING:<ids>
    # 实际注入结果
    ref_loaded: Mapped[bool] = mapped_column(Boolean, default=False)      # 参考文件真的取到了
    setting_loaded: Mapped[bool] = mapped_column(Boolean, default=False)  # 设定详情真的取到了
    # 流程分支标记
    short_circuited: Mapped[bool] = mapped_column(Boolean, default=False)  # Pass1 直接当答案，未二次调用
    pass1_failed: Mapped[bool] = mapped_column(Boolean, default=False)     # Pass1 失败/不可用，退回单次流式
    model_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ModelConfigORM(Base):
    __tablename__ = "model_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    vendor: Mapped[str] = mapped_column(String(20))
    api_base: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(255), default="")
    model_name: Mapped[str] = mapped_column(String(80))
    context_window: Mapped[int] = mapped_column(Integer, default=32768)
    temperature: Mapped[float] = mapped_column(Float, default=0.4)
    top_p: Mapped[float] = mapped_column(Float, default=0.9)
    max_tokens: Mapped[int] = mapped_column(Integer, default=6000)
    role: Mapped[str] = mapped_column(String(20), default="primary")
    is_backup: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enable_thinking: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OutlineORM(Base):
    __tablename__ = "outlines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    template_id: Mapped[str] = mapped_column(String(36))
    chapters: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="draft")


class ReferenceDocORM(Base):
    """每本小说的参考文档（§用户需求：手动上传，供 AI 生成时参考读取）。

    以纯文本形式存储文件内容，便于直接拼入生成 Prompt。
    支持 .txt/.md/.json/.csv/.log 等文本类文件；PDF/Word 等二进制解析后续扩展。
    """
    __tablename__ = "reference_docs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    article_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # 非空=篇章参考文档（按篇维度）
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(60), default="text/plain")
    size: Mapped[int] = mapped_column(Integer, default=0)            # 字节数
    content_text: Mapped[str] = mapped_column(Text, default="")      # 文本正文
    # ---- 相关性筛选支撑列（需求 2）----
    # summary：文档要旨（上传时截断生成，或由 AI 摘要）。命中打分与「其余一行」注入都用它。
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # tags：检索关键词，命中权重最高
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # source：upload=用户上传 / global=从全局池导入 / auto=系统写入（篇章参考）
    source: Mapped[str] = mapped_column(String(20), default="upload")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============================================================================
# 本轮（用户大任务 1）新增的 3 个 ORM：设定库 / SKILL / 工作流
# ----------------------------------------------------------------------------
# 设定库（SettingORM）与 SKILL（CustomSkillORM）按设计是「全局共享」资源：
# 创建小说时可以让用户挑选其中一套作为世界观/写作助手，不与具体 project 绑定。
# 工作流（WorkflowORM）也是全局共享：描写特定类型剧情的固定流程模板，跨作品复用。
# 注意：数据库的"技能"是 SkillORM（绑定角色，按 project 隔离），
#       此处 CustomSkillORM 是用户自定义的"写作 SKILL"提示词模板（全局），两者独立。
# ============================================================================

class SettingORM(Base):
    """全局共享的世界观/设定库（境界修为、货币系统、势力模板、规则等）。

    全局共享：不带 project_id，多本小说可复用同一套设定。
    数据按 category 分桶：境界 / 货币 / 体系 / 规则 / 其它。
    列表返回（不分页）；创建后由 settings_list_cache() 缓存热门场景。
    """
    __tablename__ = "settings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))              # 设定名，例如「玄幻九境界」「灵石货币」
    category: Mapped[str] = mapped_column(String(30), index=True, default="其它")
    # levels/grades 为可选 JSON 数组，例如 ["炼气","筑基","金丹",...] 或 [{k,v}]
    levels: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)        # 检索用关键词
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否为可被选用的模板
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomSkillORM(Base):
    """自定义写作 SKILL（用户为 AI 增加的提示词/技能模板，全局共享）。

    与资料库的 SkillORM（按 project 隔离的「角色技能」）独立。
    enabled：开关默认 true；trigger 描述 AI 在何场景调用（剧情商讨/章节生成/记忆压缩…）。
    """
    __tablename__ = "custom_skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 提示词主体（可空）；空表示只作为"标签"存在，不注入
    prompt_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str | None] = mapped_column(String(120), nullable=True)  # discussion | chapter | memory | parse | all
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # ---- 调度控制（防止多个 SKILL 相互打架）----
    # category：同一分类内互斥，只有 priority 最高的一个生效；跨分类可叠加。
    #   风格 / 结构 / 禁忌 / 口吻 / 通用
    category: Mapped[str] = mapped_column(String(30), default="通用", index=True)
    # priority：数值越大越优先。互斥时取最大者；拼接时按升序排（高优先级更贴近指令末尾）。
    priority: Mapped[int] = mapped_column(Integer, default=100)
    # builtin：系统预置的 SKILL（可禁用、可改，但删除时给出提示）
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkflowORM(Base):
    """工作流：可重用的剧情/创作流程模板（节点+边结构，全局共享）。

    nodes/edges 均为 JSON：
        nodes: [{id, type, label, params: {...}, position: {x,y}}]
        edges: [{from, to, condition?: 'always'|'onSuccess'|'onFailure'}]
    设计上参考简易 DAG 流程图，前端用 vue-flow/原生 SVG 可视化编辑；本轮仅 CRUD 化。
    """
    __tablename__ = "workflows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkflowRunORM(Base):
    """工作流运行记录（一次 execute 对应一行，SSE 流式执行）。"""

    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # pending/running/success/failed
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)            # 运行入参（start 节点变量）
    outputs: Mapped[dict] = mapped_column(JSON, default=dict)           # end 节点输出（最终结果）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkflowNodeRunORM(Base):
    """工作流节点执行轨迹（一次 node 执行一行，供「运行历史→节点轨迹」查看）。"""

    __tablename__ = "workflow_node_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    workflow_id: Mapped[str] = mapped_column(String(36), index=True)
    node_id: Mapped[str] = mapped_column(String(60))                    # 画布节点 id
    node_type: Mapped[str] = mapped_column(String(30), default="generic")
    label: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/success/failed/skipped
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)            # 节点实际读取的变量值
    outputs: Mapped[dict] = mapped_column(JSON, default=dict)           # 节点输出（可能含长正文）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# 分层记忆体系（需求 3、4 的地基）
# ----------------------------------------------------------------------------
# 长篇小说写到几十章后必然超出任何模型的上下文窗口，业界通行解法是分层压缩：
#   L1 世界观      → settings / projects.summary（已有表）
#   L2 角色卡      → characters（已有表）
#   L3 阶段摘要    → StageSummaryORM（本次新增，每 N 章滚动压缩一次）
#   L4 最近章记忆  → ChapterMemoryORM（本次新增，每章一条结构化记忆）
#   L5 上一章结尾  → chapters.content 尾部原文（读表即可，不另存）
# 原先项目里 L3/L4 完全没有落点，"记忆"只有一个会被覆盖冲掉的篇章参考文档。
# ============================================================================

class ChapterMemoryORM(Base):
    """章级结构化记忆：每生成完一章，由写后摄取自动抽取并落库。

    这张表是「AI 记得住前文」的核心。注入下一章时读它，而不是读几万字原文。
    next_directions 同时服务需求 4——章后走向建议推送到对话区。
    """
    __tablename__ = "chapter_memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    chapter_id: Mapped[str] = mapped_column(String(36), index=True)
    article_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, default=0, index=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")              # 本章 150~250 字摘要
    ending_hook: Mapped[str | None] = mapped_column(Text, nullable=True)  # 结尾悬念/钩子（下一章开头要接住）
    characters: Mapped[list] = mapped_column(JSON, default=list)        # 出场角色名
    locations: Mapped[list] = mapped_column(JSON, default=list)         # 出场地点名
    plot_points: Mapped[list] = mapped_column(JSON, default=list)       # 关键事件 3~5 条
    foreshadow_actions: Mapped[list] = mapped_column(JSON, default=list)  # [{action: bury|hint|resolve, desc}]
    next_directions: Mapped[list] = mapped_column(JSON, default=list)   # [{title, detail, tension}] 需求 4
    new_entities: Mapped[list] = mapped_column(JSON, default=list)      # [{kind: character|faction|location, name, brief}] 待确认入库
    # pending=已摄取待用户确认实体入库；confirmed=已确认；skipped=用户忽略
    status: Mapped[str] = mapped_column(String(20), default="pending")
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)        # 模型原始输出，解析失败时排查用
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StageSummaryORM(Base):
    """阶段滚动摘要（L3）：把连续 N 章的章级记忆再压一层。

    scope=article/volume/range，覆盖 [from_chapter_no, to_chapter_no]。
    写到第 50 章时注入的是若干条阶段摘要 + 最近几章的章级记忆，
    而不是 50 条章级记忆，避免上下文线性膨胀。
    """
    __tablename__ = "stage_summaries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    scope: Mapped[str] = mapped_column(String(20), default="range")     # range | article | volume
    scope_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    from_chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    to_chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    key_events: Mapped[list] = mapped_column(JSON, default=list)
    open_threads: Mapped[list] = mapped_column(JSON, default=list)      # 尚未收束的线索
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppConfigORM(Base):
    """全局键值配置：上下文预算档位、写库模式开关、去 AI 味开关等。

    用 KV 表而不是给每个开关加一列——开关会一直加，加列要改 ORM 又要迁移。
    value 统一存 JSON，读的时候按 key 约定的形状取。
    """
    __tablename__ = "app_configs"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VectorChunkORM(Base):
    """向量索引块（A 线检索升级，2026-09-10）：语料切块 + embedding 存储。

    source of truth 在这张普通表（SQLAlchemy 管理，自动迁移友好）；
    sqlite-vec 的 vec0 虚拟表 `vec_index` 只是可选加速索引（可随时重建），
    加载失败时 vector_store 自动回退纯 Python 余弦——接口不变。
    向量维度由 embedding 模型决定（bge-m3 = 1024），换模型需清表重建。
    """
    __tablename__ = "vector_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    source_type: Mapped[str] = mapped_column(String(40), index=True)  # ref_doc | chapter_memory
    source_id: Mapped[str] = mapped_column(String(36), index=True)    # doc.id / memory.id
    chunk_idx: Mapped[int] = mapped_column(Integer, default=0)
    chunk_text: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    dim: Mapped[int] = mapped_column(Integer, default=0)
    embedding_json: Mapped[str] = mapped_column(Text, default="")     # float 数组 JSON（1024 维约 20KB）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChapterVariantORM(Base):
    """章节生成版本快照（Phase 4.1 最小 eval，2026-09-10）。

    每次生成/重新生成章节时自动留档一条，记录**当时的配置快照 + 正文 + 自动指标**，
    供人工打 1~5 分、同章多版本横向对比 —— 回答「改了配置之后，是变好还是变坏」。

    设计取舍：
    - 正文**整存**，不引用 `chapters.content`：重新生成会覆盖原章，历史版本就没了；
      而评估的前提恰恰是"能看见旧版本"。代价是空间（单章 2~3KB），可接受。
    - `config_snapshot` / `metrics` 用 JSON：指标会持续增加，不值得每加一个就改表加列。
    """
    __tablename__ = "chapter_variants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    chapter_id: Mapped[str] = mapped_column(String(36), index=True)   # 归属章（重新生成时不变）
    article_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")            # 该版本正文快照
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    # 生成时的配置快照：vendor / model_name / temperature / max_tokens / ref_mode / 各开关
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # 机器侧自动指标：duration_ms / first_token_ms / humanize_score / 上下文统计 等
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EvalRecordORM(Base):
    """人工评分记录（Phase 4.1 最小 eval，2026-09-10）。

    与 `ChapterVariantORM` 一对多：同一版本可重复评分（保留"什么时候改的分"、
    便于回看自己的标准有没有漂移）。取"当前分数"= 该 variant 下 created_at 最新的一条。
    """
    __tablename__ = "eval_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    variant_id: Mapped[str] = mapped_column(String(36), index=True)
    score: Mapped[int] = mapped_column(Integer)                       # 1~5 总分
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 预留：将来要多维度（文笔/一致性/设定符合度）时写这里，**无需改表**
    dimensions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LlmUsageLogORM(Base):
    """模型调用用量日志（Phase 4.2 观测消费端，2026-09-10）。

    背景：`trace_id` 此前只出现在响应信封里、**从不落库**；token 用量更是完全没提取。
    于是「这个月花了多少 token / 哪个场景最费 / 换模型后成本涨了多少」完全无法回答 ——
    成本治理为零。

    记录时机：每次**真实模型调用**结束时（章节生成 / 商讨 / 写后摄取…）。
    注意：多数厂商的**流式**响应不回 usage，此时由调用方按字符估算并置 `estimated=True`,
    以便区分「真实计量」与「估算」—— 不要把"没有数据"记成"零消耗"。
    """
    __tablename__ = "llm_usage_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    scene: Mapped[str] = mapped_column(String(40), index=True)  # chapter/discussion/ingest/overview/command
    vendor: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # 提示缓存计量（2026-09-13）：DeepSeek 等厂商会把输入 token 拆成
    # 「命中缓存」与「未命中」两档，**单价差 50 倍**（命中 $0.003/M vs 未命中 $0.15/M）。
    # 不拆开记的话，成本分析会完全失真 —— 只知道"输入 10 万 token"，不知道贵在哪一档。
    cache_hit_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_miss_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)  # True = 按字符估算，非厂商回传
    ok: Mapped[bool] = mapped_column(Boolean, default=True)          # False = 调用失败（也要计量，失败同样烧钱）
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class FeedbackRecordORM(Base):
    """反馈回流（Phase 4.3，2026-09-10）。

    记录「作者对 AI 产出做了什么」—— 这是**最强的改进信号**，此前完全被丢弃，
    导致模型下次照犯同样的错（`docs/01 §7` 记的「反馈闭环 ❌」）。

    当前覆盖 `chapter_edit`（作者修改了 AI 生成的正文）。
    设计取舍：只存**统计特征与少量样本**，不存改后全文 ——
    全文已在 `chapters.content` 里，重复存既翻倍占用又容易与正文不一致。
    """
    __tablename__ = "feedback_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)   # chapter_edit / direction_rejected …
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # chapter_id
    variant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)             # 对照的 AI 版本
    # 改动统计：{before_len, after_len, ratio, similarity, sample_before, sample_after}
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PlotTemplateORM(Base):
    """情节模板（Phase 7.1，2026-09-11 立项）：从多部小说凝练的**可复用结构骨架**。

    两层粒度：
    - ``arc``     篇级模板（约 8~15 章），创建篇时挂载；
    - ``segment`` 情节段模板（1~3 章），乐高件，可拼进任何 arc 空位、也用于卡文救急。

    ``structure`` 的核心是 **beat × variants**：每个节拍挂多本书的不同走法（``src`` 溯源）——
    模板的价值不是"标准答案"，而是"卡文时这个节拍还有哪几种走法"。
    **原文永不入库**（版权/体积/检索噪声），只存概括与结构模式。

    向量化：beat 级切块进 ``vector_chunks``（source_type='plot_template'，project_id=__global__），
    检索复用 A 线 Hybrid 基建 —— 见 ``plot_template_crud.index_template``。
    """
    __tablename__ = "plot_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    scale: Mapped[str] = mapped_column(String(20), index=True, default="arc")  # arc | segment
    genre_tags: Mapped[list] = mapped_column(JSON, default=list)               # 跨题材同构匹配
    logline: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {phases: [{phase, beats: [{beat, variants: [{src, how}]}]}]}
    structure: Mapped[dict] = mapped_column(JSON, default=dict)
    pitfalls: Mapped[list] = mapped_column(JSON, default=list)                 # 常见翻车点，规划时提醒
    rhythm: Mapped[str | None] = mapped_column(String(60), nullable=True)      # 如 "2-3-3-2"
    source_stats: Mapped[dict] = mapped_column(JSON, default=dict)             # {books, book_names, avg_chapters}
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|reviewed|archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChapterSummaryORM(Base):
    """入库管线的中间产物（Phase 7.1）：爬取小说 → 逐章概括 → 情节段归并。

    本身是**可回溯资产**：凝练 variants、修订模板时需要回到
    "这段概括来自哪本书的哪几章"。逐章概括落库后支持断点续跑（一本几百次 LLM 调用）。
    """
    __tablename__ = "chapter_summaries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    book_name: Mapped[str] = mapped_column(String(200), index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")                    # ~100 字章概括
    summary_raw: Mapped[str | None] = mapped_column(Text, nullable=True)      # 匿名化前的原文备份（可回滚）
    segment_no: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    segment_summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # 段概括（开始/发展/结尾）
    segment_summary_raw: Mapped[str | None] = mapped_column(Text, nullable=True)  # 匿名化前备份
    arc_summary_raw: Mapped[str | None] = mapped_column(Text, nullable=True)  # 弧概括匿名化前备份
    plot_label: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)  # 情节类型标签
    # —— Phase 7.1 故事弧归并（2026-09-11）——
    # 段（beat）是"事件粒度"，弧（arc）才是模板需要的单元（一个完整套路 = 一个爽点周期）。
    # 由 `plot_import.merge_arcs` 用 DeepSeek 归并生成（8B 做不了这种全局叙事理解）。
    arc_no: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    arc_name: Mapped[str | None] = mapped_column(String(120), nullable=True)   # 如"金手指觉醒"
    arc_summary: Mapped[str | None] = mapped_column(Text, nullable=True)        # 弧概括（起因→升级→转折→结果）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ArticlePlanORM(Base):
    """篇规划（Phase 7.2，2026-09-11）：一个「篇」的**章节级计划**。

    这是「篇 = 规划单元」的落地：由 模板检索 + 本书上下文 + 作者口述 生成
    （`plan_crud.generate_plan`），作者拍板（`status="confirmed"`）后驱动逐章生成 ——
    `context/layers.layer_chapter_plan` 会把「本章任务」注入生成上下文（P_CRITICAL 级，永不裁剪）。

    `plan["lines"]` 每行一章：`{no, beat, summary, new_chars, recall_chars, target_words, hook, template_ref}`；
    行级局部修改（含"AI 只改一行"）不需要重新生成整份计划。
    """
    __tablename__ = "article_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    article_id: Mapped[str] = mapped_column(String(36), index=True)
    template_ids: Mapped[list] = mapped_column(JSON, default=list)
    template_names: Mapped[list] = mapped_column(JSON, default=list)   # 冗余存名字，便于显示
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    # template=套模板生成 / free=无模板自由规划 / ai_draft=作者口述升格为临时模板
    origin: Mapped[str] = mapped_column(String(20), default="template")
    raw_ai: Mapped[dict] = mapped_column(JSON, default=dict)           # AI 原始输出（留档，供反馈对比）
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|confirmed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow)


class PlanCastingORM(Base):
    """篇规划的角色选角结果（Phase 7.3 ③，2026-09-13）：**槽位 → 本书角色** 的绑定。

    🔴 **本表是 casting 的唯一真相源**，`article_plans.plan["castings"]` 里只放
    **只读展示冗余**。为什么不让它直接住在 plan JSON 里（2026-09-12 定稿）：
    人工调整 casting（把"引路人师长"从甲改成乙）要频繁改这一小块，而 plan JSON 走的是
    「整体读改写」——每次都要 deepcopy 整份 plan。7.2 已经在这里踩过
    **SQLAlchemy JSON 列共享引用污染**（读出来改完写回，new==old → UPDATE 被静默跳过，
    实测"接口返回正确但库纹丝不动、DeepSeek 白调"）。拆表后两不相干：
    计划行归计划行，选角归选角。

    生命周期：`generate_plan` 时全量重算（一个 plan 一份 casting）→ 作者可在前端手动改
    （`source="manual"`，重算时**不覆盖**，见 `casting_crud`）→ 拍板后驱动生成。

    `UNIQUE(plan_id, slot)`：同一篇里同一功能位只能绑一个角色 —— 这是"同一配角在整篇
    身份一致"（门槛 6）的**数据库级**保证，不靠调用方自觉。

    `needs_reentry_note`（Phase 7.3.5 预留字段）：该角色是"蛰伏/离场"态却被选中时置真，
    要求生成时交代回归理由。7.3 只写不算，7.3.5 接上回归材料包。
    """
    __tablename__ = "plan_castings"
    __table_args__ = (
        UniqueConstraint("plan_id", "slot", name="uq_plan_casting_slot"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    article_id: Mapped[str] = mapped_column(String(36), index=True)
    template_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    slot: Mapped[str] = mapped_column(String(60))              # 功能槽位名（如 引路人师长）
    slot_desc: Mapped[str | None] = mapped_column(Text, nullable=True)   # 槽位功能说明（快照）
    slot_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 助力|阻碍|见证|对手
    # 匹配结果（未匹配到时 character_id 为空 → 该槽位走 new_chars，不硬凑）
    character_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    character_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)     # 显式余弦（可解释）
    # auto=系统选角 / manual=作者手改（重算时保留）
    source: Mapped[str] = mapped_column(String(10), default="auto")
    needs_reentry_note: Mapped[bool] = mapped_column(Boolean, default=False)  # 7.3.5 用
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow)


class PlannedCharORM(Base):
    """计划的**新角色引入单**（Phase 7.3.5 B 档，2026-09-13）：`new_chars` 的落地表。

    🔴 **补上断掉的一环**（docs/03 §7.3.5）：此前计划里的新角色只住在 plan JSON 的
    `lines[].new_chars` 里 —— 拍板后没人接手，角色永远不在库里，正文生成时
    被防幻觉闸门当"查无此人"剔除。本表在计划生成时把 new_chars 落成
    **pending 引入单**，作者确认后才真正建卡进 `characters`。

    为什么不塞进 `chapter_memories.new_entities`（写后摄取的待确认实体）：
    那条链按**章节**组织（chapter_id 维度），而计划新角色绑定的是**功能位 +
    篇内首登场行号** —— 硬塞进去要造伪章节记忆行，语义污染。独立表，两链路互不干扰。

    三条约束（防"人物爆炸"，docs/03 §7.3.5 定稿）：
    1. `slot` 绑定功能位 —— 与 7.3 casting 的 unmatched 槽位同一条链；
       **不自动猜绑定**（猜测性映射会误导作者），槽位留空由作者在计划页关联；
    2. 一篇新角色 **≤3**（`plan_crud.PLANNED_CHAR_LIMIT`），超出的剔除并告警；
    3. `first_appearance` 必填 = 该角色在计划里**首次出现的行为篇内章号**（自动填），
       是"第 2 章就用了第 7 章才登场的人"这类自洽校验的数据基础。

    生命周期：generate_plan 时落地（pending）→ 重生成时**只清 pending 行**，
    confirmed/dismissed 保留（作者已拍过板的意志不因重算而蒸发）→ 作者确认建卡
    （`status=confirmed`，回链 `character_id`）或忽略（`dismissed`）。

    `first_appearance` 刻意**只存篇内行号**：该章还没写，全局章号无从得知；
    建卡后真实出场章由写后摄取的 `last_seen_chapter` 派生，两套编号不混用。
    """
    __tablename__ = "plan_chars"
    __table_args__ = (
        UniqueConstraint("plan_id", "name", name="uq_plan_char_name"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    article_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(80))
    slot: Mapped[str | None] = mapped_column(String(60), nullable=True)       # 绑定的 cast 功能位（作者填）
    slot_desc: Mapped[str | None] = mapped_column(Text, nullable=True)        # 槽位功能说明（快照）
    first_appearance: Mapped[int] = mapped_column(Integer)                    # 篇内行号（必填，约束③）
    # pending=待作者确认 / confirmed=已建卡（character_id 回链）/ dismissed=作者忽略
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    character_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow)


class BookAliasORM(Base):
    """书级专名匿名化映射表（Phase 7.1 匿名化归一，2026-09-11 深夜）。

    入库概括中的源书专名统一替换为**代称**（用户方案，23:44 拍板）：
    - 主角 → `主角`；配角/势力按「敌友前缀 + 类型 + 数字」：`友·配角1` / `敌·宗门2` / `友·家族1`
      （数字无上限，敌友在前缀 —— 不用 A/a 大小写区分，LLM 高频笔误且视觉不可辨）
    - 境界 → **绝对阶梯** `境界1~境界N`（按本书修炼阶梯排序）+ 标注「主角当前=境界k」；
      不用相对字母（C=当前会随主角升级漂移，同批概括语义全乱）

    **双向表**：正向用于概括/模板/计划的匿名化；反向（代称 → 本书真实角色）用于
    写正文时还原 —— 模板因此可跨书复用。
    `role_desc` 是**槽位功能说明**（如"主角的师长，亦师亦友"）—— 计划生成时按说明选角，
    解决"模板 A 的男配10 与模板 B 的男配13 聚合时角色混乱"的问题。
    """
    __tablename__ = "book_aliases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    book_name: Mapped[str] = mapped_column(String(120), index=True)
    original: Mapped[str] = mapped_column(String(120))            # 原名（萧炎 / 云岚宗 / 斗之气）
    alias: Mapped[str] = mapped_column(String(60))                # 代称（主角 / 友·配角1 / 敌·宗门2 / 境界3）
    kind: Mapped[str] = mapped_column(String(20), default="role")  # protagonist|role|sect|family|force|place|item|realm
    relation: Mapped[str] = mapped_column(String(10), default="ally")  # ally|enemy|neutral
    role_desc: Mapped[str | None] = mapped_column(String(300), nullable=True)  # 槽位功能说明
    first_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
