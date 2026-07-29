"""Shared request schema for the next-review API and MCP tool."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


ReviewerKind = Literal["cc", "cdx"]
ReviewerEffort = Literal["minimal", "low", "medium", "high", "xhigh"]


class NextReviewIn(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Z0-9-]+$")
    pr_number: int = Field(..., ge=1)
    expected_sha: str = Field(
        ..., min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$"
    )
    orch: str = Field(..., min_length=1, max_length=100)
    reviewer_kind: ReviewerKind = "cdx"
    reviewer_model: str = Field(default="gpt-5.6-sol", min_length=2, max_length=64)
    reviewer_effort: ReviewerEffort | None = None
    prompt_template: str | None = Field(default=None, max_length=100_000)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_provider_effort(self) -> NextReviewIn:
        if self.reviewer_kind == "cdx":
            self.reviewer_effort = self.reviewer_effort or "high"
        elif self.reviewer_effort is not None:
            raise ValueError("Claude reviewers do not accept reasoning effort")
        return self


def mcp_input_schema() -> dict[str, object]:
    """Return the endpoint schema with only the MCP caller-owned fields."""

    schema = NextReviewIn.model_json_schema()
    properties = dict(schema.get("properties") or {})
    properties.pop("orch", None)
    required = [field for field in schema.get("required", []) if field != "orch"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


__all__ = ["NextReviewIn", "mcp_input_schema"]
