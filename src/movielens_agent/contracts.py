"""Versioned contracts shared by profiling, catalog storage and tools."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactRef(Contract):
    artifact_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class TableManifest(Contract):
    name: Literal["users", "movies", "ratings"]
    file_name: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    rows: int = Field(ge=0)
    fields: list[str]


class DatasetManifest(Contract):
    schema_version: Literal["1"] = "1"
    ref: ArtifactRef
    kind: Literal["raw_dataset"] = "raw_dataset"
    encoding: Literal["iso-8859-1"] = "iso-8859-1"
    delimiter: Literal["::"] = "::"
    has_header: Literal[False] = False
    source_directory: str
    tables: list[TableManifest]


class ToolContext(Contract):
    session_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    message_id: str | None = Field(default=None, min_length=1)
    request_id: str | None = Field(default=None, min_length=1)


class ToolError(Contract):
    code: str
    message: str


class QueryResult(Contract):
    schema_version: Literal["1"] = "1"
    call_id: str
    status: Literal["completed", "accepted", "rejected", "failed"]
    task_ref: dict[str, str] | None = None
    data: dict[str, Any] | None = None
    evidence: list[ArtifactRef] = Field(default_factory=list)
    error: ToolError | None = None

    @model_validator(mode="after")
    def valid_result(self):
        if self.status in ("rejected", "failed"):
            if self.error is None or self.data is not None or self.task_ref is not None:
                raise ValueError("Error results require an error and no result data.")
        elif self.error is not None:
            raise ValueError("Successful tool responses cannot carry errors.")
        elif self.status == "accepted":
            if not self.task_ref or set(self.task_ref) != {"task_id"} or self.data is not None:
                raise ValueError("Accepted tasks return a task reference, not final results.")
        elif self.data is None or self.task_ref is not None:
            raise ValueError("Completed queries require result data.")
        return self
