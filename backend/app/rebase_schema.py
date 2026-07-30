"""Shared request schema for the rebase endpoint and MCP tool."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RebaseDirtyPrIn(BaseModel):
    pr_number: int = Field(..., ge=1)
    ticket: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Z0-9-]+$")
    worker_id: str = Field(..., min_length=1, max_length=100)


def mcp_input_schema() -> dict[str, object]:
    schema = RebaseDirtyPrIn.model_json_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(schema.get("required", [])),
        "properties": dict(schema.get("properties") or {}),
    }


__all__ = ["RebaseDirtyPrIn", "mcp_input_schema"]
