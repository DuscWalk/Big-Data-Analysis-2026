"""Three-row workflow checks with an in-process adapter; no JVM or YARN."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_fixture import AgentFixture
from movielens_agent.governance import streaming as mr
from movielens_agent.jobs.worker import Worker
from movielens_agent.workflows.governance import GovernanceWorkflow, implementation_digest


def captured(function, *args):
    stream = io.StringIO()
    with redirect_stdout(stream):
        function(*args)
    return stream.getvalue()


class InProcessHadoop:
    """Run Python transformations on tiny fixtures; never contact a cluster."""
    runtime = SimpleNamespace(hdfs_root="/fixture", python="python")

    def __init__(self, fail_upload=False):
        self.files = {}
        self.fail_upload = fail_upload

    def fs(self, *args):
        pass

    def put(self, source, destination):
        if self.fail_upload and destination.endswith("/dataset/movies.jsonl"):
            raise OSError("Controlled upload failure")
        self.files[destination] = Path(source).read_text()

    def fetch(self, source, destination):
        Path(destination).write_text(self.files[source])

    def run_job(self, *, inputs, output, files, mapper, on_progress, **kwargs):
        config = json.loads(files["configuration.json"].read_text())
        rows = [row for source in inputs for row in self.files[source].splitlines()]
        on_progress(0, 0)
        if "aggregate-map" in mapper:
            mapped = captured(mr.aggregate_map, rows, config)
            reduced = captured(mr.aggregate_reduce, mapped.splitlines(), config)
        else:
            mode = mapper[mapper.index("--mode") + 1]
            parents = json.loads(files["parents.json"].read_text()) if "parents.json" in files else {}
            raw = json.loads(files["raw-parents.json"].read_text()) if "raw-parents.json" in files else {}
            mapped = captured(mr.map_rows, rows, config, parents, raw, mode)
            reduced = captured(mr.reduce_rows, sorted(mapped.splitlines()), mode, config)
        on_progress(100, 0)
        self.files[output] = reduced
        on_progress(100, 100)
        return []


class WorkflowProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        manifest = self.f.catalog.get(self.f.catalog.versions("ml-1m.raw")[0])
        self.task_id = self.f.tasks.submit(self.f.session, "workflow-progress", "governance.v1", {
            "source_manifest": manifest.model_dump(mode="json"),
            "configuration": self.f.config.model_dump(mode="json"),
            "config_refs": self.f.config.refs(), "implementation_sha256": implementation_digest(),
        })[0]
        self.snapshots = []

    def run_workflow(self, fail_upload=False):
        store = self.f.tasks
        original = store.update_progress

        def observe(task_id, sequence, **kwargs):
            original(task_id, sequence, **kwargs)
            attempt = store.get(task_id, self.f.session)["attempts"][-1]
            self.snapshots.append(attempt)

        workflow = GovernanceWorkflow(store, InProcessHadoop(fail_upload), self.f.root / "runs")
        with patch.object(store, "update_progress", side_effect=observe):
            Worker(store, {"governance.v1": workflow}).run(once=True)
        return store.get(self.task_id, self.f.session)

    def test_three_rows_reach_publication_with_measured_export_and_uploads(self):
        task = self.run_workflow()
        self.assertEqual(task["status"], "succeeded", task["error"])
        self.assertEqual(len(task["attempts"]), 9)
        self.assertEqual(len(task["artifacts"]), 4)
        for attempt in task["attempts"]:
            self.assertEqual(attempt["status"], "succeeded")
            self.assertTrue(attempt["progress"]["metrics"])
            for metric in attempt["progress"]["metrics"]:
                self.assertEqual(metric["current"], metric["total"])
        self.assertEqual(task["attempts"][0]["progress"]["metrics"][0]["total"], 3)
        export = [item["progress"] for item in self.snapshots if item["stage"] == "verify-and-export"]
        results = self.f.root / "runs" / self.task_id / "results"
        total = sum((results / name).stat().st_size for name in ["clean-parents.jsonl", "clean-ratings.jsonl"])
        self.assertEqual(export[0]["metrics"][0]["current"], 0)
        self.assertEqual(export[-1]["metrics"][0]["total"], total)
        self.assertTrue(any(0 < item["metrics"][0]["current"] < total for item in export))
        self.assertEqual(sorted({item["metrics"][1]["current"] for item in export}), [0, 1, 2, 3])
        downloads = [item for item in self.snapshots if "正在下载结果" in item["progress"]["message"]]
        self.assertEqual(len(downloads), 7)
        self.assertTrue(all(item["status"] == "running" for item in downloads))
        self.assertTrue(all(item["progress"]["metrics"][1]["current"] == 100 for item in downloads))

    def test_upload_failure_keeps_partial_progress_and_no_publication(self):
        task = self.run_workflow(fail_upload=True)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["artifacts"], [])
        attempt = task["attempts"][-1]
        self.assertEqual(attempt["stage"], "verify-and-export")
        self.assertEqual(attempt["status"], "failed")
        exported, uploaded = attempt["progress"]["metrics"]
        self.assertEqual(exported["current"], exported["total"])
        self.assertGreater(exported["total"], 0)
        self.assertEqual((uploaded["current"], uploaded["total"]), (1, 3))
        self.assertIn("Controlled upload failure", task["error"])


if __name__ == "__main__":
    unittest.main()
