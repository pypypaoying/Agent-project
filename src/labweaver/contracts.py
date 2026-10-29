"""Versioned task intent and verification records; never executable model input."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_type: Literal["classification", "regression"]
    target: str = Field(pattern=r"^c[1-9][0-9]*$")
    features: list[str] = Field(min_length=1, max_length=199)
    split: Literal["random", "stratified", "time", "group", "custom"] = "random"
    test_size: float = Field(default=0.2, gt=0, lt=1)
    seed: int = 42
    duplicate_policy: Literal["keep", "drop_exact"] = "keep"
    prediction_delivery: str
    metadata_delivery: str
    metrics_delivery: str

    @model_validator(mode="after")
    def unique_features(self):
        import re
        if self.task_type == "regression" and self.split == "stratified":
            raise ValueError("stratified means original classification labels. For ordinary regression use random; explicitly binned/grouped custom splits require split=custom and their own stated policy.")
        if self.target in self.features or len(set(self.features)) != len(self.features):
            raise ValueError("Target cannot be a feature; features must be unique")
        if any(not re.fullmatch(r"c[1-9][0-9]*", x) for x in self.features):
            raise ValueError("Use stable c1..cN column IDs")
        return self


class FactCheck(BaseModel):
    """Optional numeric relations over registered outputs, not arbitrary code."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["row_count", "sum", "mean", "min", "max"]
    table_delivery: str
    column: str | None = None
    metric_delivery: str
    metric_key: str


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=4000)
    data_scope: str = Field(default="全部输入记录；筛选依据用户任务", max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    modeling: ModelingSpec | None = None
    checks: list[FactCheck] = Field(default_factory=list, max_length=20)

    def validate_bindings(self, column_count, deliveries):
        kinds = {d["id"]: d["kind"] for d in deliveries}
        if self.modeling:
            m = self.modeling
            if any(int(c[1:]) > column_count for c in [m.target, *m.features]):
                raise ValueError("Unknown source column")
            for key, kind in [(m.prediction_delivery, "table"), (m.metadata_delivery, "metric"), (m.metrics_delivery, "metric")]:
                if kinds.get(key) != kind:
                    raise ValueError(f"Deliverable {key!r} must have kind={kind!r}; got {kinds.get(key)!r}. Modeling requires predictions(table), metadata(metric), metrics(metric).")
        for check in self.checks:
            if kinds.get(check.table_delivery) != "table" or kinds.get(check.metric_delivery) != "metric":
                raise ValueError("Fact check references unknown deliverables")


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check: str
    status: Literal["passed", "failed", "not_applicable", "not_checked"]
    required: bool = False
    reason: str
    evidence: dict = Field(default_factory=dict)
