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
    timestamp_min: int = Field(ge=0, le=253402300799, strict=True)
    timestamp_max: int = Field(ge=0, le=253402300799, strict=True)


class ExtendedRuleConfig(RuleConfig):
    version: Literal["rules-v2"]
    quarantine_zip_warnings: bool = Field(default=False, strict=True)
    quarantine_title_warnings: bool = Field(default=False, strict=True)
    quarantine_encoding_warnings: bool = Field(default=False, strict=True)


class MetricConfig(Contract):
    version: Literal["metrics-v1"]
    formula: Literal["constraint-ratios-equal-tables"]
    reference_time: int = Field(ge=0, le=253402300799, strict=True)
    window_seconds: int = Field(gt=0, le=253402300799, strict=True)


class TableWeights(Contract):
    users: int = Field(ge=1, le=1000, strict=True)
    movies: int = Field(ge=1, le=1000, strict=True)
    ratings: int = Field(ge=1, le=1000, strict=True)


class WeightedMetricConfig(MetricConfig):
    version: Literal["metrics-v2"]
    formula: Literal["constraint-ratios-weighted-tables"]
    table_weights: TableWeights


class SplitConfig(Contract):
    version: Literal["split-v1"]
    train_end: int = Field(ge=0, le=253402300799, strict=True)
    validation_end: int = Field(ge=0, le=253402300799, strict=True)


class GovernanceConfig(Contract):
    schema_version: Literal["1"]
    rules: RuleConfig | ExtendedRuleConfig = Field(discriminator="version")
    metrics: MetricConfig | WeightedMetricConfig = Field(discriminator="version")
    split: SplitConfig

    @model_validator(mode="after")
    def check_times(self):
        if not (self.rules.timestamp_min <= self.split.train_end
                < self.split.validation_end < self.rules.timestamp_max):
            raise ValueError("Time split must lie inside the permitted event range.")
        if self.metrics.version == "metrics-v1" and self.metrics.reference_time != self.rules.timestamp_max:
            raise ValueError("v1 evaluates freshness at the fixed historical snapshot.")
        if self.metrics.window_seconds > self.metrics.reference_time:
            raise ValueError("Freshness window must not start before Unix epoch.")
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

    def ref(self) -> dict:
        return {"artifact_id": "governance.configuration", "version": digest(self.model_dump(mode="json"))}

    def aggregation_description(self) -> str:
        if self.metrics.version == "metrics-v1":
            return "四项先分表计算再等权汇总"
        weights = self.metrics.table_weights
        return (f"四项先分表计算，再按用户:电影:评分 = {weights.users}:{weights.movies}:{weights.ratings}"
                " 的相对权重汇总")

    def warning_description(self) -> str:
        flags = (("quarantine_zip_warnings", "非典型邮编"),
                 ("quarantine_title_warnings", "标题缺少年份"),
                 ("quarantine_encoding_warnings", "疑似混合编码"))
        return "；".join(f"{label}：{'隔离' if getattr(self.rules, flag, False) else '仅警告'}"
                        for flag, label in flags) + "。这些属于方案策略，未核验事实真伪或猜测补全。"
