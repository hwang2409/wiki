from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import FastAPI

from backend.app.frontend_static import mount_frontend_static


class FrontendStaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.dist = Path(self.tmp.name)
        (self.dist / "assets").mkdir()
        (self.dist / "index.html").write_text(
            '<script type="module" src="/assets/dummy-HASH.js"></script>',
            encoding="utf-8",
        )
        (self.dist / "favicon.svg").write_text("<svg />", encoding="utf-8")
        (self.dist / "assets" / "dummy-HASH.js").write_text(
            "console.log('ok')", encoding="utf-8"
        )

        self.app = FastAPI()

        @self.app.get("/api/ping")
        def ping() -> dict[str, str]:
            return {"status": "ok"}

        self.patcher = mock.patch.dict(
            os.environ, {"WIKI_FRONTEND_DIST": str(self.dist)}, clear=False
        )
        self.patcher.start()
        mount_frontend_static(self.app)

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tmp.cleanup()

    def _request(
        self, path: str, headers: tuple[tuple[bytes, bytes], ...] = ()
    ) -> tuple[int, dict[str, str]]:
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": list(headers),
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        }
        asyncio.run(self.app(scope, receive, send))

        response = next(
            message
            for message in messages
            if message["type"] == "http.response.start"
        )
        response_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in response["headers"]  # type: ignore[index]
        }
        return response["status"], response_headers  # type: ignore[return-value]

    def test_entry_files_are_no_cache(self) -> None:
        for path in ("/", "/index.html", "/favicon.svg"):
            with self.subTest(path=path):
                status, headers = self._request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["cache-control"], "no-cache")

    def test_hashed_assets_are_immutable(self) -> None:
        status, headers = self._request("/assets/dummy-HASH.js")

        self.assertEqual(status, 200)
        self.assertEqual(
            headers["cache-control"], "public, max-age=31536000, immutable"
        )

    def test_index_conditional_request_is_not_modified(self) -> None:
        status, headers = self._request("/index.html")
        self.assertEqual(status, 200)

        conditional_status, conditional_headers = self._request(
            "/index.html",
            ((b"if-none-match", headers["etag"].encode("latin-1")),),
        )

        self.assertEqual(conditional_status, 304)
        self.assertEqual(conditional_headers["etag"], headers["etag"])
        self.assertEqual(conditional_headers["cache-control"], "no-cache")

    def test_api_routes_do_not_get_frontend_cache_headers(self) -> None:
        status, headers = self._request("/api/ping")

        self.assertEqual(status, 200)
        self.assertNotIn("cache-control", headers)


if __name__ == "__main__":
    unittest.main()
