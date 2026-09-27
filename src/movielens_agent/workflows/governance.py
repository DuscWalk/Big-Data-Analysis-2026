"""Seven real MapReduce jobs behind a reusable task workflow."""
import base64
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import time

from ..adapters.hadoop import Hadoop
from ..contracts import DatasetManifest
from ..governance.config import GovernanceConfig, canonical, digest
from ..governance.report import render_report, LIMITATIONS
from ..jobs.worker import ExternalStateUnknown
from ..jobs.progress import metric
from ..storage.tasks import file_digest


def implementation_digest():
    package = Path(__file__).resolve().parents[1]
    return digest({str(path.relative_to(package)): file_digest(path)
                   for path in sorted(package.rglob("*.py"))})


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path, on_progress=None):
    total, consumed, last = Path(path).stat().st_size, 0, time.monotonic()
    with Path(path).open("rb") as stream:
        for index, line in enumerate(stream, 1):
            yield json.loads(line)
            consumed += len(line)
            if on_progress and index % 10000 == 0 and time.monotonic() - last >= 0.5:
                on_progress(consumed, total)
                last = time.monotonic()
    if on_progress:
        on_progress(consumed, total)


def package_input(manifest, directory, on_progress=None):
    source = Path(manifest.source_directory).resolve()
    if directory.resolve().is_relative_to(source):
        raise ValueError("Task output must be outside the source dataset.")
    if {table.name for table in manifest.tables} != {"users", "movies", "ratings"} or len(manifest.tables) != 3:
        raise ValueError("Exactly three input tables are required.")
    snapshots, outputs = {}, {}
    total, completed, last = sum(table.rows for table in manifest.tables), 0, time.monotonic()
    if on_progress:
        on_progress(0, total)
    for table in manifest.tables:
        if table.file_name != table.name + ".dat":
            raise ValueError("Unexpected raw dataset file name.")
        path = source / table.file_name
        snapshots[path] = path.stat()
        checksum, rows, offset = hashlib.sha256(), 0, 0
        target = directory / (table.name + ".jsonl")
        with path.open("rb") as original, target.open("x", encoding="utf-8") as wrapped:
            for rows, raw in enumerate(original, 1):
                checksum.update(raw)
                wrapped.write(canonical({
                    "kind": "raw", "table": table.name,
                    "source_ref": {"dataset_ref": manifest.ref.model_dump(),
                                   "file_name": table.file_name, "byte_offset": offset, "line": rows},
                    "raw_b64": base64.b64encode(raw).decode("ascii"),
                }) + "\n")
                offset += len(raw)
                if on_progress and rows % 10000 == 0 and time.monotonic() - last >= 0.5:
                    if completed + rows > total:
                        raise ValueError("Registered source content changed; register a new version.")
                    on_progress(completed + rows, total)
                    last = time.monotonic()
        if checksum.hexdigest() != table.sha256 or offset != table.size_bytes or rows != table.rows:
            raise ValueError(f"Registered source content changed: {table.file_name}; register a new version.")
        outputs[table.name] = target
        completed += rows
        if on_progress:
            on_progress(completed, total)
    for path, old in snapshots.items():
        new = path.stat()
        if (new.st_ino, new.st_size, new.st_mtime_ns, new.st_ctime_ns) != (
                old.st_ino, old.st_size, old.st_mtime_ns, old.st_ctime_ns):
            raise ValueError("Source changed while input was being packaged.")
    return outputs


def parent_index(path, destination):
    index = {"users": {}, "movies": {}}
    for row in read_jsonl(path):
        if row["kind"] == "parent":
            key = str(row["id"])
            if key in index[row["table"]]:
                raise ValueError("Hadoop emitted more than one index entry for the same parent.")
            index[row["table"]][key] = {"conflict": row["conflict"]}
    write_json(destination, index)


def file_entry(path):
    path = Path(path).resolve()
    return {"name": path.name, "path": str(path), "size_bytes": path.stat().st_size,
            "sha256": file_digest(path)}


