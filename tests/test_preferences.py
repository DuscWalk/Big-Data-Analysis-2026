"""Model changes, secret boundaries and persistent conversation navigation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_fixture import AgentFixture, ScriptedModel, answer
from movielens_agent.agent.preferences import ModelPreferences
from movielens_agent.api.app import create_app


class PreferencesApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.model = ScriptedModel(self.f.settings, [])
        self.app = create_app(self.f.settings, self.model)
        self.client = self.enterContext(TestClient(self.app))

    def edit(self):
        value = self.client.get("/api/v1/model-settings").json()
        return {key: value[key] for key in ("revision", "provider", "timeout_seconds", "max_tokens")} | {
            name: {"url": value[name]["url"], "model": value[name]["model"]}
            for name in ("primary", "backup")}

    def test_view_retains_keys_without_revealing_them_and_save_survives_reload(self):
        public = self.client.get("/api/v1/model-settings")
        self.assertTrue(public.json()["primary"]["key_configured"])
        for key in ("test-primary-secret", "test-backup-secret"):
            self.assertNotIn(key, public.text)
        edit = self.edit()
        edit["primary"]["model"] = "changed-model"
        response = self.client.put("/api/v1/model-settings", json=edit)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-primary-secret", response.text)
        self.assertNotEqual(response.json()["revision"], edit["revision"])
        self.assertEqual(self.client.get("/api/v1/status").json()["model_name"], "changed-model")
        preferences = ModelPreferences(self.f.settings)
        self.assertEqual(preferences.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(preferences.snapshot().model_name, "changed-model")
        self.assertEqual(preferences.snapshot().model_api_key.get_secret_value(), "test-primary-secret")
        self.assertEqual(preferences.snapshot().max_calls, self.f.settings.max_calls)
        self.assertEqual(self.client.put("/api/v1/model-settings", json=edit).status_code, 409)

    def test_new_endpoint_requires_explicit_key_then_clear_and_replace(self):
        edit = self.edit()
        edit["primary"]["url"] = "https://other.invalid/v1"
        self.assertEqual(self.client.put("/api/v1/model-settings", json=edit).status_code, 422)
        edit["primary"]["api_key"] = "replacement-secret"
        response = self.client.put("/api/v1/model-settings", json=edit)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("replacement-secret", response.text)
        self.assertEqual(self.app.state.agent.settings.model_api_key.get_secret_value(), "replacement-secret")
        edit = self.edit()
        edit["primary"]["clear_key"] = True
        edit["primary"]["api_key"] = "ambiguous-secret"
        self.assertEqual(self.client.put("/api/v1/model-settings", json=edit).status_code, 422)
        del edit["primary"]["api_key"]
        response = self.client.put("/api/v1/model-settings", json=edit)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["primary"]["key_configured"])
        self.assertEqual(ModelPreferences(self.f.settings).snapshot().model_api_key.get_secret_value(), "")

    def test_invalid_fields_do_not_echo_submitted_credentials(self):
        secret = "private-validation-secret"
        for field, value in (("url", "https://" + secret + "@example.invalid/v1"),
                             ("api_key", {"bad": secret}), ("api_key", secret + "\nextra")):
            edit = self.edit()
            edit["primary"][field] = value
            response = self.client.put("/api/v1/model-settings", json=edit)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)
        edit = self.edit()
        edit["timeout_seconds"] = -1
        edit["primary"]["api_key"] = secret
        response = self.client.post("/api/v1/model-settings/probe", json={"provider": "primary", "configuration": edit})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)

    def test_write_failure_preserves_active_and_stored_configuration(self):
        self.assertEqual(self.client.put("/api/v1/model-settings", json=self.edit()).status_code, 200)
        path = self.app.state.model_preferences.path
        previous = path.read_bytes()
        active = self.app.state.agent
        edit = self.edit()
        edit["primary"]["model"] = "must-not-activate"
        with patch("movielens_agent.agent.preferences.os.replace", side_effect=OSError("simulated")):
            self.assertEqual(self.client.put("/api/v1/model-settings", json=edit).status_code, 503)
        self.assertIs(self.app.state.agent, active)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.client.get("/api/v1/model-settings").json()["revision"], edit["revision"])
        self.assertEqual(list(path.parent.glob(".model-settings-*")), [])

    def test_running_answer_keeps_its_configuration_snapshot(self):
        entered, release = Event(), Event()
        original = self.app.state.agent
        def blocked(payload):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization failed")
            return answer(original.settings.model_name)
        self.model.responses = [blocked]
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(self.client.post, "/api/v1/sessions/" + self.f.session + "/messages",
                                   json={"request_id": "snapshot", "content": "hello"})
            try:
                self.assertTrue(entered.wait(5))
                edit = self.edit()
                edit["primary"]["model"] = "next-model"
                self.assertEqual(self.client.put("/api/v1/model-settings", json=edit).status_code, 200)
                self.assertIsNot(self.app.state.agent, original)
                self.assertEqual(original.settings.model_name, "test-model")
                self.assertEqual(self.app.state.agent.settings.model_name, "next-model")
            finally:
                release.set()
            self.assertEqual(response.result().json()["content"], "test-model")

    def test_draft_checks_use_selected_provider_without_saving_or_creating_tasks(self):
        edit = self.edit()
        edit["backup"] = {"url": "https://backup.invalid/v1", "model": "draft-model", "api_key": "draft-secret"}
        body = {"provider": "backup", "configuration": edit}
        with patch("movielens_agent.api.app.tool_probe", return_value={"status": "completed", "native_tool_call": True,
                "model": "draft-model", "message": "draft-secret"}) as probe:
            response = self.client.post("/api/v1/model-settings/probe", json=body)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("draft-secret", response.text)
            candidate = probe.call_args.args[0]
            self.assertEqual(candidate.model_provider, "backup")
            self.assertEqual(candidate.backup_name, "draft-model")
            self.assertEqual(candidate.backup_api_key.get_secret_value(), "draft-secret")
            self.assertIn("checked_at", response.json())
        with patch("movielens_agent.api.app.list_models", return_value={"providers": [{"provider": "backup", "http_status": 200,
                "models": ["draft-model"]}]}) as models:
            self.assertEqual(self.client.post("/api/v1/model-settings/models", json=body).json()["providers"][0]["models"], ["draft-model"])
            self.assertEqual(models.call_args.args[0].backup_name, "draft-model")
        self.assertFalse(self.app.state.model_preferences.path.exists())
        self.assertEqual(self.client.get("/api/v1/model-settings").json()["revision"], edit["revision"])
        with self.f.chats.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_requests").fetchone()[0], 0)

    def test_cli_probe_uses_saved_preferences_and_explicit_provider_override(self):
        from movielens_agent.cli import main
        edit = self.edit()
        edit["provider"] = "backup"
        edit["backup"] = {"url": "https://backup.invalid/v1", "model": "saved-backup", "api_key": "backup-test"}
        self.assertEqual(self.client.put("/api/v1/model-settings", json=edit).status_code, 200)
        with patch("movielens_agent.agent.settings.Settings.load", return_value=self.f.settings), \
                patch("movielens_agent.agent.probe.tool_probe", return_value={"status": "completed"}) as probe, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["model-probe", "--catalog", str(self.f.db)]), 0)
            self.assertEqual(probe.call_args.args[0].model_provider, "backup")
            self.assertEqual(probe.call_args.args[0].backup_name, "saved-backup")
            self.assertEqual(main(["model-probe", "--catalog", str(self.f.db), "--provider", "primary"]), 0)
            self.assertEqual(probe.call_args.args[0].model_provider, "primary")
        self.assertNotIn("backup-test", output.getvalue())

    def test_text_only_model_response_is_not_reported_as_a_tool_capable_connection(self):
        from movielens_agent.agent.probe import tool_probe
        with patch("movielens_agent.agent.probe.RoutedModel") as factory:
            factory.return_value.payload.return_value = {}
            factory.return_value.complete.return_value = {"message": {"content": "ready"},
                "provider": "primary", "model": "text-only", "attempts": []}
            result = tool_probe(self.f.settings)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["native_tool_call"])
        self.assertEqual(result["error"], "INVALID_PROBE_CALL")

    def test_preferences_mutations_require_same_origin_and_do_not_leak_exceptions(self):
        self.assertEqual(self.client.put("/api/v1/model-settings", json=self.edit(),
                                        headers={"origin": "https://outside.invalid"}).status_code, 403)
        with patch("movielens_agent.api.app.tool_probe", side_effect=RuntimeError("test-primary-secret")):
            response = self.client.post("/api/v1/model-settings/probe", json={"provider": "primary", "configuration": self.edit()})
            self.assertEqual(response.json()["status"], "failed")
            self.assertNotIn("test-primary-secret", response.text)

    def test_history_search_pagination_activity_and_processing(self):
        newest = self.f.chats.create_session("早先建的实验")
        request, _ = self.f.chats.begin(newest["session_id"], "history-1", "唯一检索短语与百分号%")
        other = self.f.chats.create_session("后建但没有提问")
        self.f.chats.finish(request["request_uid"], "history answer")
        result = self.client.get("/api/v1/sessions?limit=1").json()
        self.assertTrue(result["has_more"])
        item = result["items"][0]
        self.assertEqual(item["session_id"], newest["session_id"])
        self.assertEqual(item["message_count"], 2)
        self.assertIn("唯一检索短语", item["preview"])
        second = self.client.get("/api/v1/sessions?limit=1&offset=1").json()
        self.assertEqual(second["items"][0]["session_id"], other["session_id"])
        result = self.client.get("/api/v1/sessions", params={"q": "唯一检索短语"}).json()
        self.assertEqual([row["session_id"] for row in result["items"]], [newest["session_id"]])
        self.assertEqual(len(self.client.get("/api/v1/sessions", params={"q": "%"}).json()["items"]), 1)
        self.assertEqual(self.client.get("/api/v1/sessions", params={"q": "' OR 1=1"}).json()["items"], [])
        self.f.chats.begin(other["session_id"], "history-2", "进行中")
        result = self.client.get("/api/v1/sessions").json()
        self.assertEqual(result["items"][0]["session_id"], other["session_id"])
        self.assertTrue(result["items"][0]["processing"])
        self.assertEqual(self.client.get("/api/v1/sessions?offset=-1").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/sessions?limit=101").status_code, 422)


if __name__ == "__main__":
    unittest.main()
