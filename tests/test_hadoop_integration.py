"""Opt-in integration: real HDFS/YARN, no local executor or mocked results."""
import json
import os
from pathlib import Path
import unittest
from uuid import uuid4

from movielens_agent.adapters.hadoop import Hadoop, HadoopRuntime
from movielens_agent.contracts import DatasetManifest, ToolContext
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.governance.profile import profile_dataset
from movielens_agent.jobs.worker import Worker
from movielens_agent.storage.catalog import DatasetCatalog
from movielens_agent.storage.tasks import TaskStore
from movielens_agent.tools.governance import register_governance_tools
from movielens_agent.tools.registry import ToolRegistry
from movielens_agent.workflows.governance import GovernanceWorkflow, implementation_digest
from governance_fixture import DATA, EXPECTED_DISPOSITIONS


@unittest.skipUnless(os.environ.get("MOVIELENS_HADOOP_RUNTIME"), "Set MOVIELENS_HADOOP_RUNTIME for real YARN jobs.")
class HadoopIntegrationTests(unittest.TestCase):
    def test_real_governance_and_versioned_evidence(self):
        root = Path("var/hadoop-tests") / uuid4().hex
        source = root / "raw"
        source.mkdir(parents=True)
        for table, content in DATA.items():
            (source / (table + ".dat")).write_text(content, encoding="iso-8859-1")
        manifest = DatasetManifest.model_validate(profile_dataset(source)["manifest"])
        catalog = DatasetCatalog(root / "catalog.sqlite3")
        catalog.register(manifest)
        store = TaskStore(catalog.path)
        config = GovernanceConfig.read(Path("configs/governance/default.json"))
        registry = ToolRegistry()
        register_governance_tools(registry, catalog, store, config)
        context = ToolContext(session_id="hadoop-test", call_id="submit", request_id="fixture")
        args = {"dataset_ref": manifest.ref.model_dump()}
        accepted = registry.call("governance.run", args, context)
        self.assertEqual(accepted.status, "accepted")
        self.assertIsNone(accepted.data)
        repeated = registry.call("governance.run", args, context)
        self.assertEqual(repeated.task_ref, accepted.task_ref)
        task_id = accepted.task_ref["task_id"]
        print(f"\nHadoop integration task={task_id}, catalog={catalog.path}", flush=True)
        hadoop = Hadoop(HadoopRuntime.read(os.environ["MOVIELENS_HADOOP_RUNTIME"]))
        Worker(store, {"governance.v1": GovernanceWorkflow(store, hadoop, root / "runs")}).run(once=True)
        task = store.get(task_id, context.session_id)
        self.assertEqual(task["status"], "succeeded", task["error"])
        jobs = [attempt for attempt in task["attempts"] if attempt["external_ids"]]
        self.assertEqual(len(jobs), 7)
        for attempt in jobs:
            self.assertEqual(attempt["status"], "succeeded")
            self.assertTrue(any(identifier.startswith("application_") for identifier in attempt["external_ids"]))
        quality = next(artifact for artifact in task["artifacts"] if artifact["kind"] == "quality_report")
        evidence = registry.call("artifacts.get", {"artifact_ref": quality["ref"], "mode": "summary"}, context)
        self.assertEqual(evidence.status, "completed")
        self.assertEqual(evidence.data["value"]["after"]["dispositions"], EXPECTED_DISPOSITIONS)
        self.assertEqual(evidence.data["value"]["after"]["splits"], {"train": 1, "validation": 1, "test": 1})
        for dimension in ("Accurate", "Complete", "Unique", "Consistent"):
            self.assertEqual(evidence.data["value"]["after"]["overall"][dimension]["score"], 100)
        self.assertEqual(evidence.data["value"]["before"]["tables"]["ratings"]["metrics"]["Complete"]["population"], 48)
        foreign = registry.call("tasks.get", {"task_id": task_id},
                                ToolContext(session_id="another-session", call_id="read"))
        self.assertEqual(foreign.status, "rejected")
        print(json.dumps({"task_id": task_id, "status": task["status"],
                          "jobs": [job["external_ids"] for job in jobs]}), flush=True)

    def test_known_mapper_failure_keeps_results_unpublished(self):
        root = Path("var/hadoop-tests") / uuid4().hex
        source = root / "raw"
        source.mkdir(parents=True)
        for table, content in DATA.items():
            (source / (table + ".dat")).write_text(content, encoding="iso-8859-1")
        manifest = DatasetManifest.model_validate(profile_dataset(source)["manifest"])
        store = TaskStore(root / "catalog.sqlite3")
        config = GovernanceConfig.read(Path("configs/governance/default.json"))
        task_id, _ = store.submit("failure-test", "mapper-failure", "governance.v1", {
            "source_manifest": manifest.model_dump(mode="json"),
            "configuration": config.model_dump(mode="json"), "config_refs": config.refs(),
            "implementation_sha256": implementation_digest(),
        })
        runtime = HadoopRuntime.read(os.environ["MOVIELENS_HADOOP_RUNTIME"])
        runtime = runtime.model_copy(update={"python": Path("/nonexistent/movielens-fixture-python")})
        print(f"\\nHadoop failure task={task_id}, catalog={store.path}", flush=True)
        Worker(store, {"governance.v1": GovernanceWorkflow(
            store, Hadoop(runtime), root / "runs")}).run(once=True)
        task = store.get(task_id, "failure-test")
        self.assertEqual(task["status"], "failed", task["error"])
        self.assertEqual(task["stage"], "before-parents")
        self.assertEqual(task["artifacts"], [])
        self.assertEqual(task["attempts"][-1]["status"], "failed")
        applications = [identifier for identifier in task["attempts"][-1]["external_ids"]
                        if identifier.startswith("application_")]
        self.assertEqual(len(applications), 1)
        self.assertEqual(Hadoop(runtime).application_status(applications[0])["final_state"], "FAILED")
        print({"task_id": task_id, "status": task["status"], "application_ids": applications}, flush=True)
