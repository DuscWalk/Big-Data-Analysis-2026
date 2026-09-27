from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("local_cluster", Path("scripts/hadoop/local_cluster.py"))
cluster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cluster)


class HadoopStartupTests(unittest.TestCase):
    def test_cold_start_waits_through_connection_refusal_and_safe_mode(self):
        replies = [subprocess.CompletedProcess([], 255, "", "Connection refused"),
                   subprocess.CompletedProcess([], 0, "Safe mode is ON", ""),
                   subprocess.CompletedProcess([], 0, "Safe mode is OFF", "")]
        with patch.object(cluster.subprocess, "run", side_effect=replies) as run, \
             patch.object(cluster.time, "sleep"), redirect_stdout(io.StringIO()):
            cluster.wait_for_hdfs(Path("/test/hadoop"), {})
        self.assertEqual(run.call_count, 3)

    def test_deadline_does_not_claim_cluster_is_ready(self):
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            cluster.wait_for_hdfs(Path("/test/hadoop"), {}, timeout=0)
