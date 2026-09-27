"""Validated, content-addressed governance configurations."""
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..contracts import Contract


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def digest(value) -> str:
    return "sha256-" + hashlib.sha256(canonical(value).encode()).hexdigest()


class RuleConfig(Contract):
    version: Literal["rules-v1"]
    policy: Literal["valid-first-isolate-conflicts"]
    timestamp_min: int = Field(ge=0)
    timestamp_max: int = Field(le=253402300799)


class MetricConfig(Contract):
    version: Literal["metrics-v1"]
    formula: Literal["constraint-ratios-equal-tables"]
    reference_time: int = Field(ge=0, le=253402300799)
    window_seconds: int = Field(gt=0)


class SplitConfig(Contract):
    version: Literal["split-v1"]
    train_end: int
    validation_end: int


class GovernanceConfig(Contract):
    schema_version: Literal["1"]
    rules: RuleConfig
    metrics: MetricConfig
    split: SplitConfig

    @model_validator(mode="after")
    def check_times(self):
        if not (self.rules.timestamp_min <= self.split.train_end
                < self.split.validation_end < self.rules.timestamp_max):
            raise ValueError("Time split must lie inside the permitted event range.")
        if self.metrics.reference_time != self.rules.timestamp_max:
            raise ValueError("v1 evaluates freshness at the fixed historical snapshot.")
        return self

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def refs(self) -> dict:
        return {
            name: {"artifact_id": f"governance.{name}", "version": digest(
                getattr(self, name).model_dump(mode="json"))}
            for name in ("rules", "metrics", "split")
        }
