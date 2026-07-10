from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

from backend.app import github_preview, main


def _gh_ok(stdout: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["gh"],
        0,
        stdout=json.dumps(stdout),
        stderr="",
    )


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/gh/preview",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in (headers or {}).items()
            ],
        }
    )


class GitHubPreviewRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        github_preview._preview_cache.clear()  # noqa: SLF001 - test isolation

    def tearDown(self) -> None:
        github_preview._preview_cache.clear()  # noqa: SLF001 - test isolation

    def test_pr_preview_uses_cache_and_etag(self) -> None:
        url = "https://github.com/hwang2409/wiki/pull/45"
        pr_payload = {
            "title": "WIKI-58 inline thinking rows and font picker",
            "state": "MERGED",
            "mergeStateStatus": "CLEAN",
            "updatedAt": "2026-07-10T16:20:00Z",
            "changedFiles": 3,
            "statusCheckRollup": [
                {"name": "unit", "conclusion": "SUCCESS"},
                {"name": "lint", "conclusion": "FAILURE"},
                {"name": "deploy", "status": "IN_PROGRESS"},
            ],
        }

        with mock.patch("backend.app.github_preview.subprocess.run", return_value=_gh_ok(pr_payload)) as gh_run:
            first = main.gh_preview_card(_request(), url=url)
            first_payload = json.loads(first.body)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(
                first_payload,
                {
                    "ok": True,
                    "kind": "pr",
                    "title": "WIKI-58 inline thinking rows and font picker",
                    "state": "MERGED",
                    "extra": {
                        "mergeStateStatus": "CLEAN",
                        "checks": {"pass": 1, "fail": 1, "pending": 1},
                        "changedFiles": 3,
                        "updatedAt": "2026-07-10T16:20:00Z",
                    },
                },
            )
            etag = first.headers["etag"]

            second = main.gh_preview_card(_request(), url=url)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.headers["etag"], etag)

            cached = main.gh_preview_card(_request({"if-none-match": etag}), url=url)
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(gh_run.call_count, 1)

    def test_issue_and_commit_preview_shapes(self) -> None:
        issue_url = "https://github.com/hwang2409/wiki/issues/64"
        commit_url = "https://github.com/hwang2409/wiki/commit/467c1a2fc52a9d8072282894b92a954ccb17727e"

        def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if args[1:3] == ["issue", "view"]:
                return _gh_ok(
                    {
                        "title": "URL preview cards",
                        "state": "OPEN",
                        "updatedAt": "2026-07-10T18:00:00Z",
                    }
                )
            if args[1] == "api":
                return _gh_ok(
                    {
                        "sha": "467c1a2fc52a9d8072282894b92a954ccb17727e",
                        "author": {"login": "hwang2409"},
                        "commit": {
                            "message": "Add GitHub URL previews\n\nBody",
                            "author": {"date": "2026-07-10T15:02:00Z", "name": "Henry"},
                        },
                    }
                )
            raise AssertionError(f"unexpected gh args: {args}")

        with mock.patch("backend.app.github_preview.subprocess.run", side_effect=fake_run):
            issue = main.gh_preview_card(_request(), url=issue_url)
            commit = main.gh_preview_card(_request(), url=commit_url)

        self.assertEqual(issue.status_code, 200)
        self.assertEqual(
            json.loads(issue.body),
            {
                "ok": True,
                "kind": "issue",
                "title": "URL preview cards",
                "state": "OPEN",
                "extra": {"updatedAt": "2026-07-10T18:00:00Z"},
            },
        )
        self.assertEqual(commit.status_code, 200)
        self.assertEqual(
            json.loads(commit.body),
            {
                "ok": True,
                "kind": "commit",
                "title": "Add GitHub URL previews",
                "state": None,
                "extra": {
                    "sha": "467c1a2fc52a9d8072282894b92a954ccb17727e",
                    "author": "hwang2409",
                    "date": "2026-07-10T15:02:00Z",
                },
            },
        )

    def test_invalid_url_and_gh_failure_are_handled_cleanly(self) -> None:
        with self.assertRaises(HTTPException) as invalid:
            main.gh_preview_card(
                _request(),
                url="https://github.com/hwang2409/wiki/pulls/45",
            )
        self.assertEqual(invalid.exception.status_code, 400)
        self.assertEqual(invalid.exception.detail, "Invalid GitHub preview URL")

        failing = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="boom")
        with mock.patch("backend.app.github_preview.subprocess.run", return_value=failing):
            error = main.gh_preview_card(
                _request(),
                url="https://github.com/hwang2409/wiki/pull/45",
            )

        self.assertEqual(error.status_code, 502)
        self.assertEqual(json.loads(error.body), {"ok": False, "error": "boom"})

    def test_cache_is_lru_bounded(self) -> None:
        payload = {
            "title": "Bounded cache test",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "updatedAt": "2026-07-10T16:20:00Z",
            "changedFiles": 1,
            "statusCheckRollup": [],
        }

        with mock.patch("backend.app.github_preview.subprocess.run", return_value=_gh_ok(payload)):
            original_limit = github_preview.PREVIEW_CACHE_MAX_SIZE
            github_preview.PREVIEW_CACHE_MAX_SIZE = 2
            try:
                main.gh_preview_card(_request(), url="https://github.com/hwang2409/wiki/pull/45")
                main.gh_preview_card(_request(), url="https://github.com/hwang2409/wiki/pull/46")
                main.gh_preview_card(_request(), url="https://github.com/hwang2409/wiki/pull/45")
                main.gh_preview_card(_request(), url="https://github.com/hwang2409/wiki/pull/47")
            finally:
                github_preview.PREVIEW_CACHE_MAX_SIZE = original_limit

        self.assertEqual(
            list(github_preview._preview_cache.keys()),  # noqa: SLF001 - cache contract test
            [
                "https://github.com/hwang2409/wiki/pull/45",
                "https://github.com/hwang2409/wiki/pull/47",
            ],
        )
