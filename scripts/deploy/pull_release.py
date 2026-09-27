#!/usr/bin/env python3
"""Pull a public repository release only after its exact push passed GitHub CI."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

SHA = re.compile(r"[0-9a-f]{40}")
UNITS = ["movielens-api.service", "movielens-worker.service"]


def github(path):
    request = Request("https://api.github.com/" + path, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "movielens-release-poller"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def successful_run(runs, sha, branch, repository):
    return next((run for run in runs if run.get("head_sha") == sha
                 and run.get("head_branch") == branch and run.get("event") == "push"
                 and run.get("status") == "completed" and run.get("conclusion") == "success"
                 and run.get("path") == ".github/workflows/ci.yml"
                 and (run.get("head_repository") or {}).get("full_name") == repository), None)


def busy(catalog):
    if not catalog.exists():
        return False
    with sqlite3.connect(catalog.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tasks" in tables and db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('queued','running') LIMIT 1").fetchone():
            return True
        return "chat_requests" in tables and bool(db.execute(
            "SELECT 1 FROM chat_requests WHERE status='processing' LIMIT 1").fetchone())


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, timeout=kwargs.pop("timeout", 600), **kwargs)


def systemctl(action, units=UNITS):
    runtime = "/run/user/" + str(os.getuid())
    env = os.environ | {"XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + runtime + "/bus"}
    run("systemctl", "--user", action, *units, env=env, timeout=2100)


def point_current(base, release):
    temporary = base / ".current-next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release)
    temporary.replace(base / "current")


def health(url, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                result = json.load(response)
            if "configuration_refs" in result and "datasets" in result:
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("New release did not pass the read-only API health check.")


def unpack_source(archive, code, repository, sha):
    expected = repository.split("/")[1] + "-" + sha
    with tarfile.open(archive, "r:gz") as source:
        entries = source.getmembers()
        if len(entries) > 20000 or sum(item.size for item in entries) > 256 * 1024 * 1024:
            raise ValueError("Source archive exceeds the release size limit.")
        for item in entries:
            parts = PurePosixPath(item.name).parts
            if not parts or parts[0] != expected or ".." in parts or not (item.isdir() or item.isfile()):
                raise ValueError("Source archive has an unexpected revision, path or file type.")
        for item in entries:
            target = code.joinpath(*PurePosixPath(item.name).parts[1:])
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as incoming, target.open("xb") as output:
                    shutil.copyfileobj(incoming, output)
                target.chmod(0o700 if item.mode & 0o111 else 0o600)


def prepare(base, sha, repository, python):
    release = base / "releases" / sha
    marker = release / ".prepared"
    if marker.exists():
        return release
    # Failed preparation is kept for inspection; the next attempt can rebuild it.
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    code = release / "code"
    archive = release / "source.tar.gz"
    url = "https://codeload.github.com/" + repository + "/tar.gz/" + sha
    deadline, received = time.monotonic() + 180, 0
    with urlopen(url, timeout=30) as incoming, archive.open("xb") as output:
        while chunk := incoming.read(256 * 1024):
            received += len(chunk)
            if received > 128 * 1024 * 1024 or time.monotonic() > deadline:
                raise RuntimeError("Source download exceeded its size or time limit.")
            output.write(chunk)
    unpack_source(archive, code, repository, sha)
    (release / ".source-sha").write_text(sha + "\n")
    run(python, "-m", "venv", release / "venv")
    executable = release / "venv/bin/python"
    run(executable, "-m", "pip", "install", "--only-binary=:all:", "setuptools", "wheel")
    run(executable, "-m", "pip", "install", "--only-binary=:all:", "-r", code / "requirements.lock")
    run(executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", code)
    run(executable, "-m", "pip", "check")
    marker.write_text(sha + "\n")
    return release


def activate(base, release, url, ci_url):
    catalog = base / "shared/catalog.sqlite3"
    if busy(catalog):
        print("Deployment deferred: a task or model request is active.")
        return False
    current = base / "current"
    previous = current.resolve() if current.is_symlink() else None
    # Stop the API first. systemd lets existing HTTP requests finish before
    # stopping it; recheck afterwards so newly accepted work is never discarded.
    try:
        systemctl("stop", [UNITS[0]])
    except BaseException:
        systemctl("start", [UNITS[0]])
        raise
    if busy(catalog):
        systemctl("start", [UNITS[0]])
        print("Deployment deferred: work arrived before the API stopped.")
        return False
    try:
        systemctl("stop", [UNITS[1]])
        if catalog.exists():
            backup = base / "shared/backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3")
            backup.parent.mkdir(exist_ok=True)
            with sqlite3.connect(catalog) as source, sqlite3.connect(backup) as target:
                source.backup(target)
        point_current(base, release)
        systemctl("start")
        health(url)
        systemctl("is-active")
    except BaseException:
        systemctl("stop")
        if previous:
            point_current(base, previous)
            systemctl("start")
        else:
            current.unlink(missing_ok=True)
        raise
    (base / "deployed.json").write_text(json.dumps({
        "commit": release.name, "previous": previous.name if previous else None,
        "ci_url": ci_url, "deployed_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n")
    print("Deployed " + release.name)
    return True


@contextmanager
def deployment_lock(base):
    with (base / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--repository", default="DuscWalk/Big-Data-Analysis-2026")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--health-url", default="http://127.0.0.1:8765/api/v1/status")
    parser.add_argument("--apply", action="store_true", help="Prepare and activate; otherwise check CI only")
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("Run as the dedicated application user, not root.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Invalid GitHub repository name.")
    os.umask(0o077)
    base = args.base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    with deployment_lock(base):
        endpoint = "repos/" + args.repository
        ref = github(endpoint + "/git/ref/heads/" + quote(args.branch, safe="/"))
        sha = ref["object"]["sha"]
        if not SHA.fullmatch(sha):
            raise ValueError("Invalid commit SHA from GitHub.")
        current = base / "current"
        if current.is_symlink() and current.resolve().name == sha:
            print("Already deployed " + sha)
            return
        query = urlencode({"branch": args.branch, "head_sha": sha, "event": "push", "per_page": 10})
        runs = github(endpoint + "/actions/workflows/ci.yml/runs?" + query)["workflow_runs"]
        passed = successful_run(runs, sha, args.branch, args.repository)
        if not passed:
            print("No successful push CI for branch head; deployment unchanged.")
            return
        print("CI passed: " + passed["html_url"])
        if args.apply:
            memory = {line.split(":")[0]: int(line.split()[1]) for line in
                      Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemTotal:", "MemAvailable:"))}
            if memory["MemTotal"] < 7 * 1024 * 1024 or memory["MemAvailable"] < 512 * 1024:
                raise RuntimeError("Complete deployment needs at least 7 GiB total and 512 MiB available memory.")
            if not (base / "shared/hadoop/runtime.json").is_file():
                raise RuntimeError("Initialize the dedicated Hadoop runtime before enabling deployment.")
            if busy(base / "shared/catalog.sqlite3"):
                print("Deployment deferred: application busy.")
                return
            systemctl("is-active", ["movielens-hadoop.service"])
            release = prepare(base, sha, args.repository, args.python)
            # A newer branch head is handled by the next poll, never guessed.
            activate(base, release, args.health_url, passed["html_url"])


if __name__ == "__main__":
    main()
