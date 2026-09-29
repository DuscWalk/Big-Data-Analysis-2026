"""Bounded, validated governance configuration actions for natural language."""
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from ..contracts import ArtifactRef, Contract
from ..governance.config import GovernanceConfig, TableWeights
from ..storage.governance_configs import ConfigurationConflict
from .governance import ObjectResult
from .registry import QueryTool, ToolRejected


def timestamp(value):
    if value is None or type(value) is int:
        return value
    if isinstance(value, str):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is not None and moment.microsecond == 0:
            return int(moment.timestamp())
    raise ValueError("时间应为整数 Unix 秒或含时区的 ISO 日期时间，精确到秒。")


class RuleChanges(Contract):
    timestamp_min: int | str | None = None
    timestamp_max: int | str | None = None
    quarantine_zip_warnings: bool | None = Field(default=None, strict=True)
    quarantine_title_warnings: bool | None = Field(default=None, strict=True)
    quarantine_encoding_warnings: bool | None = Field(default=None, strict=True)

    _times = field_validator("timestamp_min", "timestamp_max", mode="before")(timestamp)


class MetricChanges(Contract):
    reference_time: int | str | None = None
    window_days: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    window_seconds: int | None = Field(default=None, gt=0, le=253402300799, strict=True)
    table_weights: TableWeights | None = None

    _times = field_validator("reference_time", mode="before")(timestamp)

    @model_validator(mode="after")
    def one_window(self):
        if self.window_days is not None and self.window_seconds is not None:
            raise ValueError("窗口天数和秒数只填写一项。")
        return self


class SplitChanges(Contract):
    train_end: int | str | None = None
    validation_end: int | str | None = None

    _times = field_validator("train_end", "validation_end", mode="before")(timestamp)


class ConfigureInput(Contract):
    name: str = Field(min_length=1, max_length=80)
    base_ref: ArtifactRef | None = Field(default=None, description="Scheme to copy; omit for this request's selected/default scheme.")
    rules: RuleChanges | None = None
    metrics: MetricChanges | None = None
    split: SplitChanges | None = None
    set_default: bool = False
    default_revision: str | None = Field(default=None, max_length=64,
        description="Required when set_default=true; read the current default.revision from governance.configs.")


class DefaultInput(Contract):
    config_ref: ArtifactRef
    default_revision: str = Field(min_length=1, max_length=64)


def register_configuration_tools(registry, configurations):
    def configure(args, context):
        try:
            base_ref = args.base_ref or context.configuration_ref or ArtifactRef.model_validate(configurations.default_ref)
            value = configurations.get(base_ref)["configuration"]
            for name in ("rules", "metrics", "split"):
                changes = getattr(args, name)
                patch = changes.model_dump(exclude_none=True) if changes else {}
                if not patch:
                    continue
                if name == "rules":
                    value[name]["version"] = "rules-v2"
                elif name == "metrics":
                    value[name].update(version="metrics-v2", formula="constraint-ratios-weighted-tables")
                    value[name].setdefault("table_weights", {"users": 1, "movies": 1, "ratings": 1})
                    if "window_days" in patch:
                        patch["window_seconds"] = patch.pop("window_days") * 86400
                value[name].update(patch)
            # New schemes support an independent freshness reference, including
            # when only the permitted event range was changed.
            if value["metrics"]["version"] == "metrics-v1" and value["metrics"]["reference_time"] != value["rules"]["timestamp_max"]:
                value["metrics"].update(version="metrics-v2", formula="constraint-ratios-weighted-tables",
                                         table_weights={"users": 1, "movies": 1, "ratings": 1})
            config = GovernanceConfig.model_validate(value)
            item = configurations.register(args.name, config, make_default=args.set_default, revision=args.default_revision)
            return ObjectResult(value=item | {"default": configurations.default(), "default_changed": args.set_default}), []
        except ConfigurationConflict as error:
            raise ToolRejected("CONFIG_CONFLICT", str(error)) from None
        except KeyError:
            raise ToolRejected("CONFIG_NOT_FOUND", "基础方案未登记，请先查询方案列表。") from None
        except ValueError:
            raise ToolRejected("INVALID_CONFIGURATION", "配置不合法：检查时间顺序、UTC 时区、窗口和权重；未保存或切换默认方案。") from None

    def set_default(args, context):
        try:
            item = configurations.set_default(args.config_ref, args.default_revision)
            return ObjectResult(value=item | {"default": configurations.default(), "default_changed": True}), []
        except ConfigurationConflict as error:
            raise ToolRejected("CONFIG_CONFLICT", str(error)) from None
        except KeyError:
            raise ToolRejected("CONFIG_NOT_FOUND", "治理方案未登记，默认方案未改变。") from None

    registry.register(QueryTool("governance.configure", "1",
        "Save a new immutable scheme by applying explicitly requested rule/metric/split changes to a registered base. "
        "Times accept Unix seconds or ISO date-time with timezone. Does not run cleaning. "
        "Only set_default when the user asks; otherwise return a scheme for subsequent tasks.",
        ConfigureInput, ObjectResult, configure, mode="action"))
    registry.register(QueryTool("governance.set_default", "1",
        "Make an existing registered scheme the shared default for future requests, only when explicitly requested. "
        "Requires the latest default revision from governance.configs; existing tasks remain unchanged.",
        DefaultInput, ObjectResult, set_default, mode="action"))
