"""Single independent worker; workflows plug in as handlers."""
import fcntl
from pathlib import Path
import time

from ..storage.tasks import TaskStore


class ExternalStateUnknown(RuntimeError):
    """The external job may be alive or complete; do not retry implicitly."""


class Worker:
    def __init__(self, store: TaskStore, workflows: dict):
        self.store, self.workflows = store, workflows

    def run_once(self):
        task = self.store.claim()
        if task is None:
            return False
        try:
            handler = self.workflows.get(task["workflow"])
            if handler is None:
                raise ValueError("Workflow is not registered in this worker.")
            artifacts = handler(task)
            self.store.publish(task["task_id"], artifacts)
        except ExternalStateUnknown as error:
            self.store.stop(task["task_id"], "unknown", str(error))
        except Exception as error:
            self.store.stop(task["task_id"], "failed", f"{type(error).__name__}: {error}")
        return True

    def run(self, once=False, poll_seconds=2):
        lock_path = Path(str(self.store.path) + ".worker.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another worker owns this database.") from error
            self.store.recover_interrupted()
            while True:
                processed = self.run_once()
                if once:
                    return
                if not processed:
                    time.sleep(poll_seconds)
