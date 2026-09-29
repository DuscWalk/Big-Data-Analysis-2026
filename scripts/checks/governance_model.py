"""Opt-in live model acceptance using an isolated catalog; never starts a worker."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from agent_fixture import AgentFixture
from movielens_agent.agent.preferences import ModelPreferences
from movielens_agent.agent.service import AgentService
from movielens_agent.agent.settings import Settings
from movielens_agent.contracts import ArtifactRef


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--preferences-catalog", type=Path, default=Path("var/catalog.sqlite3"))
    parser.add_argument("--provider", choices=("primary", "backup"), default="backup")
    args = parser.parse_args()
    source = ModelPreferences(Settings.load(args.env_file, catalog=args.preferences_catalog)).snapshot()
    if not source.provider_configured(args.provider):
        raise SystemExit("Selected model provider is not configured.")
    args.output.mkdir(parents=True, exist_ok=False)
    f = AgentFixture(args.output.resolve())
    settings = source.model_copy(update={"catalog": f.db, "default_dataset_version": f.ref["version"],
                                        "model_provider": args.provider})
    agent = AgentService(settings, f.chats, f.tasks, f.catalog, f.config)
    result = {"live_model": True, "provider": args.provider,
              "model": settings.for_provider(args.provider).model_name, "cases": []}

    def save():
        (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    def request(key, content):
        started = time.monotonic()
        reply = agent.respond(f.session, key, content)
        calls = f.chats.calls(f.session, reply["message_id"])
        record = {"case": key, "status": reply["status"], "duration_seconds": round(time.monotonic() - started, 2),
                  "tools": [{"name": c["tool_name"], "status": c["status"]} for c in calls],
                  "configuration_change": reply.get("configuration_change"), "error": reply.get("error")}
        result["cases"].append(record)
        save()
        print(json.dumps(record, ensure_ascii=False), flush=True)
        assert reply["status"] == "completed", "Live model request failed; see the saved result."
        return reply

    try:
        first = request("configure", "把当前方案的时效窗口改为 180 天，用户、电影、评分表权重设为 1:1:3，"
                        "打开非典型邮编隔离。保存为「自然语言 180 天方案」并设为默认；其他参数不变，不要运行清洗。")
        saved = agent.configurations.get(ArtifactRef.model_validate(agent.configurations.default_ref))
        c = saved["configuration"]
        assert c["rules"]["quarantine_zip_warnings"] is True
        assert c["metrics"]["window_seconds"] == 180 * 86400
        assert c["metrics"]["table_weights"] == {"users": 1, "movies": 1, "ratings": 3}
        assert c["split"] == f.config.split.model_dump()
        assert first["configuration_change"]["default_changed"] is True
        with f.tasks.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        default_ref = agent.configurations.default_ref
        second = request("configure-and-run", "在当前默认方案基础上，只把窗口改为 90 天，保存为「本次 90 天方案」并清洗已登记数据。不要改变默认方案。")
        task = f.tasks.get(second["task_ids"][-1], f.session)
        assert task["status"] == "queued"
        assert task["payload"]["configuration"]["metrics"]["window_seconds"] == 90 * 86400
        assert task["payload"]["configuration"]["rules"]["quarantine_zip_warnings"] is True
        assert task["payload"]["configuration_ref"] == second["configuration_change"]["ref"]
        assert agent.configurations.default_ref == default_ref
        result.update(passed=True, no_worker_started=True, queued_tasks=1)
    except Exception as error:
        result.update(passed=False, failure_type=type(error).__name__)
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
