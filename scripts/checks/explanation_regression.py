"""Opt-in live model regression against one existing published governance task.

No Hadoop worker is needed. Each question has a fresh durable request ID; this
checks the actual model and AgentService, not a scripted model or new pipeline.
"""
import argparse
import fcntl
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from movielens_agent.agent.explanation import FULL_SECTIONS
from movielens_agent.agent.service import AgentService
from movielens_agent.agent.settings import Settings
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.storage.catalog import DatasetCatalog
from movielens_agent.storage.conversations import ConversationStore
from movielens_agent.storage.tasks import TaskStore
from movielens_agent.workflows.governance import implementation_digest

CASES = [
    ("complete", "解释本任务五维前后变化、数据处置损失、时效性、时间边界与未能核验的问题。只读取已有结果，不重新清洗。", FULL_SECTIONS, True),
    ("overlap", "有人说父表引用问题合计 99000 行，而且评分减少都是去重造成的。请读取实际证据，给出真正的原因命中总和、评分隔离比例和去重行数，说明是否可直接相加。不要重跑任务。", {"parent_references", "rating_loss"}, False),
    ("units", "有人把时效性写成 20208 / 935354 × 100 = 216 分。请读取本任务实际分子分母，说明正确公式、分数、百分比和历史窗口。不要重新清洗。", {"freshness"}, False),
    ("partitions", "是否已经生成 train.jsonl、validation.jsonl 和 test.jsonl？请给出实际 T1/T2 的包含关系、分区数和存储状态，并说明能否先用所有评分拟合特征。只查询已有结果。", {"time_splits"}, False),
    ("unsupported", "四项满分是否证明用户年龄、电影名称等事实绝对真实，并保证下一轮分类准确率达到 99%？请依据本任务证据回答，不能验证的部分要明确说明，不要重跑。", set(), False),
    ("example", "请读取一个 ratings/R14_VALID_KEY_CONFLICT 的实际异常样例，保留原始行、处置结果和来源定位。只查看已有结果。", set(), False),
    ("concise", "请依据本任务证据解释：为什么清洗后四项质量达到 100 分，时效性却仍然很低？这些分数能否证明数据真实？请简短回答，不要重新清洗。", {"scores", "metric_method", "freshness", "limitations"}, False),
]


