"""Job submission and scoped evidence queries use the same tool registry."""
import base64
from itertools import islice
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..contracts import ArtifactRef, Contract
from ..governance.config import GovernanceConfig
from ..storage.tasks import RequestConflict, file_digest
from ..workflows.governance import implementation_digest
from .registry import QueryTool, ToolRejected


class RunInput(Contract):
    dataset_ref: ArtifactRef
    rule_ref: ArtifactRef | None = None
    metric_ref: ArtifactRef | None = None


class Receipt(Contract):
    task_id: str
    created: bool


class TaskInput(Contract):
    task_id: str = Field(min_length=1)


class ObjectResult(Contract):
    value: dict[str, Any]


class ListInput(TaskInput):
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=20, ge=1, le=100)


class ArtifactInput(Contract):
    artifact_ref: ArtifactRef
    mode: Literal["metadata", "summary", "sample", "examples"] = Field(default="metadata",
        description="metadata: file manifest; summary: metrics/report; sample: clean JSONL rows; examples: bounded source issues.")
    file_name: str | None = Field(default=None, description="Exact registered filename; required for multi-file artifacts.")
    reason: str | None = Field(default=None, max_length=120,
        description="Only for examples: a table/rule key such as ratings/R14_VALID_KEY_CONFLICT, not a call rationale.")
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=3, ge=1, le=20)


def register_governance_tools(registry, catalog, store, config: GovernanceConfig):
    refs = config.refs()

    def submit(arguments, context):
        if not context.request_id:
            raise ToolRejected("REQUEST_ID_REQUIRED", "Job calls require an application request identifier.")
        for supplied, name in ((arguments.rule_ref, "rules"), (arguments.metric_ref, "metrics")):
            if supplied and supplied.model_dump() != refs[name]:
                raise ToolRejected("CONFIG_NOT_FOUND", "Only exact registered configurations may be used.")
        manifest = catalog.get(arguments.dataset_ref)
        if store.path.resolve().is_relative_to(Path(manifest.source_directory).resolve()):
            raise ToolRejected("INVALID_STORAGE", "Task metadata must be outside the source dataset.")
        payload = {
            "source_manifest": manifest.model_dump(mode="json"),
            "configuration": config.model_dump(mode="json"), "config_refs": refs,
            "implementation_sha256": implementation_digest(),
        }
        try:
            task_id, created = store.submit(context.session_id, context.request_id, "governance.v1", payload)
        except RequestConflict as error:
            raise ToolRejected("REQUEST_CONFLICT", str(error)) from error
        return Receipt(task_id=task_id, created=created), [manifest.ref]

    def task_get(arguments, context):
        value = store.get(arguments.task_id, context.session_id)
        # Execution paths and the full input manifest are available in the local
        # administration CLI, not needed in normal model context.
        view = {key: value[key] for key in ("task_id", "status", "stage", "error", "created_at", "updated_at")}
        view["attempts"] = [{key: attempt[key] for key in ("stage", "status", "external_ids", "error")}
                            for attempt in value["attempts"]]
        view["artifacts"] = [{"ref": item["ref"], "kind": item["kind"],
                              "file_names": [file["name"] for file in item["files"]]} for item in value["artifacts"]]
        return ObjectResult(value=view), [ArtifactRef.model_validate(item["ref"]) for item in value["artifacts"]]

    def artifact_list(arguments, context):
        task = store.get(arguments.task_id, context.session_id)
        values = task["artifacts"][arguments.offset:arguments.offset + arguments.limit]
        return ObjectResult(value={"items": values, "total": len(task["artifacts"])}), [
            ArtifactRef.model_validate(item["ref"]) for item in values]

    def artifact_get(arguments, context):
        artifact = store.artifact(arguments.artifact_ref, context.session_id)
        value = artifact
        if arguments.mode != "metadata":
            matching = [file for file in artifact["files"]
                        if arguments.file_name is None or file["name"] == arguments.file_name]
            if len(matching) != 1:
                raise ToolRejected("FILE_REQUIRED", "Select one of the artifact's registered files.")
            file = matching[0]
            path = Path(file["path"])
            if not path.is_file() or file_digest(path) != file["sha256"]:
                raise ToolRejected("ARTIFACT_INVALID", "Published file is missing or its checksum changed.")
            if arguments.mode == "examples":
                if artifact["kind"] != "quality_report":
                    raise ToolRejected("UNSUPPORTED_VIEW", "Examples require a quality report.")
                report = json.loads(path.read_text(encoding="utf-8"))
                groups = report["after"]["samples"]
                selected = groups.get(arguments.reason, []) if arguments.reason else [
                    item for entries in groups.values() for item in entries]
                examples, seen = [], set()
                for item in selected:
                    source = item["source_ref"]
                    key = source["file_name"], source["byte_offset"]
                    if key in seen:
                        continue
                    seen.add(key)
                    preview = base64.b64decode(item["raw_b64"] or "").decode("iso-8859-1").rstrip("\r\n")
                    examples.append({key: item[key] for key in (
                        "table", "source_ref", "disposition", "reasons", "warnings", "changes")} | {
                        "raw_preview": preview[:500], "preview_truncated": len(preview) > 500})
                value = {"items": examples[arguments.offset:arguments.offset + arguments.limit],
                         "available_examples": len(examples), "reason": arguments.reason,
                         "note": "Bounded representative examples, not all affected records."}
            elif arguments.mode == "sample":
                if path.suffix != ".jsonl":
                    raise ToolRejected("UNSUPPORTED_VIEW", "Sample mode requires a registered JSONL file.")
                with path.open(encoding="utf-8") as stream:
                    items = [json.loads(line) for line in islice(stream, arguments.offset,
                                                              arguments.offset + arguments.limit + 1)]
                value = {"items": items[:arguments.limit], "offset": arguments.offset,
                         "has_more": len(items) > arguments.limit, "sample_limit": arguments.limit}
            elif artifact["kind"] == "quality_report":
                report = json.loads(path.read_text(encoding="utf-8"))
                value = {key: report[key] for key in ("task_id", "input_ref", "cleaned_ref",
                         "config_refs", "configuration", "limitations")}
                value["before"] = {key: report["before"][key] for key in ("overall", "tables")}
                value["after"] = {key: report["after"][key] for key in
                                  ("overall", "tables", "dispositions", "reasons", "warnings", "splits")}
            elif path.suffix == ".md":
                if path.stat().st_size > 65536:
                    raise ToolRejected("CONTENT_TOO_LARGE", "Report exceeds the tool context limit.")
                value = {"markdown": path.read_text(encoding="utf-8")}
            else:
                value = artifact["summary"]
        return ObjectResult(value=value), [arguments.artifact_ref]

    for spec in (
        QueryTool("governance.run", "1", "Queue Hadoop before scoring, cleaning and after scoring.",
                  RunInput, Receipt, submit, mode="job"),
        QueryTool("tasks.get", "1", "Read a task visible in this conversation.",
                  TaskInput, ObjectResult, task_get),
        QueryTool("artifacts.list", "1", "List published artifacts of one task.",
                  ListInput, ObjectResult, artifact_list),
        QueryTool("artifacts.get", "1", "Read exact-version evidence or bounded source-linked samples.",
                  ArtifactInput, ObjectResult, artifact_get),
    ):
        registry.register(spec)
