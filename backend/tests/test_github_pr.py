from __future__ import annotations

from unittest import mock

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
