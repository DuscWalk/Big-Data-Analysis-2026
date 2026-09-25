import json
from pathlib import Path
import tempfile
import unittest

from agent_fixture import AgentFixture, answer, call
from quality_fixture import publish, report
from test_governance import aggregate
from movielens_agent.agent.explanation import FULL_SECTIONS, InvalidPlan, ReportAnswer
from movielens_agent.governance.explanation import explanation_sections


def plan(ref, sections, **extra):
    return json.dumps({"quality_ref": ref, "sections": sections, "unsupported": False, **extra})


class ExplanationFactsTests(unittest.TestCase):
    def test_dispositions_overlap_and_units_are_not_model_arithmetic(self):
        value = report()
        metric = value["after"]["tables"]["ratings"]["metrics"]["Up-to-date"]
        metric.update(passed=1, population=3, score=100 / 3)
        sections = explanation_sections(value)
        self.assertIn("1 / 3 × 100 = 33.333333 分", sections["freshness"])
        self.assertIn("占比 33.333333%", sections["freshness"])
        self.assertIn("合计 3 次原因命中", sections["parent_references"])
        self.assertIn("不是去重后的受影响行数", sections["parent_references"])
        self.assertIn("输入 = 保留 + 修复 + 去重 + 隔离", sections["dispositions"])
        self.assertIn("去重 1 行", sections["rating_loss"])
        self.assertIn("隔离 8 行", sections["rating_loss"])
        self.assertIn("66.666667%", sections["rating_loss"])
        self.assertIn("不等于修复后输出行数", sections["repairs"])

    def test_empty_reports_and_missing_storage_statement_do_not_invent_facts(self):
        value = report()
        value["before"] = aggregate([])
        value["after"] = aggregate([])
        value["limitations"] = []
        sections = explanation_sections(value)
        self.assertIn("不可评价", sections["scores"])
        self.assertIn("0 / 0；不可评价", sections["freshness"])
        self.assertIn("分母为 0", sections["rating_loss"])
        self.assertIn("不能确认已有独立分区文件", sections["time_splits"])
        self.assertNotIn("尚未物化", sections["time_splits"])

    def test_storage_boundaries_and_configuration_are_from_report(self):
        value = report()
        sections = explanation_sections(value)
        self.assertIn("尚未物化", sections["time_splits"])
        self.assertIn("训练 t ≤ T1：1 行", sections["time_splits"])
        self.assertIn("验证 T1 < t ≤ T2：1 行", sections["time_splits"])
        value["config_refs"]["split"]["version"] = "wrong"
        with self.assertRaises(ValueError):
            explanation_sections(value)

    def test_plan_rejects_invented_facts_free_text_wrong_refs_and_unread_examples(self):
        ref = {"artifact_id": "fixture.quality", "version": "exact"}
        value = report()
        value["explanation_sections"] = explanation_sections(value)
        reply = ReportAnswer("fixture", ref)
        reply.read_summary(ref, value, "current-summary")
        wrong_ref = ref | {"version": "old"}
        for content in ("评分去重了八行", plan(ref, ["freshness"], text="2 / 3 = 99%"),
                        plan(wrong_ref, ["freshness"]), plan(ref, ["freshness", "freshness"]),
                        plan(ref, ["made-up"]), plan(ref, ["examples:old-call"]), plan(ref, []),
                        plan(ref, ["freshness"], unsupported="false")):
            with self.subTest(content=content), self.assertRaises(InvalidPlan):
                reply.render(content)
        text, validation = reply.render(plan(ref, [], unsupported=True))
        self.assertIn("无法据此作答", text)
        self.assertTrue(validation["unsupported"])

    def test_complete_explanation_cannot_omit_required_limitations(self):
        ref = {"artifact_id": "fixture.quality", "version": "exact"}
        value = report()
        value["explanation_sections"] = explanation_sections(value)
        reply = ReportAnswer("fixture", ref, full=True)
        reply.read_summary(ref, value, "summary")
        with self.assertRaises(InvalidPlan):
            reply.render(plan(ref, ["scores"]))
        text, validation = reply.render(plan(ref, sorted(FULL_SECTIONS)))
        self.assertEqual(set(validation["sections"]), FULL_SECTIONS)
        self.assertIn("分母随隔离和去重", text)


class ExplanationLoopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.task, self.ref, self.report = publish(self.f)

    def summary(self, ref=None):
        return call("artifacts_get", {"artifact_ref": ref or self.ref, "mode": "summary"})

    def test_incorrect_prose_is_corrected_once_not_published_and_retry_is_cached(self):
        f = self.f
        def correction(payload):
            self.assertEqual(payload["tool_choice"], "none")
            return answer(plan(self.ref, ["rating_loss", "parent_references"]))
        agent = f.agent([self.summary(), answer("评分全被修复，1 / 3 = 99%"), correction])
        result = agent.respond(f.session, "correct", "解释评分损失", self.task)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response_origin"], "evidence_rendered")
        self.assertIn("隔离 8 行", result["content"])
        self.assertNotIn("99%", result["content"])
        self.assertEqual(len(result["validation"]["rejected_attempts"]), 1)
        self.assertEqual(result, agent.respond(f.session, "correct", "解释评分损失", self.task))
        self.assertEqual(len(f.model.requests), 3)
        messages = f.chats.messages(f.session)
        self.assertNotIn("99%", json.dumps(messages))
        self.assertIn("99%", json.dumps(f.chats.model_calls(f.session, result["message_id"])))
        self.assertEqual(messages[-1]["metadata"]["validation"], result["validation"])

    def test_persistent_invalid_plan_fails_honestly_after_two_attempts(self):
        f = self.f
        agent = f.agent([self.summary(), answer("2 / 3 = 99%"), answer("2 / 3 = 99%")])
        result = agent.respond(f.session, "invalid", "解释", self.task)
        self.assertEqual(result["error"]["code"], "EXPLANATION_PLAN_INVALID")
        self.assertNotIn("99%", result["content"])
        self.assertEqual(len(result["validation"]["rejected_attempts"]), 2)
        self.assertEqual(f.tasks.get(self.task, f.session)["status"], "succeeded")

    def test_normal_followup_cannot_use_status_only_or_old_history(self):
        f = self.f
        prior, _ = f.chats.begin(f.session, "old", "解释", self.task)
        f.chats.finish(prior["request_uid"], "旧回答说评分全被修复。")
        f.settings.max_rounds = 3
        agent = f.agent([call("tasks_get", {"task_id": self.task}), answer(plan(self.ref, ["scores"])),
                         answer(plan(self.ref, ["scores"]))])
        result = agent.respond(f.session, "new", "真的是这样吗？", self.task)
        self.assertEqual(result["error"]["code"], "MODEL_ROUND_LIMIT")

    def test_report_from_other_task_does_not_satisfy_selected_task_evidence(self):
        f = self.f
        _, other, _ = publish(f, "other")
        f.settings.max_rounds = 2
        agent = f.agent([self.summary(other), answer(plan(other, ["scores"]))])
        result = agent.respond(f.session, "wrong", "解释", self.task)
        self.assertEqual(result["error"]["code"], "MODEL_ROUND_LIMIT")

    def test_unbound_report_read_also_uses_the_checked_plan(self):
        f = self.f
        agent = f.agent([self.summary(), answer(plan(self.ref, ["freshness"]))])
        result = agent.respond(f.session, "unbound", "解释这个报告")
        self.assertEqual(result["response_origin"], "evidence_rendered")
        self.assertEqual(result["validation"]["task_id"], self.task)

    def test_only_current_source_linked_examples_can_be_selected(self):
        f = self.f
        def choose(payload):
            instruction = payload["messages"][-1]["content"]
            key = instruction.split("：", 1)[1].split("（", 1)[0]
            return answer(plan(self.ref, [key]))
        agent = f.agent([self.summary(), call("artifacts_get", {"artifact_ref": self.ref, "mode": "examples",
                         "reason": "ratings/R15_DUPLICATE", "limit": 1}), choose])
        result = agent.respond(f.session, "example", "给出一个评分去重样例", self.task)
        self.assertEqual(result["status"], "completed")
        self.assertIn('"raw_preview": "01::1::05::975628799"', result["content"])
        self.assertIn('"line": 2', result["content"])
        self.assertEqual(len(result["validation"]["section_sources"]), 1)


if __name__ == "__main__":
    unittest.main()
