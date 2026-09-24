"""Small query-tool registry; job tools and session persistence follow later."""
import logging
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from ..contracts import ArtifactRef, QueryResult, ToolContext, ToolError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryTool:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], tuple[BaseModel, list[ArtifactRef]]]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, QueryTool] = {}

    def register(self, tool: QueryTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def describe(self) -> list[dict]:
        return [
            {"name": tool.name, "tool_version": tool.version,
             "description": tool.description, "mode": "query",
             "input_schema": tool.input_model.model_json_schema(),
             "output_schema": tool.output_model.model_json_schema()}
            for tool in self._tools.values()
        ]

    def call(self, name: str, arguments: dict, context: ToolContext) -> QueryResult:
        def error(status, code, message):
            return QueryResult(call_id=context.call_id, status=status,
                               error=ToolError(code=code, message=message))
        if name not in self._tools:
            return error("rejected", "TOOL_NOT_FOUND", "Tool is not registered.")
        tool = self._tools[name]
        try:
            parsed = tool.input_model.model_validate(arguments)
        except ValidationError:
            return error("rejected", "INVALID_ARGUMENTS", "Arguments do not match the tool schema.")
        try:
            value, evidence = tool.handler(parsed, context)
            output = tool.output_model.model_validate(value)
            return QueryResult(call_id=context.call_id, status="completed",
                               data=output.model_dump(mode="json"), evidence=evidence)
        except KeyError:
            return error("rejected", "ARTIFACT_NOT_FOUND", "The exact dataset version is not registered.")
        except Exception:
            logger.exception("Query tool failed: %s, call_id=%s", name, context.call_id)
            return error("failed", "TOOL_EXECUTION_FAILED", "Query failed; inspect the local execution log.")
