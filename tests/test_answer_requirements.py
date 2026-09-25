"""Request-level correctness with hand-checkable evidence, without live services."""
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest

from agent_fixture import AgentFixture, answer, call
from quality_fixture import publish, report
from test_explanation import plan
from movielens_agent.agent.explanation import InvalidPlan, ReportAnswer
from movielens_agent.governance.explanation import answer_sections
from movielens_agent.governance.questions import FULL_SECTIONS, question_requirements


def choose_examples(ref, *, unsupported=False):
    def choose(payload):
        text = "\n".join(m.get("content") or "" for m in payload["messages"])
        keys = list(dict.fromkeys(re.findall(r'"(examples:[a-f0-9]+)"\s*:', text)))
        return answer(plan(ref, keys, unsupported=unsupported))
    return choose


class QuestionRequirementsTests(unittest.TestCase):
    def test_positive_negative_composite_and_dimension_scope(self):
        q = question_requirements("只简短解释唯一性为什么提高，不要完整报告，也不要样例")
        self.assertTrue(q.brief)
        self.assertFalse(q.full)
        self.assertEqual(q.dimensions, ("Unique",))
        self.assertEqual(set(q.required_sections), {"scores", "metric_method"})
        self.assertEqual(q.samples, ())
        other = question_requirements("解释时效性，但不要给样例；不要简短，详细说明")
        self.assertFalse(other.brief)
        self.assertEqual(other.samples, ())
        multi = question_requirements("四项质量满分但时效性低，能否证明真实？请简短回答")
        self.assertEqual(multi.dimensions, ())
        self.assertEqual(set(multi.required_sections), {"scores", "metric_method", "freshness", "limitations"})
        self.assertEqual(multi.max_sections, 4)
        self.assertEqual(multi.max_characters, 800)

    def test_sample_constraints_quantities_ordinals_and_multiple_rules(self):
        q = question_requirements("给出两条 ratings/R14_VALID_KEY_CONFLICT 的样例")
        self.assertEqual((q.samples[0].count, q.samples[0].table, q.samples[0].reason),
                         (2, "ratings", "ratings/R14_VALID_KEY_CONFLICT"))
        ordinal = question_requirements("查看第2条电影冲突样例").samples[0]
        self.assertEqual((ordinal.count, ordinal.offset, ordinal.reason), (1, 1, "movies/R14_VALID_KEY_CONFLICT"))
        english = question_requirements("Give two examples from users/R15_DUPLICATE").samples[0]
        self.assertEqual((english.count, english.mode), (2, "examples"))
        cleaned = question_requirements("请给出3条 users.jsonl 的清洗后样例").samples[0]
        self.assertEqual((cleaned.mode, cleaned.file_name, cleaned.count), ("sample", "users.jsonl", 3))
        both = question_requirements("各给一条 users/R15_DUPLICATE 和 movies/R14_VALID_KEY_CONFLICT 的样例")
        self.assertEqual(len(both.samples), 2)
        with self.assertRaises(ValueError):
            question_requirements("给出21条异常样例")

    def test_explicit_full_explanation_and_unknown_topic(self):
        self.assertTrue(FULL_SECTIONS.issubset(question_requirements("完整解释结果").required_sections))
        q = question_requirements("推荐下个月的票房冠军")
        self.assertFalse(q.full)
        self.assertEqual(q.required_sections, ())
        self.assertEqual(q.as_dict(), json.loads(json.dumps(q.as_dict())))


