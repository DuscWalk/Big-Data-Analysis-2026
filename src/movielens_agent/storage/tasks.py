"""Durable tasks, attempts and all-or-nothing artifact publication."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..governance.config import canonical


class RequestConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, workflow TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','unknown')),
                    stage TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(session_id, request_id));
                CREATE TABLE IF NOT EXISTS attempts (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL, status TEXT NOT NULL, log_path TEXT NOT NULL,
                    external_ids TEXT NOT NULL, error TEXT, started_at TEXT NOT NULL, ended_at TEXT,
                    PRIMARY KEY(task_id, sequence));
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT NOT NULL, version TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), manifest TEXT NOT NULL,
                    PRIMARY KEY(artifact_id,version));
            """)
            with conn:
                yield conn
        finally:
            conn.close()

    def submit(self, session_id, request_id, workflow, payload):
        if not session_id or not request_id:
            raise ValueError("Session and request identifiers must be nonempty.")
        encoded = canonical(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM tasks WHERE session_id=? AND request_id=?",
                                    (session_id, request_id)).fetchone()
            if existing:
                if existing["workflow"] != workflow or existing["payload"] != encoded:
                    raise RequestConflict("Request identifier already has different parameters.")
                return existing["task_id"], False
            task_id = uuid4().hex
            stamp = now()
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,'queued',NULL,NULL,?,?)",
                         (task_id, session_id, request_id, workflow, encoded, stamp, stamp))
            return task_id, True

    def get(self, task_id, session_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=? AND session_id=?",
                               (task_id, session_id)).fetchone()
            if row is None:
                raise KeyError("Task is not visible in this session.")
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            result["attempts"] = [dict(row) for row in conn.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY sequence", (task_id,))]
            for attempt in result["attempts"]:
                attempt["external_ids"] = json.loads(attempt["external_ids"])
            result["artifacts"] = [json.loads(row[0]) for row in conn.execute(
                "SELECT manifest FROM artifacts WHERE task_id=? ORDER BY artifact_id", (task_id,))]
            return result

    def claim(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE tasks SET status='running',updated_at=? WHERE task_id=? AND status='queued'",
                         (now(), row["task_id"]))
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            result["status"] = "running"
            return result

    def recover_interrupted(self):
        # Caller holds the single-worker OS lock. External submissions may still
        # be alive: NEVER infer failed or enqueue them again.
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET status='unknown',error=?,updated_at=? WHERE status='running'",
                         ("Worker interrupted; inspect external jobs, logs and unpublished outputs.", now()))
            conn.execute("UPDATE attempts SET status='unknown',error=? WHERE status='running'",
                         ("Worker interrupted; external state has not been verified.",))

    def start_stage(self, task_id, stage, log_path):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0] != "running":
                raise ValueError("Only running tasks may execute stages.")
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM attempts WHERE task_id=?",
                                    (task_id,)).fetchone()[0]
            conn.execute("INSERT INTO attempts VALUES (?,?,?,'running',?,'[]',NULL,?,NULL)",
                         (task_id, sequence, stage, str(log_path), now()))
            conn.execute("UPDATE tasks SET stage=?,updated_at=? WHERE task_id=?", (stage, now(), task_id))
            return sequence

    def external_id(self, task_id, sequence, identifier):
        with self.connect() as conn:
            row = conn.execute("SELECT external_ids FROM attempts WHERE task_id=? AND sequence=?",
                               (task_id, sequence)).fetchone()
            ids = json.loads(row[0])
            if identifier not in ids:
                ids.append(identifier)
                conn.execute("UPDATE attempts SET external_ids=? WHERE task_id=? AND sequence=?",
                             (canonical(ids), task_id, sequence))

    def end_stage(self, task_id, sequence, status="succeeded", error=None):
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET status=?,error=?,ended_at=? WHERE task_id=? AND sequence=?",
                         (status, error, now(), task_id, sequence))

    def stop(self, task_id, status, error):
        if status not in ("failed", "unknown"):
            raise ValueError("Use publish for task success.")
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET status=?,error=?,updated_at=? WHERE task_id=? AND status='running'",
                         (status, error, now(), task_id))

    def publish(self, task_id, artifacts):
        if not artifacts:
            raise ValueError("Cannot publish an empty result.")
        refs = set()
        # All verification occurs before any visible database mutations.
        for artifact in artifacts:
            ref = artifact["ref"]
            key = ref["artifact_id"], ref["version"]
            if key in refs or artifact["producer_task_id"] != task_id or artifact["state"] != "ready":
                raise ValueError("Invalid or duplicate artifact.")
            refs.add(key)
            if not artifact["files"]:
                raise ValueError("Artifacts require verified files.")
            for file in artifact["files"]:
                path = Path(file["path"])
                if not path.is_file() or path.stat().st_size != file["size_bytes"]:
                    raise ValueError("Artifact file missing or wrong size.")
                if file_digest(path) != file["sha256"]:
                    raise ValueError("Artifact file checksum mismatch.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row[0] != "running":
                raise ValueError("Only running tasks can publish outputs.")
            for artifact in artifacts:
                ref = artifact["ref"]
                conn.execute("INSERT INTO artifacts VALUES (?,?,?,?)",
                             (ref["artifact_id"], ref["version"], task_id, canonical(artifact)))
            conn.execute("UPDATE tasks SET status='succeeded',stage='published',error=NULL,updated_at=? WHERE task_id=?",
                         (now(), task_id))

    def artifact(self, ref, session_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT a.manifest FROM artifacts a JOIN tasks t ON a.task_id=t.task_id "
                "WHERE a.artifact_id=? AND a.version=? AND t.session_id=? AND t.status='succeeded'",
                (ref.artifact_id, ref.version, session_id)).fetchone()
            if row is None:
                raise KeyError("Artifact is not published or visible in this session.")
            return json.loads(row[0])
