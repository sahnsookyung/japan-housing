from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from japan_housing.config import RuntimeConfig
from japan_housing.http import BoundedHttpClient, HttpFailure, ResponseTooLarge


def _client(handler, *, response_limit: int = 1024) -> BoundedHttpClient:
    config = RuntimeConfig(
        state_path=Path("unused.sqlite3"),
        artifacts_path=Path("unused-artifacts"),
        global_workers=1,
        per_host_workers=1,
        max_queued_tasks=1,
        per_host_interval_seconds=0.0001,
        request_budget_per_run=10,
        response_limit_bytes=response_limit,
        max_attempts=1,
    )
    client = BoundedHttpClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def test_redirect_cannot_escape_source_allowlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/collect"})

    with (
        _client(handler) as client,
        pytest.raises(HttpFailure, match="outside the source allowlist"),
    ):
        client.get("https://source.example/start", allowed_hosts=frozenset({"source.example"}))


def test_response_body_limit_is_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 33)

    with _client(handler, response_limit=32) as client, pytest.raises(ResponseTooLarge):
        client.get("https://source.example/large", allowed_hosts=frozenset({"source.example"}))


def test_robots_uncertainty_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _client(handler) as client:
        assert not client.robots_allowed(
            "https://source.example/list", allowed_hosts=frozenset({"source.example"})
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://source.example/list",
        "https://source.example:444/list",
        "https://user:pass@source.example/list",
    ],
)
def test_unsafe_source_urls_are_rejected(url: str) -> None:
    with pytest.raises(HttpFailure):
        BoundedHttpClient._check_url(url, frozenset({"source.example"}))


def test_conditional_request_and_304_are_supported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-None-Match"] == '"fixture"'
        return httpx.Response(304, headers={"ETag": '"fixture"'})

    with _client(handler) as client:
        response = client.get(
            "https://source.example/detail/1",
            allowed_hosts=frozenset({"source.example"}),
            request_headers={"If-None-Match": '"fixture"'},
        )
        snapshot = client.snapshot(frozenset({"source.example"}))

    assert response.status_code == 304
    assert response.body_size == 0
    assert snapshot["statuses"] == {"304": 1}
