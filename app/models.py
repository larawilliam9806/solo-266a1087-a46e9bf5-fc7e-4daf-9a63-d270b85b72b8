"""Request models for JSON payloads."""

from __future__ import annotations

from pydantic import BaseModel, Field, StrictStr, field_validator

DIMENSIONS = ("district", "size_band")


class RespondentIn(BaseModel):
    household_id: StrictStr = Field(..., alias="household_id")
    district: StrictStr
    size_band: StrictStr
    init_weight: float = Field(..., alias="init_weight")

    model_config = {"extra": "ignore", "populate_by_name": True}

    @field_validator("household_id", "district", "size_band")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("init_weight")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        if v <= 0:
            raise ValueError("init_weight must be positive")
        return float(v)


class RespondentBatch(BaseModel):
    records: list[RespondentIn] = Field(..., min_length=1)


class TargetIn(BaseModel):
    dimension: StrictStr
    group_name: StrictStr
    target_count: float

    model_config = {"extra": "ignore"}

    @field_validator("dimension")
    @classmethod
    def _known_dimension(cls, v: str) -> str:
        v = v.strip()
        if v not in DIMENSIONS:
            raise ValueError(f"dimension must be one of {DIMENSIONS}")
        return v

    @field_validator("group_name")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("target_count")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        if v < 0:
            raise ValueError("target_count must be non-negative")
        return float(v)


class TargetBatch(BaseModel):
    records: list[TargetIn] = Field(..., min_length=1)


class SupplementPlanRequest(BaseModel):
    """无法配平的作业号 + 至多 30 户候补资料（字段沿用应答名单）。"""

    job_id: int = Field(..., gt=0)
    candidates: list[RespondentIn] = Field(..., min_length=1, max_length=30)


class SupplementCompleteRequest(BaseModel):
    """补访结束后凭方案号回交的已应答户号。"""

    responded_household_ids: list[StrictStr] = Field(..., min_length=1)

    @field_validator("responded_household_ids")
    @classmethod
    def _non_blank_ids(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v for v in cleaned):
            raise ValueError("household_id must not be blank")
        return cleaned
