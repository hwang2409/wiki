"""Validate graph artifacts against the versioned D1 JSON Schemas."""

from __future__ import annotations

import argparse
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
except ImportError as exc:  # pragma: no cover - exercised by installation failures
    Draft202012Validator = None  # type: ignore[assignment]
    FormatChecker = None  # type: ignore[assignment,misc]
    Registry = None  # type: ignore[assignment,misc]
    Resource = None  # type: ignore[assignment,misc]
    _JSONSCHEMA_IMPORT_ERROR = exc
else:
    _JSONSCHEMA_IMPORT_ERROR = None


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
SCHEMA_NAMES = ("finding", "verdict", "steer", "edge", "workgraph")


class GraphLintError(ValueError):
    """A graph artifact could not be loaded or its schema could not be selected."""


def _schema_path(name: str) -> Path:
    candidate = Path(name)
    if candidate.name in {f"{schema}.schema.json" for schema in SCHEMA_NAMES}:
        path = SCHEMA_DIR / candidate.name
    elif candidate.suffix == ".json" or candidate.parent != Path("."):
        path = candidate if candidate.is_absolute() else Path.cwd() / candidate
    else:
        path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise GraphLintError(
            f"unknown schema {name!r}; expected one of {', '.join(SCHEMA_NAMES)}"
        )
    return path


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = _schema_path(name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphLintError(f"could not read schema {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphLintError(f"schema {path} must contain a JSON object")
    return payload


@lru_cache(maxsize=1)
def _schema_registry():
    registry = Registry()
    for name in SCHEMA_NAMES:
        schema = load_schema(name)
        schema_id = schema.get("$id")
        if isinstance(schema_id, str):
            registry = registry.with_resource(
                schema_id,
                Resource.from_contents(schema),
            )
    return registry


def schema_name_for_override(value: str) -> str:
    """Normalize a schema alias or a path to a checked-in schema."""
    raw = value.strip()
    if raw in SCHEMA_NAMES:
        return raw
    path = _schema_path(raw)
    for name in SCHEMA_NAMES:
        if path == SCHEMA_DIR / f"{name}.schema.json":
            return name
    raise GraphLintError(f"schema override must refer to a D1 schema: {value}")


def detect_schema(document: Any) -> str | None:
    """Select the artifact schema from its top-level shape."""
    if not isinstance(document, dict):
        return None
    if {"ticket", "nodes", "edges"}.issubset(document):
        return "workgraph"
    if {"kind", "from", "to"}.issubset(document):
        return "edge"
    if {"target_worker", "mode", "findings"}.issubset(document):
        return "steer"
    if {"worker", "state", "findings"}.issubset(document):
        return "verdict"
    if {"id", "severity", "observed", "why_wrong", "do_instead"}.issubset(document):
        return "finding"
    return None


def _json_pointer(parts: Iterable[Any]) -> str:
    encoded = []
    for part in parts:
        text = str(part).replace("~", "~0").replace("/", "~1")
        encoded.append(text)
    return "/" + "/".join(encoded) if encoded else "/"


def _message(value: str) -> str:
    return " ".join(value.splitlines())


def _validator(schema_name: str):
    if Draft202012Validator is None or FormatChecker is None or Registry is None or Resource is None:
        raise GraphLintError(
            "graph lint requires the jsonschema package; install backend/requirements.txt"
        ) from _JSONSCHEMA_IMPORT_ERROR
    schema = load_schema(schema_name)
    return Draft202012Validator(
        schema,
        registry=_schema_registry(),
        format_checker=FormatChecker(),
    )


def _sha_violations(document: Any) -> list[tuple[str, str]]:
    if not isinstance(document, dict):
        return []
    verdict_sha = document.get("sha")
    findings = document.get("findings")
    if not isinstance(verdict_sha, str) or not isinstance(findings, list):
        return []
    violations = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        source_sha = finding.get("source_sha")
        if isinstance(source_sha, str) and source_sha != verdict_sha:
            violations.append(
                (
                    f"/findings/{index}/source_sha",
                    f"must match verdict sha {verdict_sha!r} (got {source_sha!r})",
                )
            )
    return violations


def validate_document(document: Any, schema_name: str) -> list[tuple[str, str]]:
    """Return ``(JSON pointer, message)`` violations in stable order."""
    errors = [
        (_json_pointer(error.absolute_path), _message(error.message))
        for error in _validator(schema_name).iter_errors(document)
    ]
    if schema_name == "verdict":
        errors.extend(_sha_violations(document))
    return sorted(errors, key=lambda item: (item[0], item[1]))


def lint_path(path: str | Path, schema_override: str | None = None) -> list[str]:
    """Return formatted violations for one graph artifact."""
    artifact_path = Path(path)
    try:
        document = json.loads(artifact_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{artifact_path}: /: file does not exist"]
    except OSError as exc:
        return [f"{artifact_path}: /: could not read file: {_message(str(exc))}"]
    except json.JSONDecodeError as exc:
        return [f"{artifact_path}: /: invalid JSON: {_message(exc.msg)}"]

    try:
        schema_name = (
            schema_name_for_override(schema_override)
            if schema_override
            else detect_schema(document)
        )
    except GraphLintError as exc:
        return [f"{artifact_path}: /: {_message(str(exc))}"]
    if schema_name is None:
        return [
            f"{artifact_path}: /: unable to auto-detect schema; pass --schema"
        ]

    try:
        violations = validate_document(document, schema_name)
    except GraphLintError as exc:
        return [f"{artifact_path}: /: {_message(str(exc))}"]
    return [f"{artifact_path}: {pointer}: {message}" for pointer, message in violations]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki graph lint")
    parser.add_argument("path", help="JSON artifact to validate")
    parser.add_argument(
        "--schema",
        help="schema name (finding, verdict, steer, edge, or workgraph) or schema path",
    )
    args = parser.parse_args(argv)
    violations = lint_path(args.path, args.schema)
    if violations:
        print("\n".join(violations))
        return 1
    print(f"{args.path}: valid")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