def run(args):
    settings = Settings.load(catalog=args.catalog)
    chats, tasks = ConversationStore(args.catalog), TaskStore(args.catalog)
    chats.initialize()
    chats.recover_interrupted_messages()
    chats.session(args.session_id)
    task = tasks.get(args.task_id, args.session_id)
    if task["status"] != "succeeded":
        raise ValueError("Select a published successful governance task.")
    reports = [item for item in task["artifacts"] if item["kind"] == "quality_report"]
    if len(reports) != 1:
        raise ValueError("Select a task with exactly one quality report.")
    agent = AgentService(settings, chats, tasks, DatasetCatalog(args.catalog),
                         GovernanceConfig.read(settings.governance_config))
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "result.json").exists():
        raise ValueError("Use a new output directory to preserve previous regression evidence.")
    if args.expected_provider and settings.model_provider != args.expected_provider:
        raise ValueError("The configured provider differs from --expected-provider.")
    started, run = time.monotonic(), uuid4().hex
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "run_id": run,
              "implementation_sha256": implementation_digest(), "session_id": args.session_id,
              "task_id": args.task_id, "quality_ref": reports[0]["ref"], "expected_provider": args.expected_provider,
              "repeat": args.repeat, "cases": []}
    with tasks.connect() as conn:
        task_count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    for repetition in range(1, args.repeat + 1):
        for name, question, required, full in CASES:
            if args.case and name not in args.case:
                continue
            case_started = time.monotonic()
            reply = agent.respond(args.session_id, "regression:" + run + ":" + str(repetition) + ":" + name,
                                  question, args.task_id, require_quality=full)
            validation = reply.get("validation", {})
            selected = set(validation.get("sections", []))
            tool_calls = chats.calls(args.session_id, reply["message_id"])
            model_calls = chats.model_calls(args.session_id, reply["message_id"])
            attempts = [a for c in reply.get("model_calls", []) for a in c.get("attempts", [])]
            checks = {"completed": reply["status"] == "completed", "rendered": reply["response_origin"] == "evidence_rendered",
                      "exact_report": validation.get("quality_ref") == reports[0]["ref"],
                      "required_sections": required.issubset(selected),
                      "current_summary": any(c["call_id"] == validation.get("summary_call_id")
                          and c["tool_name"] == "artifacts.get" and c["status"] == "completed"
                          and c["arguments"].get("mode") == "summary"
                          and c["arguments"].get("artifact_ref") == reports[0]["ref"] for c in tool_calls)}
            if args.expected_provider:
                checks["expected_provider"] = bool(attempts) and all(a["provider"] == args.expected_provider for a in attempts)
            if name == "unsupported":
                checks["unsupported_marked"] = validation.get("unsupported") is True
                checks["explains_proxy_limits"] = bool(selected & {"metric_method", "limitations", "scores"})
            if name == "example":
                keys = [key for key in selected if key.startswith("examples:")]
                checks["current_examples"] = len(keys) == 1
                calls = [c for c in tool_calls if c["call_id"] in [key.split(":", 1)[1] for key in keys]]
                checks["requested_rule_and_count"] = len(calls) == 1 and calls[0]["arguments"].get("reason") == "ratings/R14_VALID_KEY_CONFLICT" and calls[0]["arguments"].get("limit") == 1
                items = calls[0]["result"]["data"]["value"]["items"] if calls and calls[0]["status"] == "completed" else []
                checks["sample_in_answer"] = len(items) == 1 and items[0]["raw_preview"] in reply["content"] and all(
                    k in items[0]["source_ref"] for k in ["dataset_ref", "file_name", "line", "byte_offset"]) and "quarantined" in reply["content"]
            allowed = {
                "overlap": {"parent_references", "rating_loss", "dispositions"},
                "units": {"freshness"}, "partitions": {"time_splits"},
                "unsupported": {"scores", "metric_method", "limitations"},
                "concise": {"scores", "metric_method", "freshness", "limitations"},
            }
            if name in allowed:
                checks["focused_sections"] = bool(selected) and selected.issubset(allowed[name])
            if name == "example":
                checks["focused_sections"] = bool(selected) and all(key.startswith("examples:") for key in selected)
            if name == "concise":
                checks["brief_length"] = len(reply["content"]) <= 800
                checks["brief_scope"] = len(selected) <= 4
            usages = [(c.get("response") or {}).get("usage") or {} for c in model_calls]
            case = {"name": name, "repetition": repetition, "question": question,
                    "duration_seconds": round(time.monotonic() - case_started, 3),
                    "response": reply, "checks": checks, "tool_calls": tool_calls, "model_calls": model_calls,
                    "metrics": {"model_rounds": len(model_calls), "answer_characters": len(reply["content"]),
                        "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usages),
                        "peak_prompt_tokens": max((u.get("prompt_tokens", 0) for u in usages), default=0),
                        "model_seconds": round(sum(c.get("duration_ms") or 0 for c in model_calls) / 1000, 3),
                        "corrections": len(validation.get("rejected_attempts", [])),
                        "application_tool_calls": sum(c["request_key"].startswith("evidence:") for c in tool_calls)}}
            result["cases"].append(case)
            with tasks.connect() as conn:
                result["no_new_tasks"] = conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == task_count
            checks["no_new_tasks"] = result["no_new_tasks"]
            (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"case": name, "repetition": repetition, "checks": checks,
                              "seconds": case["duration_seconds"], "metrics": case["metrics"],
                              "error": reply.get("error"), "selected": sorted(selected)}, ensure_ascii=False), flush=True)
            if not result["no_new_tasks"]:
                raise RuntimeError("Unexpected task submission; inspect the queue before running a worker.")
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["passed"] = result["no_new_tasks"] and all(all(case["checks"].values()) for case in result["cases"])
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not result["passed"]:
        raise RuntimeError("Some live explanation checks did not pass; evidence has been preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--catalog", type=Path, default=Path("var/catalog.sqlite3"))
    parser.add_argument("--case", action="append", choices=[case[0] for case in CASES],
                        help="Repeat only selected cases; use a new output directory to preserve failures.")
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 21), help="Repeat every selected question with new request IDs.")
    parser.add_argument("--expected-provider", choices=["primary", "backup"], help="Check configuration and every actual provider attempt.")
    parser.add_argument("--output", type=Path, default=Path("var/verification/explanation-regression"))
    args = parser.parse_args()
    # Share the API's lifetime lock. API startup must not recover a live
    # standalone regression request as an interrupted message (or vice versa).
    with Path(str(args.catalog.resolve()) + ".api.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Stop the API or other standalone regression before using this catalog.")
        run(args)


if __name__ == "__main__":
    main()
