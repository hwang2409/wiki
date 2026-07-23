"""Load and select the checked-in work-graph templates.

Selection is deliberately prefix-first. A future label provider may supply a
more specific kind, but labels are optional and never needed for the default
repo-specific implement template. Unknown prefixes are an input error and
exit nonzero rather than silently choosing a repository's workflow.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


class TemplateSelectionError(ValueError):
    """A ticket cannot be mapped to a checked-in work-graph template."""


PREFIX_TO_REPO = {
    "PHO": "phoebe",
    "WIKI": "wiki",
    "MITMWEB": "tooling",
    "TIX": "tooling",
    "GAU": "tooling",
    "PUF": "tooling",
}


def _template_dir() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "templates" / "workgraphs"
    return Path(__file__).resolve().parents[1] / "templates" / "workgraphs"


@lru_cache(maxsize=1)
def load_templates() -> dict[str, dict[str, Any]]:
    directory = _template_dir()
    if not directory.is_dir():
        raise TemplateSelectionError(f"template directory does not exist: {directory}")

    templates: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.workgraph.tpl.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TemplateSelectionError(f"could not read template {path}: {exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("template_id"), str):
            raise TemplateSelectionError(f"template {path} must contain a string template_id")
        template_id = document["template_id"]
        if template_id in templates:
            raise TemplateSelectionError(f"duplicate template_id {template_id!r}")
        templates[template_id] = document
    if not templates:
        raise TemplateSelectionError(f"no work-graph templates found in {directory}")
    return templates


def _ticket_prefix(ticket_id: str) -> str:
    ticket = ticket_id.strip().upper()
    if not ticket or "-" not in ticket:
        raise TemplateSelectionError(
            f"invalid ticket id {ticket_id!r}; expected PREFIX-number"
        )
    prefix, number = ticket.split("-", 1)
    if not prefix or not number:
        raise TemplateSelectionError(
            f"invalid ticket id {ticket_id!r}; expected PREFIX-number"
        )
    return prefix


def _normalise_labels(labels: Iterable[str] | None) -> set[str]:
    return {
        label.strip().lower()
        for label in (labels or ())
        if isinstance(label, str) and label.strip()
    }


def select_template(
    ticket_id: str,
    *,
    labels: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a template selection payload for ``ticket_id``.

    The CLI has no mandatory Linear dependency. Callers with a cheap label
    source can pass labels to select the explicit frontend, migration, or plan
    templates; otherwise the repo's ``.implement`` template is selected.
    """
    ticket = ticket_id.strip().upper()
    prefix = _ticket_prefix(ticket)
    repo = PREFIX_TO_REPO.get(prefix)
    if repo is None:
        supported = ", ".join(sorted(PREFIX_TO_REPO))
        raise TemplateSelectionError(
            f"unknown ticket prefix {prefix!r}; supported prefixes: {supported}"
        )

    label_set = _normalise_labels(labels)
    candidates: list[str] = []
    if "plan" in label_set:
        candidates.append(f"{repo}.plan")
    if "migration" in label_set:
        candidates.append(f"{repo}.migration")
    if "frontend" in label_set:
        candidates.append(f"{repo}.frontend")
    candidates.append(f"{repo}.implement")

    templates = load_templates()
    template_id = next((candidate for candidate in candidates if candidate in templates), None)
    if template_id is None:
        raise TemplateSelectionError(f"no implement template is available for repo {repo!r}")

    template = templates[template_id]
    return {
        "ticket_id": ticket,
        "repo": repo,
        "template_id": template_id,
        "roles": template["roles"],
        "source": "ticket-prefix" if not label_set else "ticket-prefix+labels",
    }
