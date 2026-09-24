"""Development CLI for actual read-only profiles and registered query tools."""
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
    args = parser.parse_args(argv)
    try:
        if args.command == "profile":
            output = args.output_dir or Path("var/profiles") / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
            result = save_profile(args.data_dir, output, args.catalog, args.sample_limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        registry = ToolRegistry()
        register_dataset_tools(registry, DatasetCatalog(args.catalog))
        result = registry.call("datasets.describe", {"dataset_ref": {
            "artifact_id": args.artifact_id, "version": args.version}},
            ToolContext(session_id="local-cli", call_id=uuid4().hex))
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "completed" else 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
