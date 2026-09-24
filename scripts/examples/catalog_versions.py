"""Live extension example: register a read-only query without changing the dispatcher.

This explicitly calls the configured model; it does not submit Hadoop tasks.
The tool is registered only in this demonstration process.
"""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from movielens_agent.agent.model import tool_schemas
from movielens_agent.agent.service import AgentService
from movielens_agent.agent.settings import Settings
from movielens_agent.contracts import ArtifactRef, Contract
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.storage.catalog import DatasetCatalog
from movielens_agent.storage.conversations import ConversationStore
from movielens_agent.storage.tasks import TaskStore
from movielens_agent.tools.registry import QueryTool


class VersionsInput(Contract):
    artifact_id: str = Field(default="ml-1m.raw", min_length=1)


class VersionsOutput(Contract):
    count: int
    versions: list[ArtifactRef]


def register_catalog_versions(registry, catalog):
    def read(arguments, context):
        refs = catalog.versions(arguments.artifact_id)
        return VersionsOutput(count=len(refs), versions=refs), refs
    registry.register(QueryTool("datasets.versions", "1", "List exact registered raw dataset versions. Read-only.",
                                VersionsInput, VersionsOutput, read))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("var/verification/extension.json"))
    args = parser.parse_args()
    settings = Settings.load(catalog=args.catalog)
    chats = ConversationStore(settings.catalog)
    chats.initialize()
    session = chats.create_session("真实查询工具扩展验证")["session_id"]
    catalog = DatasetCatalog(settings.catalog)
    agent = AgentService(settings, chats, TaskStore(settings.catalog), catalog,
                         GovernanceConfig.read(settings.governance_config))
    register_catalog_versions(agent.registry, catalog)
    # Snapshot schemas after registration; dispatch and storage are unchanged.
    agent.aliases, agent.definitions = tool_schemas(agent.registry.describe())
    response = agent.respond(session, uuid4().hex,
        "这次仅调用 datasets_versions 查询已登记的 ml-1m.raw 版本清单，展示数量与完整精确版本，不提交治理任务。")
    calls = chats.calls(session, response["message_id"])
    passed = response["status"] == "completed" and any(
        c["tool_name"] == "datasets.versions" and c["status"] == "completed" for c in calls)
    result = settings.redact({"passed": passed, "session_id": session, "response": response,
                              "calls": calls, "model_calls": chats.model_calls(session, response["message_id"])})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": passed, "session_id": session, "tools": response["tool_calls"],
                      "result_path": str(args.output)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Extension check failed (" + type(error).__name__ + "); inspect the local records.")
        raise SystemExit(1)
