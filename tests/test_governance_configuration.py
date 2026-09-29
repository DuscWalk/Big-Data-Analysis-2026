"""Configuration flows and tiny real transformations; no model network or JVM."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_fixture import AgentFixture, ScriptedModel, answer, call
from test_governance import envelopes, captured
from test_progress_workflow import InProcessHadoop
from movielens_agent.cli import main
from movielens_agent.api.app import create_app
from movielens_agent.contracts import ArtifactRef, ToolContext
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.governance import streaming as mr
from movielens_agent.governance.explanation import explanation_sections
from movielens_agent.jobs.worker import Worker
from movielens_agent.storage.governance_configs import GovernanceConfigurations, ConfigurationConflict
from movielens_agent.workflows.governance import GovernanceWorkflow


def editable(config):
    value = config.model_dump(mode="json")
    value["rules"]["version"] = "rules-v2"
    value["metrics"].update(version="metrics-v2", formula="constraint-ratios-weighted-tables",
                            table_weights={"users": 1, "movies": 1, "ratings": 3})
    value["metrics"]["reference_time"] = 978307200
    value["metrics"]["window_seconds"] = 180 * 86400
    return value


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.agent = self.f.agent([])
        self.configs = self.agent.configurations
        self.custom = GovernanceConfig.model_validate(editable(self.f.config))

    def register(self, config=None):
        return self.configs.register("180 天加权方案", config or self.custom)

    def test_legacy_references_and_round_trip_are_unchanged(self):
        saved = json.loads(Path("docs/iterations/01-governance/handoff/dataset-reference.json").read_text())
        self.assertEqual(self.f.config.refs(), saved["config_refs"])
        self.assertEqual(self.f.config.model_dump(mode="json"), json.loads(Path("configs/governance/default.json").read_text()))
        self.assertEqual(GovernanceConfig.model_validate(self.custom.model_dump()).refs(), self.custom.refs())

    def test_immutable_registration_restarts_and_content_deduplication(self):
        first = self.register()
        duplicate = self.configs.register("另一个名称", self.custom)
        self.assertEqual(first["ref"], duplicate["ref"])
        self.assertEqual(first["name"], duplicate["name"])
        reopened = GovernanceConfigurations(self.f.db, self.f.config)
        self.assertEqual(reopened.get(ArtifactRef.model_validate(first["ref"])), first)
        self.assertEqual(len(reopened.list()["items"]), 2)

    def test_default_switch_is_persistent_and_stale_updates_are_atomic(self):
        initial = self.configs.default()
        item = self.register()
        self.configs.set_default(ArtifactRef.model_validate(item["ref"]), initial["revision"])
        self.assertEqual(GovernanceConfigurations(self.f.db, self.f.config).default_ref, item["ref"])
        changed = self.custom.model_dump(mode="json")
        changed["metrics"]["window_seconds"] = 90 * 86400
        with self.assertRaises(ConfigurationConflict):
            self.configs.register("过期修改", GovernanceConfig.model_validate(changed), make_default=True, revision=initial["revision"])
        self.assertEqual(len(self.configs.list()["items"]), 2)
        self.assertEqual(self.configs.default_ref, item["ref"])

    def test_invalid_parameters_do_not_create_schemes(self):
        cases = [("metrics", "window_seconds", 0), ("metrics", "window_seconds", 253402300800),
                 ("rules", "timestamp_min", True), ("rules", "quarantine_zip_warnings", "yes"),
                 ("split", "train_end", self.custom.split.validation_end),
                 ("metrics", "formula", "invented-formula")]
        for section, field, value in cases:
            with self.subTest(field=field):
                data = editable(self.f.config); data[section][field] = value
                with self.assertRaises(ValidationError):
                    GovernanceConfig.model_validate(data)
        for weight in [0, -1, 1.5, True, 1001]:
            data = editable(self.f.config); data["metrics"]["table_weights"]["users"] = weight
            with self.assertRaises(ValidationError):
                GovernanceConfig.model_validate(data)

    def test_natural_configuration_persists_without_submitting_a_task(self):
        revision = self.configs.default()["revision"]
        self.f.model.responses = [call("governance_configure", {
            "name": "自然语言方案", "rules": {"quarantine_zip_warnings": True},
            "metrics": {"window_days": 180, "reference_time": "2001-01-01T00:00:00Z"},
            "set_default": True, "default_revision": revision}), answer("已按要求保存并设为默认。")]
        result = self.agent.respond(self.f.session, "configure", "隔离非典型邮编，时效参照 2001 年初、窗口 180 天，设为默认。")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["configuration_change"]["default_changed"])
        selected = self.configs.get(ArtifactRef.model_validate(self.configs.default_ref))
        self.assertTrue(selected["configuration"]["rules"]["quarantine_zip_warnings"])
        self.assertEqual(selected["configuration"]["metrics"]["window_seconds"], 180 * 86400)
        self.assertEqual(selected["configuration"]["metrics"]["reference_time"], 978307200)
        self.assertIsNone(self.f.tasks.claim())

    def test_configure_then_run_uses_new_scheme_and_changes_are_audited(self):
        self.f.model.responses = [call("governance_configure", {"name": "短窗口", "metrics": {"window_days": 30}}),
                                 call("governance_run", {"dataset_ref": self.f.ref})]
        result = self.agent.respond(self.f.session, "edit-run", "改为 30 天窗口并清洗", configuration_ref=self.configs.default_ref)
        self.assertEqual(result["response_origin"], "application_receipt")
        task = self.f.tasks.get(result["task_ids"][-1], self.f.session)
        self.assertEqual(task["payload"]["configuration"]["metrics"]["window_seconds"], 30 * 86400)
        self.assertEqual(task["payload"]["configuration_ref"], result["configuration_change"]["ref"])
        self.assertEqual(self.configs.default_ref, self.f.config.ref())

    def test_explicit_selection_is_bound_to_task_and_request_identity(self):
        item = self.register()
        self.f.model.responses = [call("governance_run", {"dataset_ref": self.f.ref})]
        result = self.agent.respond(self.f.session, "selected", "清洗", configuration_ref=item["ref"])
        task = self.f.tasks.get(result["task_ids"][-1], self.f.session)
        self.assertEqual(task["payload"]["configuration"], self.custom.model_dump(mode="json"))
        self.assertEqual(self.agent.respond(self.f.session, "selected", "清洗", configuration_ref=item["ref"]), result)
        from movielens_agent.storage.conversations import ConversationConflict
        with self.assertRaises(ConversationConflict):
            self.agent.respond(self.f.session, "selected", "清洗", configuration_ref=self.f.config.ref())

    def test_model_cannot_silently_override_user_selection(self):
        item = self.register()
        self.f.model.responses = [call("governance_run", {"dataset_ref": self.f.ref, "config_ref": self.f.config.ref()}), answer("方案冲突")]
        result = self.agent.respond(self.f.session, "conflict", "清洗", configuration_ref=item["ref"])
        calls = self.f.chats.calls(self.f.session, result["message_id"])
        self.assertEqual(calls[0]["result"]["error"]["code"], "CONFIG_SELECTION_CONFLICT")
        self.assertIsNone(self.f.tasks.claim())

    def test_failed_configuration_cannot_fall_back_to_old_scheme_for_cleaning(self):
        self.f.model.responses = [call("governance_configure", {"name": "非法窗口", "metrics": {"window_days": 0}}),
                                 call("governance_run", {"dataset_ref": self.f.ref}), answer("请修正窗口。")]
        result = self.agent.respond(self.f.session, "invalid-run", "更改窗口并清洗")
        calls = self.f.chats.calls(self.f.session, result["message_id"])
        self.assertEqual(calls[1]["result"]["error"]["code"], "CONFIGURATION_UNRESOLVED")
        self.assertIsNone(self.f.tasks.claim())

    def test_correcting_failed_configuration_allows_cleaning(self):
        self.f.model.responses = [call("governance_configure", {"name": "非法窗口", "metrics": {"window_days": 0}}),
                                 call("governance_configure", {"name": "合法窗口", "metrics": {"window_days": 90}}),
                                 call("governance_run", {"dataset_ref": self.f.ref})]
        result = self.agent.respond(self.f.session, "fixed-run", "改为 90 天窗口并清洗")
        task = self.f.tasks.get(result["task_ids"][-1], self.f.session)
        self.assertEqual(task["payload"]["configuration"]["metrics"]["window_seconds"], 90 * 86400)

    def test_cli_config_file_overrides_persisted_web_default(self):
        item = self.register()
        self.configs.set_default(ArtifactRef.model_validate(item["ref"]), self.configs.default()["revision"])
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["submit", "--catalog", str(self.f.db), "--session-id", self.f.session,
                           "--request-id", "cli-config", "--version", self.f.ref["version"],
                           "--config", "configs/governance/default.json"])
        self.assertEqual(status, 0, output.getvalue())
        receipt = json.loads(output.getvalue())
        task = self.f.tasks.get(receipt["task_ref"]["task_id"], self.f.session)
        self.assertEqual(task["payload"]["configuration_ref"], self.f.config.ref())
        self.assertEqual(self.configs.default_ref, item["ref"])

    def test_default_is_frozen_while_model_is_busy(self):
        entered, release = Event(), Event()
        initial = self.configs.default()
        item = self.register()
        def blocked(payload):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization failed")
            return call("governance_run", {"dataset_ref": self.f.ref})
        self.f.model.responses = [blocked]
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(self.agent.respond, self.f.session, "frozen", "清洗")
            try:
                self.assertTrue(entered.wait(5))
                self.configs.set_default(ArtifactRef.model_validate(item["ref"]), initial["revision"])
            finally:
                release.set()
            task = self.f.tasks.get(response.result()["task_ids"][-1], self.f.session)
        self.assertEqual(task["payload"]["configuration_ref"], initial["ref"])
        self.f.model.responses = [call("governance_run", {"dataset_ref": self.f.ref})]
        following = self.agent.respond(self.f.session, "following", "清洗")
        self.assertEqual(self.f.tasks.get(following["task_ids"][-1], self.f.session)["payload"]["configuration_ref"], item["ref"])

    def test_report_mode_blocks_configuration_actions_even_if_model_attempts_them(self):
        context = ToolContext(session_id=self.f.session, call_id="read-only")
        result = self.agent.registry.call("governance.configure", {"name": "不能修改", "metrics": {"window_days": 5}}, context, allow_jobs=False)
        self.assertEqual(result.error.code, "READ_ONLY_EXPLANATION")
        self.assertEqual(len(self.configs.list()["items"]), 1)

    def test_unknown_config_never_falls_back_to_default(self):
        ref = {"artifact_id": "governance.configuration", "version": "missing"}
        result = self.agent.registry.call("governance.run", {"dataset_ref": self.f.ref, "config_ref": ref},
                                         ToolContext(session_id=self.f.session, call_id="missing", request_id="missing"))
        self.assertEqual(result.error.code, "CONFIG_NOT_FOUND")
        self.assertIsNone(self.f.tasks.claim())

    def test_warning_switches_change_actual_mapper_errors(self):
        examples = [("users", "1::F::18::4::ABCD\n", "quarantine_zip_warnings", "R16_ZIP_WARNING_POLICY"),
                    ("movies", "1::Title::Drama\n", "quarantine_title_warnings", "R17_TITLE_WARNING_POLICY"),
                    ("movies", "1::LÃ©on (1994)::Drama\n", "quarantine_encoding_warnings", "R18_ENCODING_WARNING_POLICY")]
        for table, text, flag, reason in examples:
            row = json.loads(envelopes(table, text)[0])
            off = self.custom.model_dump(mode="json")
            on = self.custom.model_dump(mode="json"); on["rules"][flag] = True
            self.assertNotIn(reason, mr.evaluate(row, off)["errors"])
            self.assertIn(reason, mr.evaluate(row, on)["errors"])

    def test_hadoop_aggregation_applies_hand_calculated_weights(self):
        config = self.custom.model_dump(mode="json")
        # Six rows: all user/movie checks pass, one of two ratings has illegal score 9.
        rows = envelopes("users", "1::F::18::4::00100\n2::M::25::1::00200\n") + envelopes("movies", "1::Toy (1995)::Animation\n2::Film (2000)::Drama\n") + envelopes("ratings", "1::1::5::978307200\n2::2::9::978307200\n")
        mapped = captured(mr.map_rows, rows, config, {}, {}, "score")
        groups = captured(mr.reduce_rows, sorted(mapped), "score", config)
        partial = captured(mr.aggregate_map, groups, config)
        result = json.loads(captured(mr.aggregate_reduce, partial, config)[0])
        self.assertEqual(result["overall"]["Accurate"]["score"], (100 + 100 + 3 * 50) / 5)
        self.assertEqual(result["overall"]["Accurate"]["weights"], {"users": .2, "movies": .2, "ratings": .6})
        self.assertEqual(result["overall"]["Up-to-date"]["score"], 100)
        self.assertEqual(result["overall"]["Up-to-date"]["weights"], {"ratings": 1})

    def test_three_row_workflow_publishes_selected_parameters_and_dynamic_report(self):
        item = self.register()
        self.f.model.responses = [call("governance_run", {"dataset_ref": self.f.ref})]
        receipt = self.agent.respond(self.f.session, "tiny-run", "清洗", configuration_ref=item["ref"])
        task_id = receipt["task_ids"][-1]
        Worker(self.f.tasks, {"governance.v1": GovernanceWorkflow(self.f.tasks, InProcessHadoop(), self.f.root / "runs")}).run(once=True)
        task = self.f.tasks.get(task_id, self.f.session)
        self.assertEqual(task["status"], "succeeded", task["error"])
        directory = self.f.root / "runs" / task_id / "published"
        report = json.loads((directory / "quality.json").read_text())
        text = (directory / "report.md").read_text()
        self.assertEqual(report["configuration"], self.custom.model_dump(mode="json"))
        self.assertEqual(report["configuration_ref"], item["ref"])
        self.assertIn("1:1:3", text)
        self.assertIn("2001-01-01T00:00:00+00:00", text)
        self.assertNotIn("历史参照固定于 2003", text)
        self.assertIn("1:1:3", explanation_sections(report)["metric_method"])


class ConfigurationApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.model = ScriptedModel(self.f.settings, [])
        self.app = create_app(self.f.settings, self.model)
        self.client = self.enterContext(TestClient(self.app))
        self.route = "/api/v1/governance-configs"

    def test_save_set_default_and_reload_exposes_exact_configuration_without_paths(self):
        initial = self.client.get(self.route).json()["default"]
        saved = self.client.post(self.route, json={"name": "页面方案", "configuration": editable(self.f.config)}).json()
        self.assertEqual(self.client.get(self.route).json()["default"], initial)
        changed = self.client.put(self.route + "/default", json={"config_ref": saved["ref"], "revision": initial["revision"]})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/status").json()["configuration"], saved["configuration"])
        self.assertNotIn(str(self.f.root), changed.text)
        self.assertEqual(self.client.put(self.route + "/default", json={"config_ref": initial["ref"], "revision": initial["revision"]}).status_code, 409)
        self.assertEqual(self.client.get("/static/governance.js").status_code, 200)

    def test_one_click_saves_draft_and_sets_default_without_a_task(self):
        default = self.client.get(self.route).json()["default"]
        body = {"name": "一键默认", "configuration": editable(self.f.config), "set_default": True, "revision": default["revision"]}
        saved = self.client.post(self.route, json=body)
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(saved.json()["ref"], saved.json()["default"]["ref"])
        self.assertIsNone(self.f.tasks.claim())
        self.assertEqual(self.client.post(self.route, json=body).status_code, 409)

    def test_invalid_and_cross_origin_changes_leave_default_untouched(self):
        initial = self.client.get(self.route).json()
        value = editable(self.f.config); value["metrics"]["table_weights"]["ratings"] = 0
        body = {"name": "错误", "configuration": value}
        self.assertEqual(self.client.post(self.route, json=body).status_code, 422)
        self.assertEqual(self.client.post(self.route, json=body, headers={"origin": "https://external.invalid"}).status_code, 403)
        self.assertEqual(self.client.get(self.route).json(), initial)

    def test_message_selection_visible_before_completion_and_unknown_ref_rejected(self):
        item = self.client.post(self.route, json={"name": "页面选择", "configuration": editable(self.f.config)}).json()
        self.model.responses = [call("governance_run", {"dataset_ref": self.f.ref})]
        route = "/api/v1/sessions/" + self.f.session
        result = self.client.post(route + "/messages", json={"request_id": "selected", "content": "清洗", "configuration_ref": item["ref"]})
        self.assertEqual(result.status_code, 200)
        task = self.client.get(route + "/tasks/" + result.json()["task_ids"][-1]).json()
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["configuration"], item["configuration"])
        self.assertEqual(task["configuration_ref"], item["ref"])
        bad = {"artifact_id": "governance.configuration", "version": "unknown"}
        self.assertEqual(self.client.post(route + "/messages", json={"request_id": "bad", "content": "清洗", "configuration_ref": bad}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
