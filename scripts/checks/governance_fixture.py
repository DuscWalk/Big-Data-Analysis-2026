"""Three-row browser fixture with scripted model responses; no cluster or network model."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from agent_fixture import AgentFixture, ScriptedModel, answer, call
from test_progress_workflow import InProcessHadoop
from movielens_agent.api.app import create_app
from movielens_agent.jobs.worker import Worker
from movielens_agent.workflows.governance import GovernanceWorkflow
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    fixture = AgentFixture(args.root.resolve())
    agent = fixture.agent([call("governance_run", {"dataset_ref": fixture.ref})])
    receipt = agent.respond(fixture.session, "baseline", "生成三行测试报告")
    task_id = receipt["task_ids"][-1]
    Worker(fixture.tasks, {"governance.v1": GovernanceWorkflow(
        fixture.tasks, InProcessHadoop(), fixture.root / "runs")}).run(once=True)
    assert fixture.tasks.get(task_id, fixture.session)["status"] == "succeeded"

    def configure(payload):
        return call("governance_configure", {"name": "对话 60 天方案", "metrics": {"window_days": 60},
            "set_default": True, "default_revision": agent.configurations.default()["revision"]})

    model = ScriptedModel(fixture.settings, [call("governance_run", {"dataset_ref": fixture.ref}),
                                           configure, answer("已保存 60 天窗口并设为默认，未提交清洗。")])
    metadata = {"test_double": True, "session_id": fixture.session, "task_id": task_id,
                "base_url": f"http://127.0.0.1:{args.port}", "default_ref": fixture.config.ref()}
    (args.root / "fixture.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata), flush=True)
    uvicorn.run(create_app(fixture.settings, model), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
