"""A bounded model-selected answer plan, rendered from current tool evidence."""
import json

from pydantic import Field, StrictBool, ValidationError

from ..contracts import ArtifactRef, Contract
from ..governance.config import canonical

POLICY = "quality-facts-v1"
FULL_SECTIONS = {"scores", "dispositions", "freshness", "time_splits", "limitations"}


class AnswerPlan(Contract):
    quality_ref: ArtifactRef
    sections: list[str] = Field(max_length=16)
    unsupported: StrictBool


class InvalidPlan(ValueError):
    pass


class ReportAnswer:
    def __init__(self, task_id, quality_ref, full=False):
        self.task_id, self.ref, self.full = task_id, quality_ref, full
        self.sections, self.sources, self.rejections = {}, {}, []
        self.summary_call_id = None

    @property
    def ready(self):
        return self.summary_call_id is not None

    def instructions(self):
        return ("当前是已完成治理任务的证据解释。先通过工具读取该任务精确 quality_report 的 summary。"
                "最终回复只能是 JSON 对象，结构为 " + canonical({
                    "quality_ref": self.ref, "sections": ["freshness"], "unsupported": False}) +
                "。sections 从本轮摘要的 explanation_sections 选取与用户问题有关的要点并排序；"
                "应用使用这些事实原文生成回答。不要附加 Markdown 围栏、自由文本、数字或其他字段。"
                "普通追问仅选相关要点，不必每次复述全报告。需要异常样例时，先读该质量产物的 examples；"
                "需要清洗样例时，先读该任务 cleaned_dataset 的 sample，应用会提供额外可选 section ID。"
                "若问题含证据无法确认的结论或不支持的问题，unsupported 必须为 true；完全无法回答时 sections 可为空。"
                "不得依据历史回答推断数据，不得为解释已有结果重新提交任务。" +
                ("本次为完整自动解释，sections 至少包含 " + canonical(sorted(FULL_SECTIONS)) + "。" if self.full else ""))

    def read_summary(self, ref, value, call_id):
        if ref != self.ref or value["task_id"] != self.task_id:
            return
        self.sections.update(value["explanation_sections"])
        self.summary_call_id = call_id
        for key in value["explanation_sections"]:
            self.sources[key] = call_id

    def read_excerpt(self, ref, mode, value, call_id, file_name=None):
        # Caller verifies membership in this task. Only bounded tool outputs are
        # quoted; raw text remains data and is never evaluated or made into HTML.
        key = mode + ":" + call_id
        title = "已读取的来源样例（有限代表样例，不能推算总体）" if mode == "examples" else "已读取的清洗数据样例"
        self.sections[key] = title + "：\n" + json.dumps(
            {"artifact_ref": ref, "file_name": file_name, **value}, ensure_ascii=False, indent=2)
        self.sources[key] = call_id
        return "本轮新增可选解释要点：" + key + "（" + title + "）。"

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
        if self.full and not FULL_SECTIONS.issubset(plan.sections):
            raise InvalidPlan("自动解释缺少必要的评分、处置、时效、划分或局限。")
        parts = [f"依据任务 {self.task_id} 的已发布质量报告："]
        parts.extend(self.sections[key] for key in plan.sections)
        if plan.unsupported:
            parts.append("本轮已读取的治理证据不能确认问题中的其他结论，或尚无对应的解释要点；这些部分无法据此作答。")
        validation = {"policy": POLICY, "task_id": self.task_id, "quality_ref": self.ref,
                      "summary_call_id": self.summary_call_id, "sections": plan.sections,
                      "section_sources": {key: self.sources[key] for key in plan.sections},
                      "unsupported": plan.unsupported, "rejected_attempts": self.rejections}
        return "\n\n".join(parts), validation
