"""Model-selected answers checked against current evidence and explicit requests."""
import json

from pydantic import Field, StrictBool, ValidationError

from ..contracts import ArtifactRef, Contract
from ..governance.config import canonical
from ..governance.explanation import answer_sections
from ..governance.questions import FULL_SECTIONS, explicit_governance_run, question_requirements

POLICY = "quality-facts-v2"


def prompt_json(value):
    # Unicode is readable evidence, not literal \u escape sequences for the model.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AnswerPlan(Contract):
    quality_ref: ArtifactRef
    sections: list[str] = Field(max_length=16)
    unsupported: StrictBool


class InvalidPlan(ValueError):
    pass


class ReportAnswer:
    def __init__(self, task_id, quality_ref, full=False, question=""):
        self.task_id, self.ref = task_id, quality_ref
        self.requirements = question_requirements(question, full)
        self.full = self.requirements.full
        self.sections, self.sources, self.rejections = {}, {}, []
        self.excerpts = {}
        self.summary_call_id = None
        self.application_calls = []

    @classmethod
    def for_request(cls, task_id, quality_ref, *, full=False, question=""):
        if not full and explicit_governance_run(question):
            return None
        return cls(task_id, quality_ref, full=full, question=question)

    @property
    def ready(self):
        return self.summary_call_id is not None

    def instructions(self):
        return ("当前是已完成治理任务的证据解释。应用准备的本轮证据可直接使用，不必再次查询任务或摘要。"
                "没有本轮证据时才通过工具补读；历史回答不是事实依据。最终回复只能是 JSON 对象：" +
                canonical({"quality_ref": self.ref, "sections": ["freshness"], "unsupported": False}) +
                "。sections 从本轮可用要点选择，应用负责生成事实文字；不附加 Markdown、自由文本或其他字段。"
                "必须覆盖回答要求，样例题须选择符合规则/文件/数量的样例要点，不以报告概述替代样例。"
                "原始样例留在工具记录，由应用按来源生成；模型上下文只展示其选择信息。"
                "证据无法确认的结论或没有符合条件的样例时，unsupported=true，并选取能说明限制的已有要点。"
                "仅解释已有结果时不得重新清洗。必要主题：" + prompt_json(self.requirements.required_sections) +
                "；最多要点数：" + str(self.requirements.max_sections) +
                ("；回答最多 " + str(self.requirements.max_characters) + " 字符。" if self.requirements.brief else "。") +
                "样例要求：" + prompt_json(self.requirements.as_dict()["samples"]))

    def sample_queries(self, artifacts):
        for sample in self.requirements.samples:
            ref = self.ref
            if sample.mode == "sample":
                cleaned = [a for a in artifacts if a["kind"] == "cleaned_dataset"]
                if len(cleaned) != 1:
                    raise ValueError("本任务没有唯一的已发布清洗数据。")
                ref = cleaned[0]["ref"]
            yield sample.arguments(ref)

    def read_summary(self, ref, value, call_id):
        if ref != self.ref or value["task_id"] != self.task_id:
            return
        sections = answer_sections(value, brief=self.requirements.brief, dimensions=self.requirements.dimensions)
        self.sections.update(sections)
        self.summary_call_id = call_id
        self.sources.update({key: call_id for key in sections})

    def read_excerpt(self, ref, mode, value, call_id, file_name=None, arguments=None):
        key = mode + ":" + call_id
        arguments = arguments or {"mode": mode, "file_name": file_name,
                                  "reason": value.get("reason"), "offset": value.get("offset", 0)}
        metadata = {"ref": ref, "mode": mode, "file_name": file_name, "value": value, "arguments": arguments}
        self.excerpts[key] = metadata
        self.sections[key] = self._excerpt_text(metadata)
        self.sources[key] = call_id
        return "本轮新增样例要点：" + key + "。按原问题选择；空样例不能推断总体不存在异常。"

    def _excerpt_text(self, excerpt):
        value, mode = excerpt["value"], excerpt["mode"]
        items = value["items"]
        title = "来源异常样例" if mode == "examples" else "清洗数据样例"
        condition = excerpt["arguments"].get("reason") or excerpt["file_name"] or excerpt["arguments"].get("table") or "不限规则"
        lines = [f"{title}（{condition}，本次返回 {len(items)} 条；有限样例不能推算总体）："]
        if not items:
            lines.append("当前报告或所选范围未提供符合条件的样例，不能据此认定总体不存在这类记录。")
        for i, item in enumerate(items, 1):
            if mode == "examples":
                lines.append(f"{i}. 原始行：" + item["raw_preview"] + ("（原始行预览已截断）" if item.get("preview_truncated") else ""))
                lines.append("处置：" + item["disposition"] + "；原因：" + "、".join(item["reasons"]) + "。")
                if item.get("changes"):
                    lines.append("变更：" + prompt_json(item["changes"]))
            else:
                lines.append(f"{i}. 清洗值：" + prompt_json({k: v for k, v in item.items() if k != "source_ref"}))
            source = item.get("source_ref", {})
            lines.append("来源：" + prompt_json(source))
        if len(items) < excerpt["arguments"].get("limit", len(items)):
            lines.append("返回数量少于请求；这里只展示已发布产物中符合筛选/分页条件的现有样例。")
        return "\n".join(lines)

    def model_view(self):
        sections = {key: text for key, text in self.sections.items() if key not in self.excerpts}
        for key, e in self.excerpts.items():
            sections[key] = {"kind": e["mode"], "file_name": e["file_name"],
                             "reason": e["arguments"].get("reason"), "table": e["arguments"].get("table"),
                             "offset": e["arguments"].get("offset", 0), "count": len(e["value"]["items"]),
                             "available_examples": e["value"].get("available_examples"),
                             "note": "原始行、处置及来源由应用从此工具调用生成；为空时须标 unsupported=true。"}
        return {"task_id": self.task_id, "quality_ref": self.ref, "summary_call_id": self.summary_call_id,
                "application_call_ids": self.application_calls, "requirements": self.requirements.as_dict(),
                "explanation_sections": sections}

    @staticmethod
    def _matches(sample, excerpt):
        a = excerpt["arguments"]
        return (excerpt["mode"] == sample.mode and a.get("offset", 0) == sample.offset
                and (not sample.reason or a.get("reason") == sample.reason)
                and (not sample.file_name or excerpt["file_name"] == sample.file_name)
                and (sample.mode != "examples" or not sample.table or (
                    (a.get("table") == sample.table or (a.get("reason") or "").startswith(sample.table + "/"))
                    and all(item.get("table") == sample.table for item in excerpt["value"]["items"]))))

    def _check_samples(self, selected, unsupported):
        sample_checks = []
        for sample in self.requirements.samples:
            matches = [key for key in selected if key in self.excerpts and self._matches(sample, self.excerpts[key])]
            if len(matches) != 1:
                raise InvalidPlan("每项样例要求必须选择一份本轮匹配的样例，不能遗漏、混用规则/文件或重复展示。")
            excerpt = self.excerpts[matches[0]]
            value, a = excerpt["value"], excerpt["arguments"]
            items = value["items"]
            if a.get("limit", sample.count) != sample.count:
                raise InvalidPlan("样例查询数量与本轮请求不符。")
            if sample.mode == "examples":
                expected = min(sample.count, max(0, value["available_examples"] - sample.offset))
                if len(items) != expected:
                    raise InvalidPlan("样例数量不符合请求及报告可用范围。")
            elif len(items) > sample.count or value.get("has_more") and len(items) != sample.count:
                raise InvalidPlan("清洗样例数量不符合请求。")
            if not items and not unsupported:
                raise InvalidPlan("当前无匹配样例，必须明确 unsupported=true。")
            for item in items:
                source = item.get("source_ref", {})
                if not all(k in source for k in ("dataset_ref", "file_name", "line", "byte_offset")):
                    raise InvalidPlan("样例缺少原始来源定位。")
                if sample.reason and sample.reason.split("/", 1)[1] not in item.get("reasons", []):
                    raise InvalidPlan("样例实际原因与请求不符。")
            sample_checks.append({"section": matches[0], "count": len(items), "requested_count": sample.count,
                                  "reason": sample.reason, "file_name": sample.file_name, "offset": sample.offset})
        if self.requirements.samples:
            matched = {c["section"] for c in sample_checks}
            if any(key in self.excerpts and key not in matched for key in selected):
                raise InvalidPlan("包含本次样例要求之外的记录。")
        return sample_checks

    def render(self, content):
        if not self.ready:
            raise InvalidPlan("缺少本轮质量摘要。")
        try:
            plan = AnswerPlan.model_validate_json(content)
        except (ValidationError, ValueError, TypeError) as error:
            raise InvalidPlan("最终回答必须是规定结构的 JSON，不接受自由文本或额外字段。") from error
        if plan.quality_ref.model_dump() != self.ref:
            raise InvalidPlan("quality_ref 与本轮所选任务的报告不一致。")
        if len(set(plan.sections)) != len(plan.sections) or any(key not in self.sections for key in plan.sections):
            raise InvalidPlan("sections 含重复或尚未读取的要点。")
        if not plan.sections and not plan.unsupported:
            raise InvalidPlan("空要点必须明确 unsupported=true。")
        missing = set(self.requirements.required_sections) - set(plan.sections)
        if missing:
            raise InvalidPlan("遗漏用户要求的解释主题：" + "、".join(sorted(missing)))
        sample_checks = self._check_samples(plan.sections, plan.unsupported)
        if len(plan.sections) > self.requirements.max_sections:
            raise InvalidPlan("选择要点过多，请仅保留用户所问主题及必要依据。")
        parts = [f"依据任务 {self.task_id} 的已发布质量报告："]
        parts.extend(self.sections[key] for key in plan.sections)
        if plan.unsupported:
            parts.append("本轮已读取的治理证据不能确认问题中的其他结论，或尚无对应的解释要点；这些部分无法据此作答。")
        rendered = "\n\n".join(parts)
        if self.requirements.max_characters and len(rendered) > self.requirements.max_characters:
            raise InvalidPlan("回答超过本轮简短要求，请减少无关要点；不要删去必要证据。")
        validation = {"policy": POLICY, "task_id": self.task_id, "quality_ref": self.ref,
                      "summary_call_id": self.summary_call_id, "sections": plan.sections,
                      "section_sources": {key: self.sources[key] for key in plan.sections},
                      "unsupported": plan.unsupported, "rejected_attempts": self.rejections,
                      "requirements": self.requirements.as_dict(), "sample_checks": sample_checks,
                      "application_call_ids": self.application_calls, "answer_characters": len(rendered)}
        return rendered, validation
