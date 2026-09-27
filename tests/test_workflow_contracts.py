from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from movielens_agent.cli import save_profile
from movielens_agent.contracts import ArtifactRef, ToolContext
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.jobs.worker import Worker
from movielens_agent.storage.catalog import DatasetCatalog
from movielens_agent.storage.tasks import TaskStore
from movielens_agent.tools.governance import interpretation_facts, register_governance_tools
from movielens_agent.tools.registry import ToolRegistry
from movielens_agent.workflows.governance import GovernanceWorkflow, package_input


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "raw"
        self.source.mkdir()
        for table, content in {"users": "1::F::18::4::00100\n",
                               "movies": "1::Toy (1995)::Animation\n",
                               "ratings": "1::1::5::978307200\n"}.items():
            (self.source / (table + ".dat")).write_text(content)
        self.db = self.root / "state.sqlite3"
        with redirect_stderr(io.StringIO()):
            result = save_profile(self.source, self.root / "profile", self.db)
        self.ref = ArtifactRef.model_validate(result["dataset_ref"])
        self.catalog = DatasetCatalog(self.db)
        self.store = TaskStore(self.db)
        self.registry = ToolRegistry()
        self.config = GovernanceConfig.read(Path("configs/governance/default.json"))
        register_governance_tools(self.registry, self.catalog, self.store, self.config)
        self.context = ToolContext(session_id="session", call_id="call", request_id="request")
        self.arguments = {"dataset_ref": self.ref.model_dump()}

    def test_interpretation_guide_preserves_overlapping_reason_semantics(self):
        report = {"after": {"reasons": {"ratings": {"R12_MISSING_USER": 2,
                    "R12_MISSING_MOVIE": 2, "R08_RATING_DOMAIN": 3}},
                    "dispositions": {"ratings": {"quarantined": 5}}},
                  "limitations": ["时间分区仅登记过滤条件，尚未物化。"]}
        facts = interpretation_facts(report)
        self.assertEqual(facts["rating_parent_reference_reason_hits"], 4)
        self.assertTrue(facts["reason_hits_may_overlap"])
        self.assertEqual(facts["ratings_deduplicated"], 0)
        self.assertEqual(facts["time_split_storage_statement"], report["limitations"][0])
        self.assertNotIn("affected_rows", facts)

    def test_registered_job_is_idempotent_and_returns_no_final_scores(self):
        first = self.registry.call("governance.run", self.arguments, self.context)
        second = self.registry.call("governance.run", self.arguments, self.context)
        self.assertEqual(first.status, "accepted")
        self.assertIsNone(first.data)
        self.assertEqual(first.task_ref, second.task_ref)
        task = self.store.get(first.task_ref["task_id"], "session")
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["payload"]["config_refs"], self.config.refs())
        read = self.registry.call("tasks.get", first.task_ref, self.context)
        self.assertEqual(read.status, "completed")
        other = self.registry.call("tasks.get", first.task_ref,
                                  ToolContext(session_id="other", call_id="read"))
        self.assertEqual(other.status, "rejected")

    def test_model_cannot_override_identity_paths_or_choose_unregistered_rules(self):
        for extra in ({"session_id": "another"}, {"command": "arbitrary"}, {"source_path": "/tmp"}):
            result = self.registry.call("governance.run", self.arguments | extra, self.context)
            self.assertEqual(result.error.code, "INVALID_ARGUMENTS")
        result = self.registry.call("governance.run", self.arguments | {
            "rule_ref": {"artifact_id": "rules", "version": "unregistered"}}, self.context)
        self.assertEqual(result.error.code, "CONFIG_NOT_FOUND")
        result = self.registry.call("governance.run", self.arguments,
                                    ToolContext(session_id="session", call_id="call"))
        self.assertEqual(result.error.code, "REQUEST_ID_REQUIRED")
        self.assertIsNone(self.store.claim())

    def test_changed_source_content_is_rejected_before_hadoop_submission(self):
        manifest = self.catalog.get(self.ref)
        target = self.root / "packaged"
        target.mkdir()
        (self.source / "ratings.dat").write_text("1::1::1::978307200\n")
        with self.assertRaisesRegex(ValueError, "source content changed"):
            package_input(manifest, target)
        with self.assertRaisesRegex(ValueError, "outside"):
            package_input(manifest, self.source)

    def test_queued_implementation_change_is_not_silently_executed(self):
        task_id, _ = self.store.submit("session", "old-code", "governance.v1", {
            "source_manifest": self.catalog.get(self.ref).model_dump(mode="json"),
            "configuration": self.config.model_dump(mode="json"),
            "config_refs": self.config.refs(), "implementation_sha256": "sha256-old-code",
        })
        Worker(self.store, {"governance.v1": GovernanceWorkflow(
            self.store, None, self.root / "runs")}).run(once=True)
        task = self.store.get(task_id, "session")
        self.assertEqual(task["status"], "failed")
        self.assertIn("Implementation changed", task["error"])
        self.assertEqual(task["artifacts"], [])
        self.assertFalse((self.root / "runs").exists())


if __name__ == "__main__":
    unittest.main()
