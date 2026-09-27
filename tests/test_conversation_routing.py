"""Selected task context must not turn ordinary messages into report requests."""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from agent_fixture import AgentFixture, ScriptedModel, answer, call
from quality_fixture import publish
from test_explanation import plan
from movielens_agent.agent.explanation import ReportAnswer
from movielens_agent.api.app import create_app


class ConversationRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.task, self.ref, _ = publish(self.f)

    def test_greeting_stays_conversation_with_history_and_inherited_task(self):
        f = self.f
        prior, _ = f.chats.begin(f.session, "old", "解释评分损失", self.task)
        f.chats.finish(prior["request_uid"], "依据已发布质量报告：评分减少了。")
        with f.chats.connect() as conn:
            conn.execute("UPDATE chat_sessions SET active_task_id=? WHERE session_id=?", (self.task, f.session))
        def greet(payload):
            self.assertEqual(payload["messages"][-1]["content"], "你好")
            self.assertNotIn("explanation_sections", json.dumps(payload, ensure_ascii=False))
            self.assertEqual(f.chats.calls(f.session, f.chats.messages(f.session)[-1]["message_id"]), [])
            self.assertIn("quality_report_ref", payload["messages"][0]["content"])
            return answer("你好！有什么我可以帮你的吗？")
        agent = f.agent([greet])
        result = agent.respond(f.session, "hello", "你好")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response_origin"], "model")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["task_ids"], [self.task])
        self.assertEqual(len(f.model.requests), 1)
        self.assertEqual(f.chats.session(f.session)["active_task_id"], self.task)
        self.assertEqual(agent.respond(f.session, "hello", "你好"), result)
        self.assertEqual(len(f.model.requests), 1)
        # The task stays available for a subsequent concrete question.
        report = f.agent([call("artifacts_get", {"artifact_ref": self.ref, "mode": "summary"}),
                          answer(plan(self.ref, ["rating_loss"]))]).respond(
            f.session, "report-after-hello", "你好，请解释评分损失")
        self.assertEqual(report["response_origin"], "evidence_rendered")
        self.assertEqual(report["validation"]["task_id"], self.task)

    def test_general_questions_do_not_depend_on_report_readability(self):
        f = self.f
        (f.root / self.task / "quality.json").write_text("unreadable old report")
        questions = ["谢谢", "你能做什么？", "什么是数据清洗？", "什么是唯一性？",
                     "质量报告是什么意思？", "如何配置模型？", "给一个 Python 排序示例",
                     "推荐下个月的票房冠军", "不要解释质量报告，只打个招呼",
                     "你为什么一直在给我讲质量报告？"]
        for i, question in enumerate(questions):
            with self.subTest(question=question):
                result = f.agent([answer("普通回答")]).respond(f.session, "general-" + str(i), question, self.task)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["response_origin"], "model")
                self.assertEqual(result["tool_calls"], [])
                self.assertNotIn("validation", result)
                self.assertEqual(len(f.model.requests), 1)

    def test_greeting_does_not_force_evidence_for_pending_or_failed_tasks(self):
        f = self.f
        for status in ("queued", "failed"):
            task = f.tasks.submit(f.session, status, "test", {})[0]
            if status == "failed":
                with f.tasks.connect() as conn:
                    conn.execute("UPDATE tasks SET status='failed' WHERE task_id=?", (task,))
            result = f.agent([answer("你好！")]).respond(f.session, status, "你好", task)
            self.assertEqual(result["response_origin"], "model")
            self.assertEqual(result["tool_calls"], [])

    def test_status_question_reads_task_without_preparing_quality_report(self):
        f = self.f
        (f.root / self.task / "quality.json").write_text("unreadable old report")
        result = f.agent([call("tasks_get", {"task_id": self.task}), answer("该任务已完成。")]).respond(
            f.session, "status", "你好，当前任务完成了吗？", self.task)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response_origin"], "model")
        self.assertEqual([c["name"] for c in result["tool_calls"]], ["tasks.get"])

    def test_mixed_greeting_report_question_keeps_strict_evidence(self):
        for i, question in enumerate(["你好，请解释评分损失", "谢谢，请给出本次时效性的分子和分母",
                                      "什么是数据清洗？再解释本次评分损失"]):
            with self.subTest(question=question):
                reply = ReportAnswer(self.task, self.ref, question=question)
                self.assertIsNotNone(reply)
                sections = list(reply.requirements.required_sections)
                result = self.f.agent([call("artifacts_get", {"artifact_ref": self.ref, "mode": "summary"}),
                                       answer(plan(self.ref, sections))]).respond(
                    self.f.session, "mixed-" + str(i), question, self.task)
                self.assertEqual(result["response_origin"], "evidence_rendered")
                self.assertEqual(result["validation"]["quality_ref"], self.ref)
                self.assertEqual(len(result["tool_calls"]), 1)
        (self.f.root / self.task / "quality.json").write_text("unreadable report")
        failed = self.f.agent([call("artifacts_get", {"artifact_ref": self.ref, "mode": "summary"})]).respond(
            self.f.session, "mixed-failed", "你好，请解释评分损失", self.task)
        self.assertEqual(failed["error"]["code"], "EVIDENCE_UNAVAILABLE")
        self.assertEqual(len(self.f.model.requests), 1)

    def test_explicit_full_explanation_prepares_evidence_before_model(self):
        f = self.f
        forced = ReportAnswer(self.task, self.ref, question="你好", full=True)
        result = f.agent([answer(plan(self.ref, list(forced.requirements.required_sections)))]).respond(
            f.session, "forced", "你好", self.task, require_quality=True)
        self.assertEqual(result["response_origin"], "evidence_rendered")
        self.assertTrue(result["validation"]["requirements"]["full"])

    def test_model_directed_report_read_still_uses_validated_rendering(self):
        # The model may choose a report even without report keywords. That
        # actual tool choice must activate validation for the selected task.
        result = self.f.agent([call("artifacts_get", {"artifact_ref": self.ref, "mode": "summary"}),
                               answer(plan(self.ref, ["rating_loss"]))]).respond(
            self.f.session, "model-read", "这里发生了什么？", self.task)
        self.assertEqual(result["response_origin"], "evidence_rendered")
        self.assertEqual(result["validation"]["quality_ref"], self.ref)
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["validation"]["application_call_ids"], [])

    def test_model_chosen_sample_read_prepares_missing_summary_only(self):
        from test_answer_requirements import choose_examples
        f = self.f
        def select(payload):
            text = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("01::1::05::975628799", text)
            self.assertNotIn("governance_run", [t["function"]["name"] for t in payload["tools"]])
            return choose_examples(self.ref)(payload)
        result = f.agent([call("artifacts_get", {"artifact_ref": self.ref, "mode": "examples",
                          "reason": "ratings/R15_DUPLICATE", "table": "ratings", "limit": 1}), select]).respond(
            f.session, "sample-first", "给一个评分重复样例", self.task)
        self.assertEqual(result["response_origin"], "evidence_rendered")
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertEqual(len(result["validation"]["application_call_ids"]), 1)
        self.assertEqual(result["validation"]["sample_checks"][0]["count"], 1)

    def test_http_greeting_with_selected_task_returns_normal_reply(self):
        f = self.f
        model = ScriptedModel(f.settings, [answer("你好！")])
        with TestClient(create_app(f.settings, model)) as client:
            response = client.post("/api/v1/sessions/" + f.session + "/messages", json={
                "request_id": "http-greeting", "content": "你好", "task_id": self.task})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["content"], "你好！")
            self.assertEqual(response.json()["tool_calls"], [])
            self.assertEqual(response.json()["response_origin"], "model")

    def test_new_run_is_model_chosen_and_does_not_read_old_report(self):
        f = self.f
        (f.root / self.task / "quality.json").write_text("unreadable old report")
        result = f.agent([call("governance_run", {"dataset_ref": f.ref})]).respond(
            f.session, "new-run", "你好，请重新清洗一次", self.task)
        self.assertEqual(result["response_origin"], "application_receipt")
        self.assertEqual([c["name"] for c in result["tool_calls"]], ["governance.run"])
        self.assertEqual(len(result["task_ids"]), 2)

    def test_model_can_clarify_without_querying_the_selected_task(self):
        result = self.f.agent([answer("你想继续看报告，还是了解使用方法？")]).respond(
            self.f.session, "clarify", "说说那个", self.task)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(len(self.f.model.requests), 1)


if __name__ == "__main__":
    unittest.main()
