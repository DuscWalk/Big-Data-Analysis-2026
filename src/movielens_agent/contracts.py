"""Versioned contracts shared by profiling, catalog storage and tools."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class ToolError(Contract):
    code: str
    message: str


class QueryResult(Contract):
    schema_version: Literal["1"] = "1"
    call_id: str
    status: Literal["completed", "rejected", "failed"]
    data: dict[str, Any] | None = None
    evidence: list[ArtifactRef] = Field(default_factory=list)
    error: ToolError | None = None
