from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from movielens_agent.contracts import ArtifactRef
from movielens_agent.jobs.worker import ExternalStateUnknown, Worker
from movielens_agent.storage.tasks import RequestConflict, TaskStore
from movielens_agent.workflows.governance import make_artifact


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / "state.sqlite3")

    def submit(self, request="request", payload=None):
        return self.store.submit("session", request, "test.workflow", payload or {"a": 1, "b": 2})[0]

    def test_concurrent_idempotent_submissions_and_changed_payload_conflict(self):
        self.submit()  # Initialize schema before concurrent clients.
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.submit(), range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.submit(payload={"b": 2, "a": 1}), ids[0])
        with self.assertRaises(RequestConflict):
            self.submit(payload={"a": 3})

    def test_claim_and_restart_keep_unknown_jobs_out_of_queue(self):
        task_id = self.submit()
        self.assertEqual(self.store.claim()["task_id"], task_id)
        self.assertIsNone(self.store.claim())
        seq = self.store.start_stage(task_id, "external", self.root / "job.log")
        self.store.external_id(task_id, seq, "application_1_1")
        TaskStore(self.store.path).recover_interrupted()
        task = self.store.get(task_id, "session")
        self.assertEqual(task["status"], "unknown")
        self.assertEqual(task["attempts"][0]["external_ids"], ["application_1_1"])
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.submit(), task_id)

    def test_missing_or_corrupted_output_cannot_publish_partial_result(self):
        task_id = self.submit()
        self.store.claim()
        good = self.root / "good.txt"
        bad = self.root / "bad.txt"
        good.write_text("verified")
        bad.write_text("initial")
        artifacts = [make_artifact(task_id, "good", "report", [good], {}),
                     make_artifact(task_id, "bad", "report", [bad], {})]
        bad.write_text("changed")
        with self.assertRaises(ValueError):
            self.store.publish(task_id, artifacts)
        task = self.store.get(task_id, "session")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["artifacts"], [])
        bad.write_text("initial")
        self.store.publish(task_id, artifacts)
        self.assertEqual(self.store.get(task_id, "session")["status"], "succeeded")
        ref = ArtifactRef.model_validate(artifacts[0]["ref"])
        self.assertEqual(self.store.artifact(ref, "session")["files"][0]["sha256"],
                         artifacts[0]["files"][0]["sha256"])
        with self.assertRaises(KeyError):
            self.store.get(task_id, "different-session")
        with self.assertRaises(KeyError):
            self.store.artifact(ref, "different-session")

    def test_failed_and_uncertain_workflows_have_no_published_artifacts(self):
        for index, exception, status in ((0, RuntimeError("known failure"), "failed"),
                                         (1, ExternalStateUnknown("client lost"), "unknown")):
            task_id = self.submit(request=str(index))
            def fail(task):
                raise exception
            Worker(self.store, {"test.workflow": fail}).run(once=True)
            task = self.store.get(task_id, "session")
            self.assertEqual(task["status"], status)
            self.assertEqual(task["artifacts"], [])

    def test_unregistered_workflow_is_a_real_failure(self):
        task_id = self.submit()
        Worker(self.store, {}).run(once=True)
        self.assertEqual(self.store.get(task_id, "session")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
