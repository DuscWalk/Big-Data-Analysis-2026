"""Measured progress, persisted attempts and real Hadoop log framing."""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_fixture import AgentFixture, ScriptedModel
from movielens_agent.adapters.hadoop import JobProgress
from movielens_agent.api.app import create_app
from movielens_agent.jobs.progress import metric
from movielens_agent.storage.tasks import TaskStore
from movielens_agent.workflows.governance import package_input, read_jsonl


class JobProgressTests(unittest.TestCase):
    def test_complete_lines_split_chunks_carriage_returns_and_duplicate_values(self):
        values = []
        parser = JobProgress(lambda mapped, reduced: values.append((mapped, reduced)))
        parser.feed(b"INFO mapreduce.Job: map 0% red")
        self.assertEqual(values, [])
        parser.feed(b"uce 0%\rINFO mapreduce.Job: map 45% reduce 0%\n")
        parser.feed(b"INFO mapreduce.Job: map 45% reduce 0%\nother log\n")
        parser.feed(b"INFO mapreduce.Job: map 100% reduce 5")
        self.assertEqual(values, [(0, 0), (45, 0)])
        parser.feed(b"0%\nINFO mapreduce.Job: map 100% reduce 100%")
        parser.feed(b"", final=True)
        self.assertEqual(values, [(0, 0), (45, 0), (100, 50), (100, 100)])

    def test_invalid_percentages_are_ignored_and_retries_may_reduce_progress(self):
        values = []
        parser = JobProgress(lambda *pair: values.append(pair))
        parser.feed(b"map 105% reduce 10%\nmap 20% reduce 999%\nmap -1% reduce 0%\n")
        self.assertEqual(values, [])
        parser.feed(b"map 70% reduce 30%\nmap 40% reduce 0%\n")
        self.assertEqual(values, [(70, 30), (40, 0)])


class StoredProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = AgentFixture(Path(temp.name))
        self.task = self.f.tasks.submit(self.f.session, "progress", "governance.v1", {})[0]
        self.f.tasks.claim()
        self.sequence = self.f.tasks.start_stage(self.task, "before-ratings", self.f.root / "private.log")

    def report(self, value=35):
        self.f.tasks.update_progress(self.task, self.sequence, message="Hadoop 作业处理中",
            metrics=[metric("map", "Map", value, 100, "percent"), metric("reduce", "Reduce", 0, 100, "percent")])

    def test_progress_survives_reload_is_scoped_and_late_updates_do_not_revive_stage(self):
        self.report()
        store = TaskStore(self.f.db)
        value = store.get(self.task, self.f.session)["attempts"][0]["progress"]
        self.assertEqual(value["metrics"][0]["current"], 35)
        self.assertIn("updated_at", value)
        with self.assertRaises(KeyError):
            store.get(self.task, "different-session")
        self.f.tasks.end_stage(self.task, self.sequence, "failed", "controlled failure")
        self.report(100)
        after = store.get(self.task, self.f.session)["attempts"][0]
        self.assertEqual(after["progress"], value)
        self.assertEqual(after["status"], "failed")

    def test_worker_interruption_keeps_last_progress_and_rejects_late_updates(self):
        self.report()
        self.f.tasks.recover_interrupted()
        self.report(90)
        task = self.f.tasks.get(self.task, self.f.session)
        self.assertEqual(task["status"], "unknown")
        self.assertEqual(task["attempts"][0]["status"], "unknown")
        self.assertEqual(task["attempts"][0]["progress"]["metrics"][0]["current"], 35)

    def test_invalid_measurements_do_not_replace_valid_snapshot(self):
        self.report()
        for values in [metric("map", "Map", 101, 100, "percent"), metric("rows", "行数", -1, 2, "rows"),
                       metric("bad", "错误", 2, 2, "percent")]:
            with self.assertRaises(ValidationError):
                self.f.tasks.update_progress(self.task, self.sequence, message="invalid", metrics=[values])
        self.assertEqual(self.f.tasks.get(self.task, self.f.session)["attempts"][0]["progress"]["metrics"][0]["current"], 35)

    def test_existing_database_adds_progress_storage_without_rewriting_attempts(self):
        with self.f.tasks.connect() as db:
            db.execute("DROP TABLE attempt_progress")
        task = TaskStore(self.f.db).get(self.task, self.f.session)
        self.assertIsNone(task["attempts"][0]["progress"])
        self.assertEqual(task["attempts"][0]["sequence"], self.sequence)
        self.report()
        self.assertEqual(self.f.tasks.get(self.task, self.f.session)["attempts"][0]["progress"]["metrics"][0]["current"], 35)

    def test_http_progress_is_current_and_does_not_expose_execution_paths(self):
        with TestClient(create_app(self.f.settings, ScriptedModel(self.f.settings, []))) as client:
            path = "/api/v1/sessions/" + self.f.session + "/tasks/" + self.task
            for value in (25, 65):
                self.report(value)
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                task = response.json()
                self.assertEqual(task["workflow"], "governance.v1")
                self.assertEqual(task["attempts"][0]["progress"]["metrics"][0]["current"], value)
                self.assertNotIn(str(self.f.root), response.text)
            other = self.f.chats.create_session()["session_id"]
            self.assertEqual(client.get(path.replace(self.f.session, other)).status_code, 404)

    def test_packaging_reports_exact_rows_and_jsonl_reports_consumed_bytes(self):
        manifest = self.f.catalog.get(self.f.catalog.versions("ml-1m.raw")[0])
        target = self.f.root / "packaged"
        target.mkdir()
        rows = []
        outputs = package_input(manifest, target, on_progress=lambda *pair: rows.append(pair))
        self.assertEqual(rows[0], (0, 3))
        self.assertEqual(rows[-1], (3, 3))
        self.assertEqual([pair[0] for pair in rows], [0, 1, 2, 3])
        data = list(read_jsonl(outputs["ratings"]))
        self.assertEqual(len(data), 1)
        values = []
        self.assertEqual(list(read_jsonl(outputs["ratings"], on_progress=lambda *pair: values.append(pair))), data)
        total = outputs["ratings"].stat().st_size
        self.assertEqual(values[-1], (total, total))
        empty = self.f.root / "empty.jsonl"
        empty.write_bytes(b"")
        self.assertEqual(list(read_jsonl(empty, on_progress=lambda *pair: values.append(pair))), [])
        self.assertEqual(values[-1], (0, 0))


if __name__ == "__main__":
    unittest.main()
