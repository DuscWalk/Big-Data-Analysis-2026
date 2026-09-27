"""Deployment policy and rollback checks without SSH, services or cloud load."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import tarfile
import unittest
from unittest.mock import call, patch

spec = importlib.util.spec_from_file_location("pull_release", Path("scripts/deploy/pull_release.py"))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.old = self.base / "releases" / ("a" * 40)
        self.new = self.base / "releases" / ("b" * 40)
        self.old.mkdir(parents=True)
        self.new.mkdir()
        (self.base / "current").symlink_to(self.old)
        (self.base / "shared").mkdir()
        self.catalog = self.base / "shared/catalog.sqlite3"
        with sqlite3.connect(self.catalog) as db:
            db.executescript("CREATE TABLE tasks(status TEXT); CREATE TABLE chat_requests(status TEXT);")
        (self.base / "shared/.env").write_text("fixture configuration\n")

    def test_only_successful_push_of_exact_branch_commit_can_deploy(self):
        run = {"head_sha": "b" * 40, "head_branch": "feat/example", "event": "push",
               "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yml",
               "head_repository": {"full_name": "owner/repository"}, "html_url": "https://github.com/fixture"}
        accepted = lambda items: deploy.successful_run(items, "b" * 40, "feat/example", "owner/repository")
        self.assertEqual(accepted([run]), run)
        for field, value in [("head_sha", "a" * 40), ("head_branch", "main"), ("event", "pull_request"),
                             ("status", "in_progress"), ("conclusion", "failure"), ("conclusion", "cancelled"),
                             ("path", ".github/workflows/other.yml"),
                             ("head_repository", {"full_name": "untrusted/fork"})]:
            with self.subTest(field=field, value=value):
                self.assertIsNone(accepted([run | {field: value}]))

    def test_source_archive_must_match_commit_and_stay_inside_release(self):
        for suffix, revision, kind in [("src/example.py", "b" * 40, tarfile.REGTYPE),
                                       ("../../outside", "b" * 40, tarfile.REGTYPE),
                                       ("src/example.py", "a" * 40, tarfile.REGTYPE),
                                       ("src/link", "b" * 40, tarfile.SYMTYPE)]:
            with self.subTest(suffix=suffix, revision=revision, kind=kind):
                archive = self.base / "source.tar.gz"
                with tarfile.open(archive, "w:gz") as stream:
                    entry = tarfile.TarInfo("repository-" + revision + "/" + suffix)
                    entry.type = kind
                    if kind == tarfile.REGTYPE:
                        entry.size = 7
                        stream.addfile(entry, io.BytesIO(b"fixture"))
                    else:
                        entry.linkname = "/etc/passwd"
                        stream.addfile(entry)
                code = self.base / "extracted"
                if suffix == "src/example.py" and revision == "b" * 40:
                    deploy.unpack_source(archive, code, "owner/repository", "b" * 40)
                    self.assertEqual((code / suffix).read_bytes(), b"fixture")
                else:
                    with self.assertRaises(ValueError):
                        deploy.unpack_source(archive, code, "owner/repository", "b" * 40)
                self.assertFalse((self.base / "outside").exists())

    def test_busy_tasks_or_model_requests_defer_without_changing_data(self):
        self.assertFalse(deploy.busy(self.base / "absent.sqlite3"))
        for table, status in [("tasks", "queued"), ("tasks", "running"), ("chat_requests", "processing")]:
            with self.subTest(table=table, status=status):
                with sqlite3.connect(self.catalog) as db:
                    db.execute("INSERT INTO " + table + " VALUES (?)", (status,))
                with patch.object(deploy, "systemctl") as service:
                    self.assertFalse(deploy.activate(self.base, self.new, "unused", "ci"))
                    service.assert_not_called()
                self.assertEqual((self.base / "current").resolve(), self.old)
                with sqlite3.connect(self.catalog) as db:
                    self.assertEqual(db.execute("SELECT status FROM " + table).fetchone()[0], status)
                    db.execute("DELETE FROM " + table)

    def test_work_arriving_during_api_stop_keeps_old_release(self):
        with patch.object(deploy, "busy", side_effect=[False, True]), patch.object(deploy, "systemctl") as service:
            self.assertFalse(deploy.activate(self.base, self.new, "unused", "ci"))
        self.assertEqual(service.call_args_list, [call("stop", [deploy.UNITS[0]]), call("start", [deploy.UNITS[0]])])
        self.assertEqual((self.base / "current").resolve(), self.old)

    def test_success_records_exact_commit_and_preserves_shared_configuration(self):
        with patch.object(deploy, "systemctl"), patch.object(deploy, "health"):
            self.assertTrue(deploy.activate(self.base, self.new, "unused", "https://github.com/fixture"))
        self.assertEqual((self.base / "current").resolve(), self.new)
        metadata = json.loads((self.base / "deployed.json").read_text())
        self.assertEqual(metadata["commit"], "b" * 40)
        self.assertEqual(metadata["previous"], "a" * 40)
        self.assertEqual((self.base / "shared/.env").read_text(), "fixture configuration\n")
        self.assertEqual(len(list((self.base / "shared/backups").glob("*.sqlite3"))), 1)

    def test_failed_health_check_restores_old_code_without_restoring_database(self):
        with patch.object(deploy, "systemctl") as service, patch.object(
                deploy, "health", side_effect=RuntimeError("Controlled health failure")):
            with self.assertRaisesRegex(RuntimeError, "Controlled health failure"):
                deploy.activate(self.base, self.new, "unused", "ci")
        self.assertEqual((self.base / "current").resolve(), self.old)
        self.assertFalse((self.base / "deployed.json").exists())
        self.assertEqual((self.base / "shared/.env").read_text(), "fixture configuration\n")
        self.assertEqual(service.call_args_list[-2:], [call("stop"), call("start")])

    def test_api_stop_failure_requests_old_service_restart(self):
        with patch.object(deploy, "systemctl", side_effect=[RuntimeError("stop failed"), None]) as service:
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                deploy.activate(self.base, self.new, "unused", "ci")
        self.assertEqual(service.call_args_list, [call("stop", [deploy.UNITS[0]]), call("start", [deploy.UNITS[0]])])
        self.assertEqual((self.base / "current").resolve(), self.old)

    def test_failed_first_release_leaves_no_current_link(self):
        (self.base / "current").unlink()
        with patch.object(deploy, "systemctl"), patch.object(deploy, "health", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                deploy.activate(self.base, self.new, "unused", "ci")
        self.assertFalse((self.base / "current").is_symlink())
        self.assertTrue(self.catalog.exists())


if __name__ == "__main__":
    unittest.main()
