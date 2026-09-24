"""Dataset profiling, registered tools and the independent workflow worker."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from uuid import uuid4

from .contracts import DatasetManifest, ToolContext
from .governance.profile import profile_dataset
from .storage.catalog import DatasetCatalog
from .tools.datasets import register_dataset_tools
from .tools.registry import ToolRegistry
from .adapters.hadoop import Hadoop, HadoopRuntime
from .governance.config import GovernanceConfig
from .jobs.worker import Worker
from .storage.tasks import TaskStore
from .tools.governance import register_governance_tools
from .workflows.governance import GovernanceWorkflow


def _outside_source(target: Path, source: Path) -> None:
    if target.resolve().is_relative_to(source.resolve()):
        raise ValueError("Generated outputs and the catalog must be outside the source dataset directory.")


def save_profile(data_dir: Path, output_dir: Path, catalog_path: Path,
                 sample_limit: int = 3) -> dict:
    _outside_source(output_dir, data_dir)
    _outside_source(catalog_path, data_dir)
    if output_dir.exists():
        raise FileExistsError("Output directory already exists; select a new run directory.")
    profile = profile_dataset(data_dir, sample_limit=sample_limit,
                              progress=lambda message: print(message, file=sys.stderr, flush=True))
    manifest = DatasetManifest.model_validate(profile["manifest"])
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".profile-", dir=output_dir.parent))
    try:
        for name, value in (("profile.json", profile), ("manifest.json", profile["manifest"])):
            (temporary / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
        if output_dir.exists():
            raise FileExistsError("Output directory appeared during profiling; refusing to replace it.")
        os.rename(temporary, output_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    DatasetCatalog(catalog_path).register(manifest)
    return {"profile_path": str(output_dir / "profile.json"),
            "manifest_path": str(output_dir / "manifest.json"),
            "catalog_path": str(catalog_path), "dataset_ref": manifest.ref.model_dump(),
            "rows": {table.name: table.rows for table in manifest.tables}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    profile = commands.add_parser("profile", help="Read all source files and register their exact version")
    profile.add_argument("--data-dir", type=Path, default=Path("ml-1m"))
    profile.add_argument("--output-dir", type=Path)
    profile.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    profile.add_argument("--sample-limit", type=int, default=3)
    describe = commands.add_parser("describe", help="Invoke datasets.describe through the tool registry")
    describe.add_argument("--artifact-id", default="ml-1m.raw")
    describe.add_argument("--version", required=True)
    describe.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))

    submit = commands.add_parser("submit", help="Queue governance.run through the common tool registry")
    submit.add_argument("--artifact-id", default="ml-1m.raw")
    submit.add_argument("--version", required=True)
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--session-id", default="local-cli")
    submit.add_argument("--config", type=Path, default=Path("configs/governance/default.json"))
    submit.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    worker = commands.add_parser("worker", help="Run the independent durable task worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    worker.add_argument("--runtime", type=Path, default=Path("var/hadoop/runtime.json"))
    worker.add_argument("--run-root", type=Path, default=Path("var/runs"))
    task = commands.add_parser("task", help="Inspect persisted state, attempts and output references")
    task.add_argument("--task-id", required=True)
    task.add_argument("--session-id", default="local-cli")
    task.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    task.add_argument("--external", action="store_true", help="Also query current YARN states, without resubmitting")
    task.add_argument("--runtime", type=Path, default=Path("var/hadoop/runtime.json"))
    evidence = commands.add_parser("evidence", help="Read published evidence through artifacts.get")
    evidence.add_argument("--artifact-id", required=True)
    evidence.add_argument("--version", required=True)
    evidence.add_argument("--session-id", default="local-cli")
    evidence.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    evidence.add_argument("--config", type=Path, default=Path("configs/governance/default.json"))
    evidence.add_argument("--mode", choices=("metadata", "summary", "sample", "examples"), default="metadata")
    evidence.add_argument("--file-name")
    evidence.add_argument("--reason")
    evidence.add_argument("--limit", type=int, default=3)
    evidence.add_argument("--offset", type=int, default=0)
    serve = commands.add_parser("serve", help="Serve the local conversation UI and API")
    serve.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--env-file", type=Path, default=Path(".env"))
    serve.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    serve.add_argument("--config", type=Path, default=Path("configs/governance/default.json"))
    probe = commands.add_parser("model-probe", help="Check model discovery or native tool calling, without printing secrets")
    probe.add_argument("--env-file", type=Path, default=Path(".env"))
    probe.add_argument("--provider", choices=("auto", "primary", "backup"), default="auto")
    probe.add_argument("--list-models", action="store_true")
    session = commands.add_parser("session", help="Create a local conversation, optionally for an existing CLI session ID")
    session.add_argument("--session-id")
    session.add_argument("--title", default="本地实验")
    session.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    args = parser.parse_args(argv)
    # Configuration validation can include sensitive input in third-party errors;
    # these commands report only the exception type, never raw exception text.
    if args.command in ("serve", "model-probe"):
        try:
            from .agent.settings import Settings
            if args.command == "serve":
                import uvicorn
                from .api.app import create_app
                settings = Settings.load(args.env_file, catalog=args.catalog, governance_config=args.config)
                uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
                return 0
            from .agent.probe import list_models, tool_probe
            settings = Settings.load(args.env_file, model_provider=args.provider)
            result = list_models(settings) if args.list_models else tool_probe(settings)
            print(json.dumps(settings.redact(result), ensure_ascii=False, indent=2))
            if args.list_models:
                return 0 if any(p.get("http_status") == 200 for p in result["providers"]) else 1
            return 0 if result["status"] == "completed" else 1
        except Exception as error:
            print(f"Application setup failed ({type(error).__name__}); inspect configuration locally.", file=sys.stderr)
            return 1
    try:
        if args.command == "session":
            from .storage.conversations import ConversationStore
            store = ConversationStore(args.catalog)
            store.initialize()
            print(json.dumps(store.create_session(args.title, args.session_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "profile":
            output = args.output_dir or Path("var/profiles") / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
            result = save_profile(args.data_dir, output, args.catalog, args.sample_limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "worker":
            store = TaskStore(args.catalog)
            hadoop = Hadoop(HadoopRuntime.read(args.runtime))
            Worker(store, {"governance.v1": GovernanceWorkflow(store, hadoop, args.run_root)}).run(once=args.once)
            return 0
        if args.command == "task":
            value = TaskStore(args.catalog).get(args.task_id, args.session_id)
            if args.external:
                hadoop = Hadoop(HadoopRuntime.read(args.runtime))
                value["external_status"] = [
                    hadoop.application_status(identifier) for attempt in value["attempts"]
                    for identifier in attempt["external_ids"] if identifier.startswith("application_")]
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        registry = ToolRegistry()
        register_dataset_tools(registry, DatasetCatalog(args.catalog))

        if args.command in ("submit", "evidence"):
            register_governance_tools(registry, DatasetCatalog(args.catalog), TaskStore(args.catalog),
                                      GovernanceConfig.read(args.config))
            context = ToolContext(session_id=args.session_id, call_id=uuid4().hex,
                                  request_id=args.request_id if args.command == "submit" else None)
            if args.command == "submit":
                result = registry.call("governance.run", {"dataset_ref": {
                    "artifact_id": args.artifact_id, "version": args.version}}, context)
            else:
                result = registry.call("artifacts.get", {"artifact_ref": {
                    "artifact_id": args.artifact_id, "version": args.version},
                    "mode": args.mode, "file_name": args.file_name, "limit": args.limit,
                    "offset": args.offset, "reason": args.reason}, context)
            print(result.model_dump_json(indent=2))
            return 0 if result.status in ("completed", "accepted") else 1
        result = registry.call("datasets.describe", {"dataset_ref": {
            "artifact_id": args.artifact_id, "version": args.version}},
            ToolContext(session_id="local-cli", call_id=uuid4().hex))
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "completed" else 1
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
