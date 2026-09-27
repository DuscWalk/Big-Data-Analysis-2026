"""Portable, pinned readers for a published v1 cleaned dataset."""
import hashlib
import json
import os
from pathlib import Path

from ..contracts import ArtifactRef
from ..governance.config import GovernanceConfig, digest

TABLES = ("users", "movies", "ratings")
SPLITS = ("train", "validation", "test")


class CleanedDataset:
    def __init__(self, manifest_path, *, expected_ref: ArtifactRef, expected_task_id: str):
        self.directory = Path(manifest_path).resolve().parent
        self.manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        manifest = self.manifest
        if (manifest.get("schema_version") != "1" or manifest.get("state") != "ready"
            or manifest.get("kind") != "cleaned_dataset"
            or manifest.get("producer_task_id") != expected_task_id
            or manifest.get("ref") != expected_ref.model_dump()
            or expected_ref.artifact_id != expected_task_id + ".cleaned"):
            raise ValueError("The manifest does not match the pinned, published dataset.")
        self.files = {file["name"]: file for file in manifest["files"]}
        if len(manifest["files"]) != 3 or set(self.files) != {name + ".jsonl" for name in TABLES}:
            raise ValueError("Exactly three cleaned JSONL files are required.")
        identity = {"kind": manifest["kind"], "summary": manifest["summary"],
                    "files": [{key: file[key] for key in ("name", "size_bytes", "sha256")}
                              for file in manifest["files"]]}
        if digest(identity) != expected_ref.version:
            raise ValueError("The manifest content does not match the pinned version.")
        self.summary = manifest["summary"]
        self.config = GovernanceConfig.model_validate(self.summary["configuration"])
        if (self.config.refs() != self.summary["config_refs"]
            or self.summary["format"] != "jsonl" or self.summary["encoding"] != "utf-8"
            or self.summary["split_materialization"] != "filter-by-timestamp"):
            raise ValueError("Unsupported dataset configuration or storage format.")
        rows, splits = self.summary["rows"], self.summary["splits"]
        if (set(rows) != set(TABLES) or set(splits) != set(SPLITS)
            or any(type(v) is not int or v < 0 for v in [*rows.values(), *splits.values()])
            or sum(splits.values()) != rows["ratings"]):
            raise ValueError("Invalid row or partition counts in manifest.")

    def _path(self, table):
        if table not in TABLES:
            raise ValueError("Unknown cleaned table.")
        # Ignore producer-machine absolute paths. A copied manifest is portable
        # when its three data files are placed beside it.
        path = self.directory / (table + ".jsonl")
        if path.is_symlink() or not path.is_file():
            raise ValueError("A cleaned file is missing or is a symbolic link.")
        return path

    @staticmethod
    def _snapshot(stream):
        stat = os.fstat(stream.fileno())
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _verify_stream(self, table, stream):
        initial, checksum = self._snapshot(stream), hashlib.sha256()
        size = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
            size += len(block)
        file = self.files[table + ".jsonl"]
        if (size != file["size_bytes"] or checksum.hexdigest() != file["sha256"]
            or self._snapshot(stream) != initial):
            raise ValueError(f"Cleaned file changed: {table}.jsonl")
        stream.seek(0)
        return initial

    def verify(self):
        """Check all three file checksums before beginning downstream work."""
        for table in TABLES:
            with self._path(table).open("rb") as stream:
                self._verify_stream(table, stream)
        return self.manifest["ref"]

    def _rows(self, table):
        # Recheck on the SAME open file before yielding any row, including when
        # a consumer resumes using a dataset after an earlier verify().
        with self._path(table).open("rb") as stream:
            snapshot = self._verify_stream(table, stream)
            count = 0
            for count, line in enumerate(stream, 1):
                row = json.loads(line.decode("utf-8"))
                if row["source_ref"]["dataset_ref"] != self.summary["input_ref"]:
                    raise ValueError("Cleaned row has a different source version.")
                yield row
            if count != self.summary["rows"][table] or self._snapshot(stream) != snapshot:
                raise ValueError("Cleaned row count or file changed during reading.")

    def entities(self, table):
        if table not in ("users", "movies"):
            raise ValueError("Read ratings with an explicit train/validation/test split.")
        return self._rows(table)

    def ratings(self, split):
        """Stream one explicit temporal partition; never default to all rows."""
        if split not in SPLITS:
            raise ValueError("Choose train, validation or test explicitly.")
        if not self.summary["usable_for_training"]:
            raise ValueError("This dataset is not marked usable for training.")
        return self._ratings(split)

    def _ratings(self, split):
        count = 0
        for row in self._rows("ratings"):
            stamp = row["timestamp"]
            if type(stamp) is not int or not self.config.rules.timestamp_min <= stamp <= self.config.rules.timestamp_max:
                raise ValueError("Invalid cleaned event timestamp.")
            actual = ("train" if stamp <= self.config.split.train_end else
                      "validation" if stamp <= self.config.split.validation_end else "test")
            if actual == split:
                count += 1
                yield row
        if count != self.summary["splits"][split]:
            raise ValueError("Partition count differs from the pinned manifest.")
