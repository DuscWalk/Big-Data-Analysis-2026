import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_fixture import AgentFixture, answer, call
from movielens_agent.agent.model import CompatibleModel, ModelError, RoutedModel
from movielens_agent.agent.settings import Settings
from movielens_agent.storage.conversations import ConversationBusy, ConversationConflict, ConversationStore


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(model_url="https://primary.invalid/v1/", model_name="primary-model",
                                 model_api_key="primary-secret", backup_url="https://backup.invalid/v1",
                                 backup_name="backup-model", backup_api_key="backup-secret")

    def test_dotenv_spaces_and_environment_precedence_without_interpolation(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"MODEL_NAME": "from-env"}, clear=True):
            path = Path(root) / ".env"
            path.write_text('MODEL_URL = "https://example.invalid/v1"\nMODEL_NAME = file-name\n'
                            'MODEL_API_KEY = "${HOME} literal"\nMODEL_NAME_BACKUP = backup\n')
            settings = Settings.load(path)
            self.assertEqual(settings.model_name, "from-env")
            self.assertEqual(settings.model_api_key.get_secret_value(), "${HOME} literal")
            self.assertEqual(settings.backup_name, "backup")
            self.assertNotIn("${HOME}", repr(settings))
        self.assertEqual(self.settings.redact({"a": ["primary-secret backup-secret"]}),
                         {"a": ["[REDACTED] [REDACTED]"]})

    def test_automatic_fallback_has_separate_credentials_and_durable_attempts(self):
        seen = []
        def handler(request):
            body = json.loads(request.content)
            seen.append((request.url.host, request.headers["authorization"], body))
            if request.url.host == "primary.invalid":
                return httpx.Response(502, text="private response primary-secret")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "backup-secret ready"}}], "usage": {"total_tokens": 12}})
        router = RoutedModel(self.settings, httpx.MockTransport(handler))
        result = router.complete(router.payload([{"role": "user", "content": "hello"}], []))
        self.assertEqual([x[1] for x in seen], ["Bearer primary-secret", "Bearer backup-secret"])
        self.assertEqual([x[2]["model"] for x in seen], ["primary-model", "backup-model"])
        self.assertEqual(result["provider"], "backup")
        self.assertEqual([a["status"] for a in result["attempts"]], ["failed", "completed"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(all("secret" not in json.dumps(x[2]) for x in seen))
        self.assertTrue(all("TOOL_CALL_PARSER" not in x[2] for x in seen))

    def test_success_never_tries_another_provider(self):
        seen = []
        def handler(request):
            seen.append(request.url.host)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]})
        result = RoutedModel(self.settings, httpx.MockTransport(handler)).complete({"messages": []})
        self.assertEqual(seen, ["primary.invalid"])
        self.assertEqual(result["provider"], "primary")

    def test_malformed_truncated_duplicate_and_timeout_fail_without_raw_body(self):
        c = {"id": "same", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
        values = [[], {"choices": [None]}, {"choices": [{"message": "primary-secret"}]},
                  {"choices": [{"message": {"content": ""}}]},
                  {"choices": [{"message": {"tool_calls": [c, c]}}]}]
        for value in values:
            with self.subTest(value=type(value).__name__):
                client = CompatibleModel(self.settings, httpx.MockTransport(lambda r: httpx.Response(200, json=value)))
                with self.assertRaises(ModelError) as caught:
                    client.complete({})
                self.assertEqual(caught.exception.code, "MODEL_INVALID_RESPONSE")
                self.assertNotIn("primary-secret", str(caught.exception))
        client = CompatibleModel(self.settings, httpx.MockTransport(lambda r: httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"tool_calls": [c]}}]})))
        with self.assertRaises(ModelError) as caught:
            client.complete({})
        self.assertEqual(caught.exception.code, "MODEL_TRUNCATED")
        def timeout(request):
            raise httpx.ReadTimeout("primary-secret", request=request)
        with self.assertRaises(ModelError) as caught:
            CompatibleModel(self.settings, httpx.MockTransport(timeout)).complete({})
        self.assertEqual(caught.exception.code, "MODEL_TIMEOUT")
        self.assertNotIn("primary-secret", str(caught.exception))

    def test_redirect_does_not_forward_authorization(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(307, headers={"location": "https://other.invalid/v1"})
        with self.assertRaises(ModelError):
            CompatibleModel(self.settings, httpx.MockTransport(handler)).complete({})
        self.assertEqual(len(seen), 1)


class AgentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))

    def test_tool_loop_normalizes_duplicate_job_calls_and_request_retries(self):
        f = self.f
        agent = f.agent([call("governance_run", {"dataset_ref": f.ref}, "one"),
                         call("governance_run", {"dataset_ref": f.ref, "rule_ref": None, "metric_ref": None}, "two"),
                         answer("已受理，尚未运行。")])
        first = agent.respond(f.session, "request", "清洗")
        second = agent.respond(f.session, "request", "清洗")
        self.assertEqual(first, second)
        self.assertEqual(len(f.model.requests), 3)
        self.assertEqual(len(first["task_ids"]), 1)
        self.assertEqual(f.tasks.get(first["task_ids"][0], f.session)["status"], "queued")
        self.assertEqual(len(f.chats.messages(f.session)), 2)
        with f.tasks.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        with self.assertRaises(ConversationConflict):
            agent.respond(f.session, "request", "不同消息")

    def test_model_failure_preserves_accepted_task_and_redacts_all_persistence(self):
        f = self.f
        agent = f.agent([call("governance_run", {"dataset_ref": f.ref}), ModelError("MODEL_TIMEOUT", "模型超时")])
        result = agent.respond(f.session, "outage", "清洗 test-primary-secret test-backup-secret")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(f.tasks.get(result["task_ids"][0], f.session)["status"], "queued")
        with f.tasks.connect() as conn:
            dump = "\n".join(conn.iterdump())
        for secret in ("test-primary-secret", "test-backup-secret"):
            self.assertNotIn(secret, dump)
            self.assertNotIn(secret, json.dumps(f.model.requests))
        self.assertEqual(result["model_calls"][-1]["status"], "failed")

    def test_unknown_invalid_and_identity_injection_are_rejected(self):
        f = self.f
        agent = f.agent([call("shell", {}), call("governance_run", "[1]"),
                         call("governance_run", {"dataset_ref": f.ref, "session_id": "other"}), answer()])
        result = agent.respond(f.session, "bad-calls", "测试")
        calls = f.chats.calls(f.session, result["message_id"])
        self.assertEqual([c["result"]["error"]["code"] for c in calls],
                         ["TOOL_NOT_FOUND", "INVALID_ARGUMENTS", "INVALID_ARGUMENTS"])
        self.assertIsNone(f.tasks.claim())

    def test_round_and_context_limits_fail_explicitly(self):
        f = self.f
        f.settings.max_rounds = 2
        agent = f.agent([call("datasets_describe", {"dataset_ref": f.ref}), call("datasets_describe", {"dataset_ref": f.ref})])
        self.assertEqual(agent.respond(f.session, "limit", "查询")["error"]["code"], "MODEL_ROUND_LIMIT")
        f.settings.max_context_chars = 4096
        agent = f.agent([])
        self.assertEqual(agent.respond(f.session, "context", "查询" * 5000)["error"]["code"], "CONTEXT_LIMIT")
        self.assertEqual(f.model.requests, [])

    def test_last_round_reserves_a_final_answer(self):
        f = self.f
        f.settings.max_rounds = 2
        def final(payload):
            self.assertEqual(payload["tool_choice"], "none")
            self.assertEqual(payload["messages"][-2]["role"], "tool")
            return answer("已读取登记清单。")
        agent = f.agent([call("datasets_describe", {"dataset_ref": f.ref}), final])
        self.assertEqual(agent.respond(f.session, "bounded-final", "查看清单")["status"], "completed")

    def test_binding_does_not_accept_evidence_from_other_task(self):
        f = self.f
        wanted = f.tasks.submit(f.session, "a", "test", {})[0]
        other = f.tasks.submit(f.session, "b", "test", {})[0]
        f.settings.max_rounds = 2
        agent = f.agent([call("tasks_get", {"task_id": other}), answer("已查询")])
        result = agent.respond(f.session, "bound", "查看", wanted)
        self.assertEqual(result["error"]["code"], "MODEL_ROUND_LIMIT")
        self.assertEqual(result["task_ids"], [wanted])

    def test_interruption_recovers_accepted_task_without_reexecution(self):
        f = self.f
        request, _ = f.chats.begin(f.session, "crashed", "清洗")
        uid = request["request_uid"]
        call_id = f.chats.start_tool(uid, "governance.run", "stable-key", {"dataset_ref": f.ref})
        model_id = f.chats.start_model(uid, 0, {"model": "test"})
        task_id = f.tasks.submit(f.session, "stable-key", "test", {})[0]
        restored = ConversationStore(f.db)
        restored.initialize()
        restored.recover_interrupted_messages()
        agent = f.agent([])
        result = agent.respond(f.session, "crashed", "清洗")
        self.assertEqual(result["error"]["code"], "MESSAGE_INTERRUPTED")
        self.assertEqual(result["task_ids"], [task_id])
        self.assertEqual(f.tasks.get(task_id, f.session)["status"], "queued")
        self.assertEqual(f.chats.calls(f.session, result["message_id"])[0]["status"], "accepted")
        self.assertEqual(f.chats.model_calls(f.session, result["message_id"])[0]["status"], "unknown")
        self.assertEqual(f.model.requests, [])

    def test_one_in_flight_message_and_explicit_task_isolation(self):
        f = self.f
        request, _ = f.chats.begin(f.session, "a", "first")
        with self.assertRaises(ConversationBusy):
            f.chats.begin(f.session, "b", "second")
        f.chats.finish(request["request_uid"], "done")
        other = f.chats.create_session()["session_id"]
        task = f.tasks.submit(other, "a", "test", {})[0]
        with self.assertRaises(KeyError):
            f.chats.begin(f.session, "b", "second", task)

    def test_unexpected_adapter_error_does_not_leave_started_attempt(self):
        f = self.f
        agent = f.agent([RuntimeError("test-primary-secret")])
        with self.assertLogs("movielens_agent.agent.service", level="ERROR") as logs:
            result = agent.respond(f.session, "unexpected", "查询")
        self.assertEqual(result["error"]["code"], "AGENT_ERROR")
        self.assertNotIn("test-primary-secret", str(logs.output))
        self.assertEqual(f.chats.model_calls(f.session, result["message_id"])[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
