"""Small, typed progress snapshots; units describe actual measured work."""
from typing import Literal

from pydantic import Field, model_validator

from ..contracts import Contract


class ProgressMetric(Contract):
    key: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=80)
    current: int = Field(ge=0)
    total: int = Field(ge=0)
    unit: Literal["rows", "files", "bytes", "percent"]

    @model_validator(mode="after")
    def within_total(self):
        if self.current > self.total:
            raise ValueError("Progress cannot exceed its measured total.")
        if self.unit == "percent" and self.total != 100:
            raise ValueError("Percentage metrics use a total of 100.")
        return self


class StageProgress(Contract):
    message: str = Field(max_length=200)
    metrics: list[ProgressMetric] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def unique_metrics(self):
        if len({metric.key for metric in self.metrics}) != len(self.metrics):
            raise ValueError("Progress metric keys must be unique.")
        return self


def metric(key, label, current, total, unit):
    return {"key": key, "label": label, "current": current, "total": total, "unit": unit}