def make_artifact(task_id, name, kind, paths, summary):
    files = [file_entry(path) for path in paths]
    version = digest({"kind": kind, "summary": summary,
                      "files": [{key: file[key] for key in ("name", "size_bytes", "sha256")} for file in files]})
    return {"ref": {"artifact_id": f"{task_id}.{name}", "version": version},
            "schema_version": "1", "kind": kind, "state": "ready",
            "producer_task_id": task_id, "files": files, "summary": summary}


class GovernanceWorkflow:
    def __init__(self, store, hadoop: Hadoop, run_root):
        self.store, self.hadoop, self.run_root = store, hadoop, Path(run_root).resolve()

    def __call__(self, task):
        task_id, payload = task["task_id"], task["payload"]
        manifest = DatasetManifest.model_validate(payload["source_manifest"])
        config = GovernanceConfig.model_validate(payload["configuration"])
        if config.refs() != payload["config_refs"]:
            raise ValueError("Configuration version mismatch.")
        code_version = implementation_digest()
        if code_version != payload["implementation_sha256"]:
            raise ValueError("Implementation changed after submission; submit a new request.")
        root = self.run_root / task_id
        if root.is_relative_to(Path(manifest.source_directory).resolve()):
            raise ValueError("Task output must be outside the source dataset.")
        root.mkdir(parents=True, exist_ok=False)
        for name in ("input", "results", "logs", "published"):
            (root / name).mkdir()
        write_json(root / "configuration.json", config.model_dump(mode="json"))
        shutil.copyfile(Path(__file__).parents[1] / "governance/streaming.py", root / "streaming.py")
        remote = self.hadoop.runtime.hdfs_root.rstrip("/") + "/tasks/" + task_id
        jobs = []

        @contextmanager
        def stage(name):
            log = root / "logs" / (name + ".log")
            sequence = self.store.start_stage(task_id, name, log)
            try:
                yield sequence, log
            except BaseException as error:
                unknown = isinstance(error, (ExternalStateUnknown, KeyboardInterrupt, SystemExit))
                self.store.end_stage(task_id, sequence, "unknown" if unknown else "failed", str(error))
                raise
            else:
                self.store.end_stage(task_id, sequence)

        with stage("prepare-inputs") as (sequence, log):
            total_rows = sum(table.rows for table in manifest.tables)
            def input_progress(completed, total):
                self.store.update_progress(task_id, sequence, message="正在核对并封装原始数据", metrics=[
                    metric("packaged", "已封装数据", completed, total, "rows"),
                    metric("uploaded", "已上传输入", 0, 3, "files")])
            sources = package_input(manifest, root / "input", on_progress=input_progress)
            self.store.update_progress(task_id, sequence, message="正在上传输入至 HDFS", metrics=[
                metric("packaged", "已封装数据", total_rows, total_rows, "rows"),
                metric("uploaded", "已上传输入", 0, len(sources), "files")])
            self.hadoop.fs("-mkdir", "-p", remote + "/input")
            for count, (table, path) in enumerate(sources.items(), 1):
                self.hadoop.put(path, remote + "/input/" + table + ".jsonl")
                self.store.update_progress(task_id, sequence, message="正在上传输入至 HDFS", metrics=[
                    metric("packaged", "已封装数据", total_rows, total_rows, "rows"),
                    metric("uploaded", "已上传输入", count, len(sources), "files")])
            write_json(log, {"packaged_rows": {t.name: t.rows for t in manifest.tables},
                             "hdfs_directory": remote + "/input"})

        def run(name, inputs, mode="score", parents=None, raw_parents=None, aggregate=False, reducers=1):
            files = {"streaming.py": root / "streaming.py",
                     "configuration.json": root / "configuration.json"}
            python = str(self.hadoop.runtime.python)
            mapper = [python, "streaming.py", "aggregate-map" if aggregate else "map"]
            reducer = [python, "streaming.py", "aggregate-reduce" if aggregate else "reduce"]
            if not aggregate:
                mapper += ["--mode", mode]
                reducer += ["--mode", mode]
                for flag, path, alias in (("--parents", parents, "parents.json"),
                                         ("--raw-parents", raw_parents, "raw-parents.json")):
                    if path:
                        files[alias] = path
                        mapper += [flag, alias]
            output = remote + "/" + name
            local = root / "results" / (name + ".jsonl")
            with stage(name) as (sequence, log):
                observed = []
                self.store.update_progress(task_id, sequence, message="正在提交作业，等待 Hadoop 上报进度")
                def job_progress(mapped, reduced):
                    observed[:] = [metric("map", "Map", mapped, 100, "percent"),
                                   metric("reduce", "Reduce", reduced, 100, "percent")]
                    self.store.update_progress(task_id, sequence, message="Hadoop 作业处理中", metrics=observed)
                ids = self.hadoop.run_job(
                    task_id=task_id, stage=name, inputs=inputs, output=output,
                    files=files, mapper=mapper, reducer=reducer, reducers=reducers,
                    log_path=log, on_identifier=lambda value: self.store.external_id(task_id, sequence, value),
                    on_progress=job_progress)
                self.store.update_progress(task_id, sequence, message="作业成功，正在下载结果", metrics=observed)
                self.hadoop.fetch(output, local)
                jobs.append({"stage": name, "external_ids": ids, "log_path": str(log),
                             "hdfs_output": output})
            return output, local

        raw_parents_remote, raw_parents_output = run("before-parents",
            [remote + "/input/users.jsonl", remote + "/input/movies.jsonl"])
        raw_parent_file = root / "raw-parents.json"
        parent_index(raw_parents_output, raw_parent_file)
        raw_ratings_remote, _ = run("before-ratings", [remote + "/input/ratings.jsonl"],
                                   parents=raw_parent_file, reducers=2)
        _, before_path = run("before-metrics", [raw_parents_remote, raw_ratings_remote], aggregate=True)
        clean_parents_remote, clean_parents_output = run("clean-parents",
            [remote + "/input/users.jsonl", remote + "/input/movies.jsonl"], mode="clean")
        clean_parent_file = root / "clean-parents.json"
        parent_index(clean_parents_output, clean_parent_file)
        clean_ratings_remote, clean_ratings_output = run("clean-ratings",
            [remote + "/input/ratings.jsonl"], mode="clean", parents=clean_parent_file,
            raw_parents=raw_parent_file, reducers=2)
        after_groups_remote, _ = run("after-groups", [clean_parents_remote, clean_ratings_remote],
                                    parents=clean_parent_file, reducers=2)
        _, after_path = run("after-metrics",
            [after_groups_remote, clean_parents_remote, clean_ratings_remote], aggregate=True)

        with stage("verify-and-export") as (sequence, log):
            export_total = sum(path.stat().st_size for path in (clean_parents_output, clean_ratings_output))
            def export_progress(completed, message="正在核对并导出记录", uploaded=0):
                self.store.update_progress(task_id, sequence, message=message, metrics=[
                    metric("exported", "已读取处理结果", completed, export_total, "bytes"),
                    metric("uploaded", "已上传清洗数据", uploaded, 3, "files")])
            export_progress(0, "正在检查指标与配置")
            before_items, after_items = list(read_jsonl(before_path)), list(read_jsonl(after_path))
            if len(before_items) != 1 or len(after_items) != 1:
                raise ValueError("Expected exactly one complete quality result per phase.")
            before, after = before_items[0], after_items[0]
            for quality in (before, after):
                if quality["configuration"] != config.model_dump(mode="json"):
                    raise ValueError("Hadoop used different metric/rule configurations.")
            published = root / "published"
            counts = Counter()
            dispositions = {table: Counter() for table in ("users", "movies", "ratings")}
            parents = {"users": set(), "movies": set()}
            streams = {table: (published / (table + ".jsonl")).open("x", encoding="utf-8")
                       for table in ("users", "movies", "ratings")}
            try:
                with (published / "dispositions.jsonl").open("x", encoding="utf-8") as history:
                    consumed = 0
                    for path in (clean_parents_output, clean_ratings_output):
                        for item in read_jsonl(path, on_progress=lambda current, total: export_progress(consumed + current)):
                            kind, table = item["kind"], item["table"]
                            if kind == "record":
                                data = item["data"]
                                if table == "ratings":
                                    if data["user_id"] not in parents["users"] or data["movie_id"] not in parents["movies"]:
                                        raise ValueError("Candidate output has dangling references.")
                                else:
                                    field = "user_id" if table == "users" else "movie_id"
                                    if data[field] in parents[table]:
                                        raise ValueError("Duplicate cleaned parent.")
                                    parents[table].add(data[field])
                                streams[table].write(canonical(data | {"source_ref": item["source_ref"]}) + "\n")
                                counts[table] += 1
                            elif kind == "disposition":
                                history.write(canonical(item) + "\n")
                                dispositions[table][item["disposition"]] += 1
                        consumed += path.stat().st_size
            finally:
                for stream in streams.values():
                    stream.close()
            export_progress(export_total, "正在核对行数守恒、外键和文件摘要")
            for table in manifest.tables:
                disp = after["dispositions"][table.name]
                output_rows = after["tables"][table.name]["rows"]
                if (before["tables"][table.name]["rows"] != table.rows
                    or counts[table.name] != output_rows
                    or dict(dispositions[table.name]) != disp
                    or table.rows != output_rows + disp.get("deduplicated", 0) + disp.get("quarantined", 0)
                    or output_rows != disp.get("kept", 0) + disp.get("repaired", 0)):
                    raise ValueError(f"Record conservation failed for {table.name}.")
                for dimension in ("Accurate", "Complete", "Unique", "Consistent"):
                    score = after["tables"][table.name]["metrics"][dimension]["score"]
                    if output_rows and score != 100:
                        raise ValueError(f"Cleaned data violates {dimension} for {table.name}.")
            if sum(after["splits"].values()) != counts["ratings"]:
                raise ValueError("Time split conservation failed.")
            dataset_summary = {
                "input_ref": manifest.ref.model_dump(), "config_refs": config.refs(),
                "configuration": config.model_dump(mode="json"), "encoding": "utf-8",
                "format": "jsonl", "rows": dict(counts), "splits": after["splits"],
                "split_materialization": "filter-by-timestamp", "hdfs_directory": remote + "/dataset",
                "implementation_sha256": code_version,
                "usable_for_training": all(counts[table] > 0 for table in streams),
            }
            cleaned = make_artifact(task_id, "cleaned", "cleaned_dataset",
                                    [published / (table + ".jsonl") for table in streams], dataset_summary)
            result = {
                "schema_version": "1", "task_id": task_id,
                "input_ref": manifest.ref.model_dump(), "cleaned_ref": cleaned["ref"],
                "config_refs": config.refs(), "configuration": config.model_dump(mode="json"),
                "implementation_sha256": code_version, "before": before, "after": after,
                "jobs": jobs, "limitations": LIMITATIONS,
            }
            write_json(published / "quality.json", result)
            write_json(published / "dataset-manifest.json", cleaned)
            (published / "report.md").write_text(render_report(result), encoding="utf-8")
            export_progress(export_total, "正在上传清洗数据至 HDFS")
            self.hadoop.fs("-mkdir", "-p", remote + "/dataset")
            for count, table in enumerate(streams, 1):
                self.hadoop.put(published / (table + ".jsonl"), remote + "/dataset/" + table + ".jsonl")
                export_progress(export_total, "正在上传清洗数据至 HDFS", uploaded=count)
            artifacts = [
                cleaned,
                make_artifact(task_id, "quality", "quality_report", [published / "quality.json"],
                              {"input_ref": manifest.ref.model_dump(), "cleaned_ref": cleaned["ref"]}),
                make_artifact(task_id, "dispositions", "disposition_log", [published / "dispositions.jsonl"],
                              {"counts": after["dispositions"], "input_ref": manifest.ref.model_dump()}),
                make_artifact(task_id, "report", "report",
                              [published / "report.md", published / "dataset-manifest.json"],
                              {"cleaned_ref": cleaned["ref"]}),
            ]
            for path in published.iterdir():
                path.chmod(0o444)
            write_json(log, {"conservation": "passed", "foreign_keys": "passed", "rows": dict(counts)})
            export_progress(export_total, "核对与导出完成", uploaded=3)
        return artifacts
