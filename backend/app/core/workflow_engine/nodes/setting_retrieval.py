"""设定检索节点：从设定库按 2-gram 相关性检索条目（对标 Dify 知识检索）。

零向量依赖：与现有 discussion/章节链路的 _relevance 同一思路，
对小说自造词（人名/门派名）最友好。
"""
from ..base import BaseNode, NodeResult


class SettingRetrievalNode(BaseNode):
    node_type = "setting-retrieval"
    node_label = "设定检索"
    category = "AI 能力"
    description = "从设定库检索相关条目（按 2-gram 相关性），供下游 LLM/章节节点做上下文。"
    params_schema = [
        {"name": "query_template", "label": "检索词模板", "type": "textarea", "required": True,
         "help": "支持 {{#node.var#}} 引用，如「本章涉及：{{#start.hint#}}」"},
        {"name": "scope", "label": "范围", "type": "select", "default": "global",
         "options": ["global", "project"], "help": "global=全局设定库；project=仅该小说选中的设定"},
        {"name": "project_id", "label": "小说(project_id)", "type": "variable", "required": False,
         "placeholder": "如 start.project_id；仅 scope=project 时用"},
        {"name": "top_k", "label": "返回条数", "type": "number", "default": 4},
        {"name": "category", "label": "分类过滤", "type": "string", "required": False,
         "placeholder": "如 体系 / 境界 / 货币，留空 = 不限"},
    ]
    outputs_desc = {
        "result": "命中设定列表 [{id,name,category,levels,description,tags,score}]",
        "result_text": "拼接好的设定文本（可直接塞进 LLM 提示词）",
    }

    def _run(self, pool, db, inputs: dict) -> NodeResult:
        from app.core.context.layers import _relevance
        from app.models.orm import ProjectORM, SettingORM, SettingTemplateORM

        query = pool.render(self.params.get("query_template") or "")
        if not query.strip():
            return NodeResult(status="failed", error="检索词为空")
        try:
            top_k = int(self.params.get("top_k") or 4)
        except (TypeError, ValueError):
            top_k = 4

        q = db.query(SettingORM)
        scope = self.params.get("scope") or "global"
        project_setting_ids = None
        if scope == "project":
            pid = pool.resolve_value(self.params.get("project_id")) or ""
            if pid:
                proj = db.query(ProjectORM).filter_by(id=str(pid)).first()
                if proj and proj.setting_ids:
                    q = q.filter(SettingORM.id.in_(proj.setting_ids))
                    project_setting_ids = list(proj.setting_ids)
        cat = (self.params.get("category") or "").strip()
        if cat:
            q = q.filter(SettingORM.category == cat)

        rows = list(q.all())

        # 设定模板（2026-09-26）：同一 id 空间，一并参与检索评分
        tq = db.query(SettingTemplateORM)
        if scope == "project" and project_setting_ids is not None:
            tq = tq.filter(SettingTemplateORM.id.in_(project_setting_ids))
        tpl_rows = tq.all() if (not cat or cat == "设定模板") else []

        scored = []
        for s in rows:
            text = " ".join(
                filter(None, [
                    s.name,
                    s.category,
                    s.description or "",
                    " ".join(str(x) for x in (s.levels or [])),
                ])
            )
            scored.append((round(_relevance(text, query), 4), ("setting", s)))
        for t in tpl_rows:
            text = " ".join(filter(None, [
                t.name, t.genre or "", t.summary or "", (t.content or "")[:800],
            ]))
            scored.append((round(_relevance(text, query), 4), ("template", t)))
        scored.sort(key=lambda x: -x[0])
        picked = scored[:top_k]

        result = []
        blocks = []
        for sc, (kind, o) in picked:
            if kind == "setting":
                item = {
                    "id": o.id,
                    "name": o.name,
                    "category": o.category,
                    "levels": o.levels or [],
                    "description": o.description or "",
                    "tags": o.tags or [],
                    "score": sc,
                }
                parts = [f"【设定：{item['name']}（{item['category']}）】"]
                if item["levels"]:
                    parts.append("层级阶梯：" + "→".join(str(x) for x in item["levels"]))
                if item["description"]:
                    parts.append(item["description"])
            else:
                item = {
                    "id": o.id,
                    "name": o.name,
                    "category": "设定模板",
                    "levels": [],
                    "description": o.content or "",
                    "tags": o.tags or [],
                    "score": sc,
                }
                parts = [f"【设定模板：{o.name}（{o.genre or '通用'}）】", o.content or ""]
            result.append(item)
            blocks.append("\n".join(parts))

        return NodeResult(outputs={"result": result, "result_text": "\n\n".join(blocks)})
