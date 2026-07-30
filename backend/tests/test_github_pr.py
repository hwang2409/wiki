from __future__ import annotations

from unittest import mock

import pytest
from fastapi import HTTPException

from backend.app import github_pr


def test_merge_binds_gh_merge_to_reviewed_head_sha() -> None:
    with (
        mock.patch.object(github_pr, "_run_gh") as run_gh,
    ):
        github_pr.merge_pr("https://github.com/hwang2409/wiki/pull/140", "0123456789abcdef")

    args = run_gh.call_args.args[0]
    assert args == [
        "pr",
        "merge",
        "https://github.com/hwang2409/wiki/pull/140",
        "--squash",
        "--match-head-commit=0123456789abcdef",
    ]


def test_merge_rejects_unknown_repository_before_gh() -> None:
    with mock.patch.object(github_pr, "_run_gh") as run_gh:
        with pytest.raises(HTTPException) as exc_info:
            github_pr.merge_pr("https://github.com/example/other/pull/140", "0123456789abcdef")

    assert exc_info.value.status_code == 403
    run_gh.assert_not_called()
