"""Published test reports built with the small, hand-checkable streaming fixture."""
from copy import deepcopy
from functools import lru_cache
import json

from governance_fixture import DATA
from test_governance import aggregate, envelopes, index, job
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.governance.report import LIMITATIONS
from movielens_agent.workflows.governance import make_artifact


@lru_cache
def computed():
    raw = {table: envelopes(table, content) for table, content in DATA.items()}
    raw_parents = job(raw["users"] + raw["movies"])
    before = aggregate(raw_parents + job(raw["ratings"], parents=index(raw_parents)))
    parents = job(raw["users"] + raw["movies"], mode="clean")
    ratings = job(raw["ratings"], mode="clean", parents=index(parents), raw_parents=index(raw_parents))
    clean = parents + ratings
    after = aggregate(job(clean, parents=index(parents)) + clean)
    records = [json.loads(row) for row in clean if json.loads(row)["kind"] == "record"]
    return before, after, records


def report(task_id="fixture"):
    before, after, _ = deepcopy(computed())
    config = GovernanceConfig.model_validate(before["configuration"])
    return {"schema_version": "1", "task_id": task_id,
            "input_ref": {"artifact_id": "fixture", "version": "1"},
            "cleaned_ref": {"artifact_id": task_id + ".cleaned", "version": "test-cleaned"},
            "config_refs": config.refs(), "configuration": config.model_dump(mode="json"),
            "before": before, "after": after, "limitations": list(LIMITATIONS)}


def publish(fixture, key="quality"):
    task = fixture.tasks.submit(fixture.session, key, "governance.v1", {})[0]
    fixture.tasks.claim()
    directory = fixture.root / task
    directory.mkdir()
    value = report(task)
    path = directory / "quality.json"
    path.write_text(json.dumps(value))
    artifact = make_artifact(task, "quality", "quality_report", [path], {})
    fixture.tasks.publish(task, [artifact])
    return task, artifact["ref"], value