class AnswerCoverageTests(unittest.TestCase):
    def setUp(self):
        self.ref = {"artifact_id": "fixture.quality", "version": "exact"}
        self.report = report()

    def ready(self, question):
        a = ReportAnswer("fixture", self.ref, question=question)
        a.read_summary(self.ref, self.report, "summary-now")
        return a

    def excerpt(self, a, *, key="sample-now", reason="ratings/R14_VALID_KEY_CONFLICT", count=1,
                items=None, table="ratings", mode="examples", file_name=None):
        if items is None:
            items = [{"table": table, "source_ref": {"dataset_ref": {"artifact_id": "fixture", "version": "1"},
                       "file_name": table + ".dat", "line": i + 1, "byte_offset": i * 30},
                      "raw_preview": f"1::1::{i + 1}::975628799", "preview_truncated": False,
                      "disposition": "quarantined", "reasons": [reason.split("/", 1)[1]], "changes": []}
                     for i in range(count)]
        value = {"items": items, "available_examples": len(items), "has_more": False}
        args = {"mode": mode, "reason": reason, "table": table, "limit": count, "offset": 0, "file_name": file_name}
        a.read_excerpt(self.ref, mode, value, key, file_name, args)
        return mode + ":" + key

    def test_sample_omission_wrong_rule_count_and_old_source_are_rejected(self):
        question = "给出两条 ratings/R14_VALID_KEY_CONFLICT 的实际样例及来源"
        for variant in ["omitted", "wrong-rule", "too-few", "old"]:
            with self.subTest(variant=variant):
                a = self.ready(question)
                key = self.excerpt(a, count=1 if variant == "too-few" else 2,
                    reason="ratings/R15_DUPLICATE" if variant == "wrong-rule" else "ratings/R14_VALID_KEY_CONFLICT")
                selected = ["scores", "metric_method", "dispositions"] if variant == "omitted" else ["examples:old"] if variant == "old" else [key]
                with self.assertRaises(InvalidPlan):
                    a.render(plan(self.ref, selected))
        a = self.ready(question)
        key = self.excerpt(a, count=2)
        text, v = a.render(plan(self.ref, [key]))
        self.assertIn('"line":2', text)
        self.assertEqual(v["sample_checks"][0]["count"], 2)
        self.assertEqual(v["section_sources"][key], "sample-now")

    def test_no_samples_requires_explicit_uncertainty_without_inventing_records(self):
        a = self.ready("读取一个 ratings/R14_VALID_KEY_CONFLICT 的样例")
        key = self.excerpt(a, items=[])
        with self.assertRaises(InvalidPlan):
            a.render(plan(self.ref, [key]))
        text, validation = a.render(plan(self.ref, [key], unsupported=True))
        self.assertIn("不能据此认定总体不存在", text)
        self.assertNotIn("原始行：", text)
        self.assertEqual(validation["sample_checks"][0]["count"], 0)

    def test_cleaned_samples_require_the_requested_file_and_source_fields(self):
        a = self.ready("给出一条 users.jsonl 清洗样例")
        wrong = self.excerpt(a, mode="sample", file_name="ratings.jsonl")
        with self.assertRaises(InvalidPlan):
            a.render(plan(self.ref, [wrong]))
        right = self.excerpt(a, mode="sample", key="right", file_name="users.jsonl", table="users")
        _, validation = a.render(plan(self.ref, [right]))
        self.assertEqual(validation["sample_checks"][0]["file_name"], "users.jsonl")
        a.excerpts[right]["value"]["items"][0]["source_ref"].pop("line")
        with self.assertRaises(InvalidPlan):
            a.render(plan(self.ref, [right]))

    def test_brief_composite_answer_requires_topics_and_cannot_dump_report(self):
        a = self.ready("请简短解释四项满分、时效性低和真实性的关系")
        required = list(a.requirements.required_sections)
        with self.assertRaises(InvalidPlan):
            a.render(plan(self.ref, ["scores"]))
        with self.assertRaises(InvalidPlan):
            a.render(plan(self.ref, list(a.sections)))
        text, validation = a.render(plan(self.ref, required))
        self.assertLessEqual(len(text), 800)
        self.assertIn("Accurate", text)
        self.assertIn("Up-to-date", text)
        self.assertIn("× 100", text)
        self.assertIn("真实性", text)
        a.sections[required[0]] += "过长内容" * 250
        with self.assertRaises(InvalidPlan):
            a.render(plan(self.ref, required))
        self.assertEqual(validation, json.loads(json.dumps(validation)))

    def test_projection_preserves_single_dimension_and_zero_denominator(self):
        sections = answer_sections(self.report, dimensions=("Unique",))
        self.assertIn("Unique", sections["scores"])
        self.assertNotIn("Accurate", sections["scores"])
        concise = answer_sections(self.report, brief=True, dimensions=("Unique",))
        self.assertIn("不同合法业务键 / 全部行 × 100", concise["metric_method"])
        value = deepcopy(self.report)
        for phase in ["before", "after"]:
            value[phase]["tables"]["ratings"]["metrics"]["Up-to-date"].update(passed=0, population=0, score=None)
        brief = answer_sections(value, brief=True)
        self.assertIn("0 / 0；不可评价", brief["freshness"])


class PreparedEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.task, self.ref, self.report = publish(self.f)

    def test_changed_published_report_fails_before_any_model_call(self):
        path = self.f.root / self.task / "quality.json"
        path.write_text("tampered")
        result = self.f.agent([]).respond(self.f.session, "invalid-report", "解释本任务", self.task)
        self.assertEqual(result["error"]["code"], "EVIDENCE_UNAVAILABLE")
        self.assertEqual(self.f.model.requests, [])
        calls = self.f.chats.calls(self.f.session, result["message_id"])
        self.assertEqual(calls[0]["result"]["error"]["code"], "ARTIFACT_INVALID")

    def test_current_sample_without_model_retrieval_and_no_raw_text_in_prompt(self):
        f = self.f
        def choose(payload):
            text = "\n".join(m.get("content") or "" for m in payload["messages"])
            self.assertNotIn("01::1::05::975628799", text)
            self.assertIn('"count":1', text)
            return choose_examples(self.ref)(payload)
        result = f.agent([choose]).respond(f.session, "sample", "给出一条 ratings/R15_DUPLICATE 样例", self.task)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertEqual(len(result["model_calls"]), 1)
        self.assertEqual(result, f.agent([]).respond(f.session, "sample", "给出一条 ratings/R15_DUPLICATE 样例", self.task))

    def test_unknown_rule_empty_result_and_explicit_quantity_limit(self):
        f = self.f
        result = f.agent([choose_examples(self.ref, unsupported=True)]).respond(
            f.session, "empty", "读取一个 ratings/R99_NOT_IN_REPORT 样例", self.task)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["validation"]["unsupported"])
        invalid = f.agent([]).respond(f.session, "too-many", "给出21条样例", self.task)
        self.assertEqual(invalid["error"]["code"], "EXPLANATION_REQUEST_UNSUPPORTED")
        self.assertEqual(f.model.requests, [])

    def test_preparation_respects_call_budget_and_new_job_receipts_still_work(self):
        f = self.f
        f.settings.max_calls = 1
        result = f.agent([]).respond(f.session, "budget", "给出评分去重样例", self.task)
        self.assertEqual(result["error"]["code"], "TOOL_CALL_LIMIT")
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(f.model.requests, [])
        f.settings.max_calls = 12
        # A new run must not depend on the old report remaining readable.
        (f.root / self.task / "quality.json").write_text("old report missing or corrupted")
        receipt = f.agent([call("governance_run", {"dataset_ref": f.ref})]).respond(
            f.session, "rerun", "请重新清洗一次", self.task)
        self.assertEqual(receipt["response_origin"], "application_receipt")
        self.assertIn(self.task, receipt["task_ids"])
        self.assertEqual(len(receipt["task_ids"]), 2)

    def test_history_is_same_task_bounded_and_original_messages_remain_intact(self):
        f = self.f
        for i in range(4):
            r, _ = f.chats.begin(f.session, "h" + str(i), "旧问题" + str(i), self.task)
            f.chats.finish(r["request_uid"], "历史长回答" * 300)
        _, other, _ = publish(f, "other")
        other_task = other["artifact_id"].split(".")[0]
        r, _ = f.chats.begin(f.session, "other-message", "其他任务问题", other_task)
        f.chats.finish(r["request_uid"], "其他任务回答")
        def choose(payload):
            text = "\n".join(m.get("content") or "" for m in payload["messages"])
            self.assertNotIn("其他任务问题", text)
            self.assertNotIn("旧问题0", text)
            self.assertIn("旧问题3", text)
            self.assertIn("历史答复已节略", text)
            self.assertEqual(payload["messages"][-1]["content"], "解释评分损失")
            return answer(plan(self.ref, ["rating_loss"]))
        result = f.agent([choose]).respond(f.session, "current", "解释评分损失", self.task)
        self.assertEqual(result["status"], "completed")
        historical = f.chats.messages(f.session)[1]["content"]
        self.assertEqual(historical, "历史长回答" * 300)


if __name__ == "__main__":
    unittest.main()
