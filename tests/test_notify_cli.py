from __future__ import annotations

from pathlib import Path

import pytest

from japan_housing.cli import _dashboard_assets
from japan_housing.notify import resolve_webhook_url, validate_webhook_url


def test_webhook_env_is_parsed_as_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/12345/test-token'\n",
        encoding="utf-8",
    )

    value = resolve_webhook_url(path)

    assert value.endswith("/12345/test-token")


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/token",
        "https://evil.invalid/api/webhooks/123/token",
        "https://discord.com:444/api/webhooks/123/token",
        "https://discord.com/api/not-webhooks/123/token",
    ],
)
def test_invalid_webhook_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_dashboard_assets_exist_in_checkout() -> None:
    root = _dashboard_assets()
    assert {"index.html", "app.js", "styles.css"} <= {path.name for path in root.iterdir()}
