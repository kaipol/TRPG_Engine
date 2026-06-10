"""Smoke-test frontend static routing and asset security boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.main import app


def main() -> None:
    client = TestClient(app)
    checks = [
        ("/", "text/html", "Z.R.I.C"),
        ("/multiplayer.html", "text/html", "Z.R.I.C"),
        ("/assets/index.css", "text/css", ".launch"),
        ("/assets/index-app.js", "text/javascript", "createApp"),
        ("/assets/multiplayer.css", "text/css", ".solo"),
        ("/assets/multiplayer-app.js", "text/javascript", "createApp"),
        ("/api/campaigns", "application/json", "status"),
    ]
    for path, expected_type, expected_text in checks:
        response = client.get(path)
        content_type = response.headers.get("content-type", "")
        assert response.status_code == 200, (path, response.status_code, response.text[:200])
        assert expected_type in content_type, (path, content_type)
        assert expected_text in response.text, path

    blocked_paths = [
        "/assets/%2e%2e/index.html",
        "/assets/%2e%2e%2findex.html",
        "/assets/%5cindex.html",
        "/assets/index.exe",
        "/api/not-a-static-file",
    ]
    for path in blocked_paths:
        response = client.get(path)
        assert response.status_code in {400, 404, 405}, (path, response.status_code)
    print("static contract smoke passed")


if __name__ == "__main__":
    main()
