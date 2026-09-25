import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest

from fastapi.testclient import TestClient

from agent_fixture import AgentFixture, ScriptedModel, answer, call
from quality_fixture import publish
from movielens_agent.agent.explanation import FULL_SECTIONS
from movielens_agent.agent.model import ModelError
from movielens_agent.api.app import create_app
from movielens_agent.workflows.governance import make_artifact


class ApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.model = ScriptedModel(self.f.settings, [])
        self.app = create_app(self.f.settings, self.model)
        self.client = self.enterContext(TestClient(self.app))
        self.route = "/api/v1/sessions/" + self.f.session

    def publish(self, kind="report", content="verified content\n", filename="report.md"):
        f = self.f
        task = f.tasks.submit(f.session, "published", "test", {})[0]
        f.tasks.claim()
        path = f.root / filename
        path.write_text(content)
        artifact = make_artifact(task, "report", kind, [path], {})
        f.tasks.publish(task, [artifact])
        ref = artifact["ref"]
        route = self.route + "/artifacts/" + ref["artifact_id"] + "/versions/" + ref["version"]
        return task, path, route

    def test_status_static_and_configuration_never_expose_keys_or_urls(self):
        response = self.client.get("/api/v1/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["model_configured"])
        for secret in ("test-primary-secret", "test-backup-secret", "primary.invalid"):
            self.assertNotIn(secret, response.text)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_fresh_catalog_without_profile_is_a_valid_empty_state(self):
        settings = self.f.settings.model_copy(update={"catalog": self.f.root / "empty.sqlite3"})
        with TestClient(create_app(settings, self.model)) as client:
            result = client.get("/api/v1/status")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["datasets"], {"registered": [], "default": None})

    def test_post_retry_conflict_trace_and_session_isolation(self):
        self.model.responses = [answer("真实测试替身，不是模型验收")]
        body = {"request_id": "a", "content": "hello"}
        first = self.client.post(self.route + "/messages", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), self.client.post(self.route + "/messages", json=body).json())
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.client.post(self.route + "/messages", json=body | {"content": "changed"}).status_code, 409)
        message_id = first.json()["message_id"]
        self.assertEqual(len(self.client.get(self.route + "/messages/" + message_id + "/calls").json()["models"]), 1)
        other = self.client.post("/api/v1/sessions", json={}).json()["session_id"]
        self.assertEqual(self.client.get("/api/v1/sessions/" + other + "/messages/" + message_id + "/calls").status_code, 404)
        self.assertEqual(self.client.post(self.route + "/messages", json={"request_id": "blank", "content": "  "}).status_code, 422)

    def test_rejects_cross_origin_mutations_and_untrusted_hosts(self):
        self.assertEqual(self.client.post("/api/v1/sessions", json={}, headers={"origin": "https://external.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/status", headers={"host": "external.invalid"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/sessions", json={}, headers={"origin": "http://testserver"}).status_code, 201)

    def test_published_download_checks_exact_file_checksum_and_scope(self):
        task, path, route = self.publish()
        self.assertEqual(self.client.get(route + "/download?file_name=report.md").content, b"verified content\n")
        self.assertEqual(self.client.get(route + "/download?file_name=../.env").status_code, 422)
        other = self.client.post("/api/v1/sessions", json={}).json()["session_id"]
        self.assertEqual(self.client.get(route.replace(self.f.session, other) + "/download").status_code, 404)
        self.assertEqual(self.client.get(self.route.replace(self.f.session, other) + "/tasks/" + task).status_code, 404)
        public = self.client.get(self.route + "/tasks/" + task)
        self.assertNotIn(str(path), public.text)
        path.write_text("tampered")
        self.assertEqual(self.client.get(route + "/download").status_code, 409)
        self.assertEqual(self.client.get(route + "?mode=summary").status_code, 409)

    def test_source_linked_examples_strip_only_line_endings_and_paginate(self):
        sample = {"table": "movies", "source_ref": {"file_name": "movies.dat", "byte_offset": 0},
                  "raw_b64": base64.b64encode(b"1::Runner::Modern\r\n").decode(),
                  "disposition": "quarantined", "reasons": ["TEST"], "warnings": [], "changes": []}
        task, path, route = self.publish("quality_report", json.dumps({"after": {
            "samples": {"movies/TEST": [sample, sample]}}}), "quality.json")
        response = self.client.get(route + "?mode=examples&reason=movies/TEST")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]["value"]
        self.assertEqual(data["available_examples"], 1)
        self.assertEqual(data["items"][0]["raw_preview"], "1::Runner::Modern")
        self.assertEqual(self.client.get(route + "?mode=examples&offset=1").json()["data"]["value"]["items"], [])

    def test_model_outage_is_persisted_but_does_not_hide_task_or_download(self):
        task, path, route = self.publish()
        self.model.responses = [ModelError("MODEL_HTTP_ERROR", "模型服务返回 HTTP 502")]
        response = self.client.post(self.route + "/messages", json={"request_id": "outage", "content": "解释", "task_id": task})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["task_ids"], [task])
        self.assertEqual(self.client.get(self.route + "/tasks/" + task).json()["status"], "succeeded")
        self.assertEqual(self.client.get(route + "/download").status_code, 200)
        self.assertEqual(len(self.client.get(self.route + "/messages").json()["items"]), 2)

    def test_explanation_requires_actual_quality_summary_not_status_only(self):
        task, _, _ = self.publish()
        self.f.settings.max_rounds = 3
        self.model.responses = [call("tasks_get", {"task_id": task}), answer("无法证明的分数"), answer("无法证明的分数")]
        response = self.client.post(self.route + "/tasks/" + task + "/explanation", json={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "MODEL_ROUND_LIMIT")
        cached = self.client.post(self.route + "/tasks/" + task + "/explanation", json={})
        self.assertEqual(cached.json(), response.json())
        self.assertEqual(len(self.model.requests), 3)

    def test_completed_explanation_persists_fact_selection_and_exact_evidence(self):
        task, ref, _ = publish(self.f)
        self.model.responses = [answer(json.dumps({"quality_ref": ref, "sections": sorted(FULL_SECTIONS),
                                                   "unsupported": False}))]
        response = self.client.post(self.route + "/tasks/" + task + "/explanation", json={})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["response_origin"], "evidence_rendered")
        self.assertEqual(result["validation"]["quality_ref"], ref)
        messages = self.client.get(self.route + "/messages").json()["items"]
        self.assertEqual(messages[-1]["metadata"]["validation"], result["validation"])
        self.assertEqual(result, self.client.post(self.route + "/tasks/" + task + "/explanation", json={}).json())
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(len(result["validation"]["application_call_ids"]), 1)

    def test_simultaneous_message_conflicts_without_blocking_read_queries(self):
        entered, release = Event(), Event()
        def blocked(payload):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization failed")
            return answer()
        self.model.responses = [blocked]
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.client.post, self.route + "/messages", json={"request_id": "a", "content": "hello"})
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.client.post(self.route + "/messages", json={"request_id": "b", "content": "hello"}).status_code, 409)
                self.assertEqual(self.client.get(self.route + "/tasks").status_code, 200)
            finally:
                release.set()
            self.assertEqual(first.result().status_code, 200)

    def test_second_server_cannot_recover_an_active_server_requests(self):
        with self.assertRaisesRegex(RuntimeError, "Another API process"):
            with TestClient(create_app(self.f.settings, self.model)):
                pass


if __name__ == "__main__":
    unittest.main()
